"""Offline recovery against in-process clients and HTTP wire fakes only; never Nexconn's network."""
import dataclasses
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, wait

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import ConfigError, create_app
from app.nexconn import ChatApprovalChannel, NexconnClient, NexconnConfig, NexconnError
from app.nexconn.fallback import ChannelTransport
from app.nexconn.upstream import ObservedNexconn, UpstreamMonitor, error_reason
from app.playground.registry import Registry
from tests.test_nexconn_api import LIVE, TOKEN, decision_event, signed
from tests.test_playground import H, ask, extra, room, start


class EpochClock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class FakeLive:
    """Public client interface, with independently controllable transport failures."""
    def __init__(self):
        self.calls = []
        self.error = None
        self.fail_method = None

    def call(self, method, *args):
        self.calls.append((method, args))
        if self.error is not None and (self.fail_method is None or self.fail_method == method):
            raise self.error
        return f"live-{len(self.calls)}"

    def issue_token(self, *args):
        return self.call("issue_token", *args)

    def ensure_group(self, *args):
        return self.call("ensure_group", *args)

    def send_group_text(self, *args):
        return self.call("send_group_text", *args)


@pytest.fixture
def make_offline_app(service, monkeypatch, tmp_path):
    clients = []

    def make(*, failure=None, fallback="auto", approver_token=TOKEN, demo=False):
        monkeypatch.setenv("TRY_MODE", "on")
        monkeypatch.setenv("TRY_DIR", str(tmp_path / f"try{len(clients)}"))
        for key in ("TRY_CREATE_LIMIT", "TRY_JOIN_LIMIT", "TRY_AGENT_LIMIT", "TRY_AGENT_LIMIT_IP"):
            monkeypatch.setenv(key, "off")
        clock, fake = EpochClock(), FakeLive()
        fake.error = failure
        cfg = dataclasses.replace(LIVE, fallback=fallback)
        channel = ChatApprovalChannel(service, cfg, fake, dispatch="inline")
        monitor = UpstreamMonitor(fallback, clock=clock)
        channel.transport = ChannelTransport(channel, upstream=monitor)
        app = create_app(service, approver_token=approver_token, demo_mode=demo, agent_rate_limit=None,
                         tools_rate_limit=None, nexconn=channel)
        channel.bootstrap()    # synchronize with the application's bootstrap thread
        client = TestClient(app)
        clients.append(client)
        return client, fake, monitor, clock

    yield make
    for client in clients:
        client.app.state.playground.close()


@pytest.mark.parametrize("error, reason", [
    (NexconnError("credentials at a private host", status=401), "auth"),
    (NexconnError("not allowed", status=403), "auth"),
    (NexconnError("signature invalid", code=1002), "auth"),
    (NexconnError("quota exceeded for this app key", status=401, code=429), "auth"),
    (NexconnError("quota exceeded for this app key", status=403), "auth"),
    (NexconnError("quota exceeded for this app key", code=1003), "auth"),
    (NexconnError("User registration limit exceeded for this app key", status=429), "unknown"),
    (NexconnError("app key rejected", status=429, code=1002), "unknown"),
    (NexconnError("app key rejected", code=429), "unknown"),
    (NexconnError("app key rejected", status=503, code=1002), "server"),
    (NexconnError("app-key expired"), "auth"),
    (NexconnError("chat not enabled"), "auth"),
    (NexconnError("free plan ended"), "auth"),
    (NexconnError("/v4/auth/access-token/issue: HTTP 503", status=503), "server"),
    (NexconnError("/v4/auth/access-token/issue: ConnectTimeout", reason="timeout"), "timeout"),
    (httpx.ConnectError("private transport detail"), "timeout"),
    (NexconnError("no API host answered"), "timeout"),
    (NexconnError("something unrecognized"), "unknown"),
])
def test_reason_classes_do_not_confuse_the_auth_path_with_an_auth_error(error, reason):
    assert error_reason(error) == reason


def test_auth_immediate_and_safe_snapshot():
    monitor = UpstreamMonitor(clock=EpochClock())
    monitor.failure(NexconnError("expired secret=do-not-expose at private.example", status=401))
    state = monitor.snapshot()
    assert state["state"] == "offline" and state["reason"] == "auth"
    assert set(state) == {"state", "reason", "since", "last_probe_at", "fallback"}
    assert "+00:00" in state["since"] and "secret" not in json.dumps(state)
    before = state["since"]
    monitor.failure(NexconnError("different private detail", status=503))
    monitor.success()  # an in-flight normal call cannot undo an offline transition
    assert monitor.snapshot()["since"] == before and monitor.offline


@pytest.mark.parametrize("error", [httpx.ReadTimeout("x"), NexconnError("down", status=503)])
def test_two_consecutive_failures_and_success_resets_streak(error):
    monitor = UpstreamMonitor()
    monitor.failure(error)
    assert not monitor.offline
    monitor.success()
    monitor.failure(error)
    assert not monitor.offline
    monitor.failure(error)
    assert monitor.offline



def test_unrecognised_refusals_need_three_in_a_row():
    """An ended plan may come back as a business code we have never seen: 3 in a row -> offline."""
    monitor = UpstreamMonitor()
    odd = NexconnError("/v4/group-channel/create: HTTP 200, code 9999: service unavailable", code=9999, status=200)
    monitor.failure(odd); monitor.failure(odd)
    assert not monitor.offline
    monitor.success()                       # any success resets the streak
    monitor.failure(odd); monitor.failure(odd)
    assert not monitor.offline
    monitor.failure(odd)
    assert monitor.offline and monitor.snapshot()["reason"] == "unknown"


def test_forced_offline_reports_manual_reason():
    monitor = UpstreamMonitor("force")
    assert monitor.offline and monitor.snapshot()["reason"] == "manual"

def test_probe_recovers_and_is_rate_limited_with_injected_clock():
    clock, calls = EpochClock(), []
    monitor = UpstreamMonitor(probe_every_s=60, clock=clock)
    error = NexconnError("expired credentials", status=403)
    monitor.failure(error)
    monitor.set_probe(lambda: calls.append(clock()))
    for _ in range(5):
        assert monitor.snapshot(probe=True)["state"] == "offline"
        assert not monitor.maybe_probe(sdk_hint=True)
    assert calls == []
    clock.now += 60
    assert monitor.snapshot(probe=True)["state"] == "live"
    assert calls == [clock()]
    assert monitor.snapshot()["last_probe_at"] is not None


def test_failed_probe_keeps_original_since_and_bounds_retries():
    clock, fake = EpochClock(), FakeLive()
    fake.error = NexconnError("expired", status=401)
    monitor = UpstreamMonitor(clock=clock)
    monitor.set_probe(lambda: fake.issue_token("bot", "bot"))
    monitor.failure(fake.error)
    since = monitor.snapshot()["since"]
    clock.now += 60
    assert monitor.snapshot(probe=True)["state"] == "offline"
    for _ in range(10):
        monitor.snapshot(probe=True)
        monitor.maybe_probe(sdk_hint=True)
    assert len(fake.calls) == 1 and monitor.snapshot()["since"] == since


def test_concurrent_lazy_requests_reserve_only_one_probe():
    clock = EpochClock()
    monitor = UpstreamMonitor(clock=clock)
    monitor.failure(NexconnError("expired"))
    clock.now += 60
    entered, release, calls = threading.Event(), threading.Event(), []

    def probe():
        calls.append(1)
        entered.set()
        assert release.wait(5)

    monitor.set_probe(probe)
    with ThreadPoolExecutor(max_workers=8) as pool:
        first = pool.submit(monitor.snapshot, probe=True)
        assert entered.wait(5)
        others = list(pool.map(lambda _: monitor.snapshot(probe=True), range(20)))
        assert all(s["state"] == "offline" for s in others)
        release.set()
        assert first.result()["state"] == "live"
    assert calls == [1]


def responsive_requests(c, pool, *, ready_status=200, mock_token=None):
    # All requests share TestClient's portal and FastAPI's 40-worker pool. A lock convoy
    # on config must not exhaust that pool and prevent even liveness from answering.
    paths = ["/nexconn/config"] * 40 + ["/health", "/health/live", "/health/ready"]
    requests = [pool.submit(c.get, path) for path in paths]
    expected = [200] * 42 + [ready_status]
    if mock_token:
        requests.append(pool.submit(c.post, "/nexconn/mock/send",
                                    headers={"X-Nexconn-Token": mock_token}, json={"text": "still online"}))
        expected.append(200)
    _, pending = wait(requests, timeout=5)
    assert not pending, f"{len(pending)} requests stalled behind Nexconn I/O"
    responses = [request.result() for request in requests]
    assert [response.status_code for response in responses] == expected
    return responses


@pytest.mark.parametrize("method, path", [
    ("GET", "/nexconn/config"), ("GET", "/health"), ("GET", "/health/ready"),
    ("POST", "/nexconn/session"),
])
def test_slow_probe_does_not_block_config_health_or_offline_sends(make_offline_app, method, path):
    c, fake, monitor, clock = make_offline_app(failure=NexconnError("expired", status=401))
    session = c.post("/nexconn/session", headers={"X-Approver-Token": TOKEN}).json()
    entered, release, calls = threading.Event(), threading.Event(), []

    def probe():
        calls.append(1)
        entered.set()
        assert release.wait(10)

    monitor.set_probe(probe)
    fake.error = None
    clock.now += 60
    with c, ThreadPoolExecutor(max_workers=48) as pool:
        first = pool.submit(c.request, method, path, headers={"X-Approver-Token": TOKEN})
        try:
            assert entered.wait(5)
            responses = responsive_requests(c, pool, mock_token=session["token"])
            assert all(response.json()["mode"] == "mock" for response in responses[:40])
            assert not first.done()
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200
    assert calls == [1] and not monitor.offline


@pytest.mark.parametrize("operation", ["send", "session", "bootstrap"])
def test_slow_live_io_does_not_block_config_or_health(make_offline_app, service, monkeypatch, operation):
    c, fake, _, _ = make_offline_app()
    channel = c.app.state.nexconn
    entered, release = threading.Event(), threading.Event()
    method = {"send": "send_group_text", "session": "issue_token", "bootstrap": "ensure_group"}[operation]
    original = getattr(fake, method)

    def blocked(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(fake, method, blocked)
    if operation == "bootstrap":
        channel.ready = False
    with c, ThreadPoolExecutor(max_workers=48) as pool:
        if operation == "send":
            first = pool.submit(service.request_approval, "P-1003", 1, "slow live send")
        elif operation == "session":
            first = pool.submit(c.post, "/nexconn/session", headers={"X-Approver-Token": TOKEN})
        else:
            first = pool.submit(channel.bootstrap)
        try:
            assert entered.wait(5)
            responses = responsive_requests(c, pool, ready_status=503 if operation == "bootstrap" else 200)
            assert all(response.json()["mode"] == "live" for response in responses[:40])
            assert not first.done()
        finally:
            release.set()
        result = first.result(timeout=5)
        if operation == "send":
            assert result["approval_id"]
            assert channel.status()["posts"][0]["status"] == "SENT"
        elif operation == "session":
            assert result.status_code == 200 and result.json()["mode"] == "live"
        else:
            assert result and channel.ready


@pytest.mark.parametrize("fallback, offline", [("off", False), ("force", True)])
def test_off_and_force_never_change_state_or_probe(fallback, offline):
    monitor = UpstreamMonitor(fallback)
    monitor.set_probe(lambda: pytest.fail("disabled/forced monitor must not probe"))
    for _ in range(3):
        monitor.failure(NexconnError("expired", status=401))
        monitor.failure(httpx.ReadTimeout("x"))
        monitor.success(probe=True)
        monitor.maybe_probe(sdk_hint=True)
    assert monitor.offline is offline


@pytest.mark.parametrize("error", [NexconnError("user quota exceeded", code=1008),
                                 NexconnError("rate limited", status=429)])
def test_registration_capacity_does_not_take_upstream_offline(error):
    monitor = UpstreamMonitor()
    for _ in range(3):
        monitor.failure(error, registration=True)
    assert not monitor.offline


@pytest.mark.parametrize("error", [
    NexconnError("User registration limit exceeded for this app key", status=429),
    NexconnError("User quota exceeded for this app key", code=1008, status=200),
    NexconnError("User limit reached for this app-key"),
    NexconnError("User count exceeds this appkey subscription"),
    NexconnError("Too many users for these credentials"),
    NexconnError("Too-many users for this app key"),
    NexconnError("app key registration refused", code=429),
])
def test_registration_capacity_with_auth_words_keeps_existing_capacity_path(make_offline_app, error):
    c, fake, monitor, _ = make_offline_app()
    fake.error, fake.fail_method = error, "issue_token"
    for name in ("First", "Second", "Third"):
        owner = start(c, name)
        assert not monitor.offline
        assert owner["chat_mode"] == "mock" and owner["fallback"]["reason"] == "capacity"
        assert c.get("/nexconn/config").json()["mode"] == "live"
    assert c.app.state.playground.registry.counter("live_users_registered") == 0
    fake.error = None
    assert start(c, "Recovered")["chat_mode"] == "live"


def test_non_registration_capacity_with_auth_words_counts_as_unrecognised_failure():
    monitor = UpstreamMonitor()
    error = NexconnError("Message limit exceeded for this app key", status=429)
    monitor.failure(error)
    monitor.failure(error)
    assert not monitor.offline
    monitor.failure(error)
    assert monitor.offline and monitor.snapshot()["reason"] == "unknown"


@pytest.mark.parametrize("setting, value", [("NEXCONN_FALLBACK", "maybe"),
                                          ("NEXCONN_PROBE_EVERY_S", "0"),
                                          ("NEXCONN_PROBE_EVERY_S", "fast")])
def test_invalid_monitor_setting_rejected_at_startup(service, monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    with pytest.raises(ConfigError, match=setting):
        create_app(service)


def test_defaults_and_settings_only_affect_live_mode():
    cfg = NexconnConfig.from_env({})
    assert cfg.fallback == "auto" and cfg.probe_every_s == 60
    cfg = NexconnConfig.from_env({"NEXCONN_MODE": "mock", "NEXCONN_FALLBACK": "force"})
    assert cfg.mode == "mock" and cfg.fallback == "force"


def test_real_wire_calls_observed_and_backup_success_counts_as_success():
    monitor = UpstreamMonitor()
    responses = [httpx.Response(503), httpx.Response(200, json={"code": 0, "token": "fake"})]
    raw = NexconnClient(LIVE, http=httpx.Client(transport=httpx.MockTransport(lambda _: responses.pop(0))))
    client = ObservedNexconn(raw, monitor)
    assert client.issue_token("bot", "bot") == "fake" and not monitor.offline
    raw.http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    for count in (1, 2):
        with pytest.raises(NexconnError):
            client.issue_token("bot", "bot")
        assert monitor.offline is (count == 2)


def test_sdk_probe_counts_one_wire_failure_once(service):
    clock = EpochClock()
    monitor = UpstreamMonitor(clock=clock)
    raw = NexconnClient(LIVE, http=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503))))
    channel = ChatApprovalChannel(service, LIVE, raw, dispatch="inline")
    channel.transport = ChannelTransport(channel, upstream=monitor)
    assert monitor.maybe_probe(sdk_hint=True)
    assert not monitor.offline  # host failover plus the probe callback still represents one failure
    clock.now += 30
    assert monitor.maybe_probe(sdk_hint=True)
    assert monitor.offline and monitor.snapshot()["reason"] == "server"


@pytest.mark.parametrize("failure", [NexconnError("plan expired", status=401), httpx.ConnectTimeout("x")])
def test_upstream_down_start_and_join_use_no_live_calls(make_offline_app, failure):
    c, fake, monitor, _ = make_offline_app(failure=failure)
    # A timeout needs two calls; the bootstrap and this real call provide the observations.
    if not monitor.offline:
        with pytest.raises(NexconnError):
            c.app.state.nexconn.transport.live.issue_token("bot", "bot")
    assert monitor.offline
    fake.calls.clear()
    owner = start(c, "Owner")
    assert owner["chat_mode"] == "mock" and owner["fallback"]["reason"] == "upstream"
    assert owner["fallback"]["active"]
    join = c.post("/try/join", json={"code": owner["invite_url"].rsplit("/", 1)[1], "name": "Teammate"})
    assert join.status_code == 200 and join.json()["chat_mode"] == "mock"
    assert room(c, join.json())[0]["mode"] == "mock"
    state = c.get("/try/state", headers=H(owner)).json()
    assert state["chat_fallback"] and state["fallback"]["upstream"] == "offline"
    assert fake.calls == []


def test_auth_failure_during_start_falls_back_without_502(make_offline_app):
    c, fake, _, _ = make_offline_app()
    fake.error = NexconnError("app-key not enabled", status=403)
    owner = start(c, "Owner")
    assert owner["chat_mode"] == "mock" and owner["fallback"]["reason"] == "upstream"


def test_stand_in_runs_real_approval_payments_daily_limit_and_gift_policy(make_offline_app):
    c, fake, _, _ = make_offline_app(failure=NexconnError("expired", status=401))
    fake.calls.clear()
    owner = start(c, "Owner")
    teammate = c.post("/try/join", json={"code": owner["invite_url"].rsplit("/", 1)[1], "name": "Teammate"}).json()
    session, _ = room(c, teammate)
    tok = {"X-Nexconn-Token": session["token"]}
    ask(c, owner, "Buy a 27-inch 4K monitor")
    state = c.get("/try/state", headers=H(owner)).json()
    aid = state["approvals"][0]["id"]
    messages = c.get("/try/mock/messages", headers=tok).json()["messages"]
    assert any(extra(m).get("approval_id") == aid for m in messages)
    decision = c.post("/try/mock/send", headers=tok, json={"text": f"approve {aid}"}).json()
    assert decision["handled"]["result"] == "AWAITING_PAYER_APPROVAL"
    assert decision["handled"]["decided_by"] == teammate["member"]["user_id"]
    state = c.get("/try/state", headers=H(owner)).json()
    assert state["approvals"][0]["status"] == "APPROVED" and len(state["orders"]) == 1
    pay = state["orders"][0]["pay_url"].replace("mock-checkout", "mock-approve")
    assert c.get(pay).status_code == 200
    assert c.get("/try/state", headers=H(owner)).json()["orders"][0]["status"] == "COMPLETED"
    # Under the per-order cap, but today's spend is over the daily cap: another real approval.
    result = ask(c, owner, "Buy a wireless mouse")
    assert "APR-" in result["final_text"] and not result["pay_links"]
    denied = ask(c, owner, "Buy a $100 gift card")
    assert "refused" in denied["final_text"] and not denied["pay_links"]
    pg = c.app.state.playground
    with pg.use(owner["sandbox_id"]) as sb:
        assert sb.svc.spent_today() == 329
        assert any(r["outcome"] == "DENIED" for r in sb.svc.store.list("decisions"))
        assert sb.channel.status()["inbound"][0]["state"] == "DONE"
    assert fake.calls == []


@pytest.mark.parametrize("trigger", ["state", "session"])
def test_existing_live_pending_card_carried_over_and_claimed_once(make_offline_app, trigger):
    c, fake, monitor, _ = make_offline_app()
    owner = start(c, "Owner")
    assert owner["chat_mode"] == "live"
    ask(c, owner, "Buy a 27-inch 4K monitor")
    aid = c.get("/try/state", headers=H(owner)).json()["approvals"][0]["id"]
    monitor.failure(NexconnError("expired", status=401))
    fake.calls.clear()
    response = c.get("/try/state", headers=H(owner)) if trigger == "state" else c.post("/try/session", headers=H(owner))
    assert response.status_code == 200
    session, messages = room(c, owner)
    cards = [m for m in messages if extra(m).get("grb") == "approval_request"]
    assert len(cards) == 1 and extra(cards[0])["approval_id"] == aid
    assert "Carried over" in json.loads(cards[0]["content"])["content"]
    tok = {"X-Nexconn-Token": session["token"]}
    for _ in range(2):
        assert c.post("/try/mock/send", headers=tok, json={"text": f"approve {aid}"}).status_code == 200
    state = c.get("/try/state", headers=H(owner)).json()
    assert state["approvals"][0]["status"] == "APPROVED" and len(state["orders"]) == 1
    assert c.app.state.playground.registry.get(owner["sandbox_id"])["fallback_reason"] == "upstream"
    assert fake.calls == []


def test_carried_approval_rechecks_hard_rules(make_offline_app):
    c, _, monitor, _ = make_offline_app()
    owner = start(c, "Owner")
    ask(c, owner, "Buy a mechanical keyboard")
    aid = c.get("/try/state", headers=H(owner)).json()["approvals"][0]["id"]
    monitor.failure(NexconnError("expired", status=401))
    session, _ = room(c, owner)
    with c.app.state.playground.use(owner["sandbox_id"]) as sb:
        sb.svc.update_policy({"allowed_merchants": ["Sandbox Furniture Co"],
                              "blocked_merchants": ["Sandbox Electronics"]})
    out = c.post("/try/mock/send", headers={"X-Nexconn-Token": session["token"]},
                 json={"text": f"approve {aid}"}).json()
    assert out["handled"]["result"] == "DENIED"
    state = c.get("/try/state", headers=H(owner)).json()
    assert state["approvals"][0]["status"] == "DENIED_BY_POLICY" and state["orders"] == []


def test_upstream_recovery_new_rooms_live_old_rooms_stay_mock(make_offline_app):
    c, fake, monitor, clock = make_offline_app(failure=NexconnError("expired", status=401))
    old = start(c, "Owner")
    fake.error = None
    clock.now += 60
    fake.calls.clear()
    new = start(c, "New")
    assert new["chat_mode"] == "live" and fake.calls and not monitor.offline
    state = c.get("/try/state", headers=H(old)).json()
    assert state["chat_mode"] == "mock" and state["fallback"]["upstream"] == "live"
    assert state["fallback"]["since"] == old["fallback"]["since"]
    assert room(c, old)[0]["mode"] == "mock"


def test_session_failure_itself_migrates_existing_live_room(make_offline_app):
    c, fake, monitor, _ = make_offline_app()
    owner = start(c, "Owner")
    ask(c, owner, "Buy a mechanical keyboard")
    fake.fail_method, fake.error = "issue_token", NexconnError("expired", status=401)
    response = c.post("/try/session", headers=H(owner))
    assert response.status_code == 200 and response.json()["mode"] == "mock" and monitor.offline
    assert c.get("/try/state", headers=H(owner)).json()["fallback"]["reason"] == "upstream"


def test_failed_join_during_busy_fallback_removes_member_and_frees_seat(make_offline_app, monkeypatch):
    monkeypatch.setenv("TRY_MAX_MEMBERS", "2")
    c, fake, monitor, _ = make_offline_app()
    owner = start(c, "Owner")
    pg, sid = c.app.state.playground, owner["sandbox_id"]
    seats = pg.registry.counter("live_users_registered")
    fake.fail_method, fake.error = "issue_token", NexconnError("expired", status=401)
    body = {"code": owner["invite_url"].rsplit("/", 1)[1], "name": "Teammate"}
    with pg.use(sid):  # an owner request is still using the sandbox when registration fails
        response = c.post("/try/join", json=body)
        assert response.status_code == 409 and monitor.offline
        assert pg.registry.member_count(sid) == 1
        assert pg.registry.counter("live_users_registered") == seats
    retry = c.post("/try/join", json=body)
    assert retry.status_code == 200 and retry.json()["chat_mode"] == "mock"
    assert pg.registry.member_count(sid) == 2
    assert c.post("/try/session", headers=H(retry.json())).status_code == 200


def test_join_failure_after_registration_removes_member_and_frees_seat(make_offline_app, monkeypatch):
    from app.playground.manager import PlaygroundError

    monkeypatch.setenv("TRY_MAX_MEMBERS", "2")
    c, _, _, _ = make_offline_app()
    owner = start(c, "Owner")
    pg = c.app.state.playground
    seats = pg.registry.counter("live_users_registered")
    body = {"code": owner["invite_url"].rsplit("/", 1)[1]}

    def fail(*args):
        raise PlaygroundError(409, "busy")

    with monkeypatch.context() as patch:
        patch.setattr(pg.registry, "touch", fail)
        assert c.post("/try/join", json=body).status_code == 409
    assert pg.registry.member_count(owner["sandbox_id"]) == 1
    assert pg.registry.counter("live_users_registered") == seats
    assert c.post("/try/join", json=body).status_code == 200


def test_fallback_cache_reloads_preserve_member_tokens(make_offline_app, monkeypatch):
    monkeypatch.setenv("TRY_CACHE_SIZE", "1")
    c, fake, _, _ = make_offline_app(fallback="force")
    owner = start(c, "Owner")
    teammate = c.post("/try/join", json={"code": owner["invite_url"].rsplit("/", 1)[1]}).json()
    sessions = [room(c, member)[0] for member in (owner, teammate)]
    other = start(c, "Other")
    pg, issued = c.app.state.playground, []
    original = pg.mock.issue_token

    def issue_token(uid, name):
        issued.append(uid)
        return original(uid, name)

    monkeypatch.setattr(pg.mock, "issue_token", issue_token)
    for _ in range(pg.mock.TOKENS_PER_USER + 1):
        for member in (owner, other):
            assert c.get("/try/state", headers=H(member)).status_code == 200
            assert list(pg._cache) == [member["sandbox_id"]]
    for session in sessions:
        token = {"X-Nexconn-Token": session["token"]}
        assert c.get("/try/mock/messages", headers=token).status_code == 200
        assert c.post("/try/mock/send", headers=token, json={"text": "still connected"}).status_code == 200
    assert issued and set(issued) == {pg.base_cfg.bot_user_id}
    assert fake.calls == []


def test_carried_pending_card_does_not_wait_for_an_abandoned_send_claim(make_offline_app):
    c, _, monitor, _ = make_offline_app()
    owner = start(c, "Owner")
    ask(c, owner, "Buy a mechanical keyboard")
    pg = c.app.state.playground
    with pg.use(owner["sandbox_id"]) as sb:
        with sb.svc.store.tx() as conn:
            conn.execute("UPDATE nexconn_posts SET status='SENDING', claimed_at=? WHERE kind='request'",
                         (sb.channel._clock(),))
    monitor.failure(NexconnError("expired", status=401))
    _, messages = room(c, owner)
    assert any(extra(m).get("grb") == "approval_request" for m in messages)


def test_fallback_metadata_and_pending_cards_survive_registry_restart(make_offline_app):
    from app.playground.manager import PlaygroundManager

    c, _, monitor, clock = make_offline_app()
    owner = start(c, "Owner")
    ask(c, owner, "Buy a mechanical keyboard")
    monitor.failure(NexconnError("expired", status=401))
    before = c.get("/try/state", headers=H(owner)).json()
    pg = c.app.state.playground
    pg.close()
    clock.now += 60  # upstream recovers, but the registry's fallback is permanent
    restarted = PlaygroundManager(pg.cfg, pg.base_cfg, live_client=pg.live_client,
                                   upstream=monitor, policy=pg.policy, catalog=pg.catalog)
    try:
        after = restarted.state(owner["secret"])
        assert after["chat_mode"] == "mock" and after["fallback"]["upstream"] == "live"
        assert after["fallback"]["since"] == before["fallback"]["since"]
        assert after["fallback"]["detail"] == "auth"
        session = restarted.session(owner["secret"])
        history = restarted.mock_messages(session["token"], 0)["messages"]
        assert any(extra(m).get("approval_id") == before["approvals"][0]["id"] for m in history)
        decision = restarted.mock_send(session["token"], f"reject {before['approvals'][0]['id']}", None)
        assert decision["handled"]["result"] == "REJECTED"
    finally:
        restarted.close()


def test_capacity_fallback_metadata_is_distinct_and_legacy_rows_default_sensibly(make_offline_app, monkeypatch):
    monkeypatch.setenv("TRY_MAX_LIVE_USERS", "0")
    c, fake, monitor, _ = make_offline_app()
    fake.calls.clear()
    owner = start(c, "Owner")
    assert owner["fallback"]["reason"] == "capacity" and not monitor.offline
    pg = c.app.state.playground
    with pg.registry.tx() as conn:
        conn.execute("UPDATE sandboxes SET fallback_reason=NULL WHERE id=?", (owner["sandbox_id"],))
    assert c.get("/try/state", headers=H(owner)).json()["fallback"]["reason"] == "capacity"
    assert fake.calls == []


def test_chat_fallback_keeps_approver_auth_and_decides_real_pending_approval(make_offline_app, service):
    c, fake, monitor, clock = make_offline_app()
    aid = service.request_approval("P-1003", 1, "approval in the main room")["approval_id"]
    monitor.failure(NexconnError("credentials expired", status=401))
    fake.calls.clear()
    cfg = c.get("/nexconn/config").json()
    assert cfg["mode"] == "mock" and cfg["configured_mode"] == "live" and cfg["fallback"]["active"]
    assert cfg["fallback"]["reason"] == "auth" and cfg["app_key"] == ""
    assert c.post("/nexconn/session").status_code == 401
    assert c.post("/nexconn/session?as_user=stranger", headers={"X-Approver-Token": TOKEN}).status_code == 403
    assert c.get("/nexconn/mock/messages").status_code == 401
    sess = c.post("/nexconn/session", headers={"X-Approver-Token": TOKEN}).json()
    tok = {"X-Nexconn-Token": sess["token"]}
    messages = c.get("/nexconn/mock/messages", headers=tok).json()["messages"]
    assert any(extra(m).get("approval_id") == aid for m in messages)
    out = c.post("/nexconn/mock/send", headers=tok, json={"text": f"approve {aid}"}).json()
    assert out["handled"]["result"] == "ORDER_PLACED"
    assert service.store.get("approvals", aid)["status"] == "APPROVED"
    assert len(service.store.list("orders")) == 1 and fake.calls == []
    clock.now += 60
    assert c.get("/nexconn/config").json()["mode"] == "live"
    assert not monitor.offline and c.app.state.nexconn.ready
    fake.calls.clear()
    service.request_approval("P-1003", 1, "new live card")
    assert any(method == "send_group_text" for method, _ in fake.calls)
    assert c.get("/nexconn/mock/messages", headers=tok).status_code == 404


def test_failed_main_posts_retried_on_stand_in(make_offline_app, service):
    c, fake, monitor, _ = make_offline_app()
    fake.fail_method, fake.error = "send_group_text", NexconnError("unavailable", status=503)
    first = service.request_approval("P-1003", 1, "first")["approval_id"]
    assert not monitor.offline
    second = service.request_approval("P-1004", 1, "second")["approval_id"]
    assert monitor.offline
    cfg = c.get("/nexconn/config").json()
    assert cfg["mode"] == "mock"
    ch = c.app.state.nexconn
    assert all(p["status"] == "SENT" for p in ch.status()["posts"])
    assert {extra(m).get("approval_id") for m in ch.client.history(ch.cfg.group_id)} == {first, second}


def test_open_demo_cannot_issue_approver_identity_even_in_force_mode(make_offline_app):
    c, fake, _, _ = make_offline_app(fallback="force", approver_token="", demo=True)
    assert c.get("/nexconn/config").json()["mode"] == "mock"
    assert c.post("/nexconn/session").status_code == 503
    assert c.post("/nexconn/relay", json={"message_id": "m", "user_id": "alice", "text": "approve APR-12345678"}).status_code == 503
    assert fake.calls == []


def test_signed_webhook_remains_available_during_fallback(make_offline_app, service):
    c, _, _, _ = make_offline_app(failure=NexconnError("expired", status=401))
    aid = service.request_approval("P-1003", 1, "main")["approval_id"]
    headers, body = signed(decision_event(aid))
    r = c.post("/nexconn/webhook", headers=headers, content=body)
    assert r.status_code == 200 and r.json()["handled"][0]["result"] == "ORDER_PLACED"
    assert c.post("/nexconn/webhook", content=body).status_code == 401


def test_health_stays_ready_while_serving_offline_fallback(make_offline_app):
    c, _, _, _ = make_offline_app(failure=NexconnError("private secret expired", status=403))
    h = c.get("/health").json()
    assert h["nexconn"]["upstream"]["state"] == "offline"
    assert h["nexconn"]["upstream"]["reason"] == "auth"
    assert h["nexconn"]["upstream"]["fallback"] == "auto"
    assert h["nexconn"]["effective_mode"] == "mock" and h["playground"]["new_sandboxes"] == "mock"
    assert "private secret" not in json.dumps(h)
    r = c.get("/health/ready")
    assert r.status_code == 200 and r.json()["checks"]["nexconn"]
    assert r.json()["nexconn_mode"] == "offline-fallback"


def test_force_and_off_in_application(make_offline_app):
    c, fake, _, _ = make_offline_app(fallback="force")
    assert start(c)["fallback"]["reason"] == "upstream" and fake.calls == []
    c2, _, monitor, _ = make_offline_app(fallback="off", failure=NexconnError("expired", status=401))
    assert not monitor.offline
    assert c2.get("/nexconn/config").json()["mode"] == "live"
    assert c2.post("/try/start", json={}).status_code == 502
    assert c2.get("/health/ready").status_code == 503


def test_sdk_hint_is_globally_limited_and_cannot_assert_an_outage(make_offline_app):
    c, fake, monitor, clock = make_offline_app()
    fake.calls.clear()
    assert c.post("/nexconn/probe", json={"status": "expired"}).json()["probed"]
    for _ in range(5):
        assert not c.post("/nexconn/probe").json()["probed"]
    assert len(fake.calls) == 1 and not monitor.offline
    clock.now += 30
    fake.error = NexconnError("expired", status=401)
    assert c.post("/nexconn/probe").json()["mode"] == "mock"
    assert monitor.offline


def test_sdk_failed_only_downgrades_authenticated_members_own_sandbox(make_offline_app):
    c, fake, monitor, _ = make_offline_app()
    a, b = start(c, "A"), start(c, "B")
    assert c.post("/try/sdk-failed").status_code == 401
    assert c.post("/try/sdk-failed", headers={"X-Try-Session": "invalid"}).status_code == 401
    teammate = c.post("/try/join", json={"code": a["invite_url"].rsplit("/", 1)[1]}).json()
    r = c.post("/try/sdk-failed", headers=H(teammate))
    assert r.status_code == 200 and r.json()["chat_mode"] == "mock"
    assert c.get("/try/state", headers=H(a)).json()["chat_mode"] == "mock"
    assert c.get("/try/state", headers=H(b)).json()["chat_mode"] == "live"
    assert not monitor.offline and c.get("/nexconn/config").json()["mode"] == "live"
    assert c.post("/try/sdk-failed", headers=H(a)).status_code == 429


def test_old_registry_migrates_idempotently_with_existing_rows(tmp_path):
    path = tmp_path / "registry.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sandboxes (id TEXT PRIMARY KEY, group_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL, "
                 "chat_mode TEXT NOT NULL, invite_hash TEXT NOT NULL UNIQUE, status TEXT NOT NULL, "
                 "created_at REAL NOT NULL, last_seen REAL NOT NULL, room_joined REAL, "
                 "group_ready INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO sandboxes VALUES ('old', 'grbtryold', 'Old', 'live', 'hash', 'ACTIVE', 1, 1, NULL, 1)")
    conn.commit()
    conn.close()
    for _ in range(2):
        reg = Registry(path)
        old = reg.get("old")
        assert old["chat_mode"] == "live" and old["fallback_reason"] is None
        assert old["fallback_since"] is None and old["fallback_detail"] is None
        reg.close()


@pytest.mark.parametrize("body", [{"code": 0, "result": {}}, {"code": 200, "result": None},
                                  {"code": 0, "result": {"accessToken": ""}}, {"code": 0, "result": "x"}])
def test_unusable_token_answers_count_as_failures_not_successes(body):
    """A 200 without a usable token is not a success: three in a row switch to the stand-in."""
    client = NexconnClient(LIVE, http=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=body))))
    client.upstream = monitor = UpstreamMonitor()
    for i in range(3):
        assert not monitor.offline
        with pytest.raises(NexconnError, match="no accessToken"):
            client.issue_token(f"u{i}", "Visitor")
    assert monitor.offline and monitor.snapshot()["reason"] == "unknown"


def test_valid_token_answer_still_counts_as_success_and_resets_the_streak():
    answers = iter([{"code": 0, "result": {}}, {"code": 0, "result": {}}, {"code": 0, "result": {"accessToken": "T"}},
                    {"code": 0, "result": {}}, {"code": 0, "result": {}}])
    client = NexconnClient(LIVE, http=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=next(answers)))))
    client.upstream = monitor = UpstreamMonitor()
    outcomes = []
    for i in range(5):
        try:
            outcomes.append(client.issue_token(f"u{i}", "Visitor"))
        except NexconnError:
            outcomes.append(None)
    assert outcomes == [None, None, "T", None, None] and not monitor.offline
