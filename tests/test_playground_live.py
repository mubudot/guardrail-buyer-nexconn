"""The /try playground in NEXCONN_MODE=live against the fake Nexconn server API (no network):
visitors are registered with their display name, each sandbox gets its own group, signed webhooks
for a playground room decide in that sandbox only, the main room is unchanged, unknown rooms are
ignored; the live-seat cap and capacity errors fall back to the offline stand-in; an outage
answers 502 and leaves nothing behind. Also /health/live, /health/ready and the 500 handler."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.nexconn import ChatApprovalChannel, NexconnClient
from app.nexconn.client import text_content
from tests.test_nexconn_api import LIVE, TOKEN, FakeNexconnServer, signed

NO_LIMITS = {"TRY_CREATE_LIMIT": "off", "TRY_JOIN_LIMIT": "off", "TRY_AGENT_LIMIT": "off", "TRY_AGENT_LIMIT_IP": "off"}


class Fake(FakeNexconnServer):
    """The fake server, plus: per-path failures and a record of issued users."""

    def __init__(self):
        super().__init__()
        self.fail: dict[str, httpx.Response] = {}

    def __call__(self, request):
        r = super().__call__(request)
        return self.fail.get(request.url.path, r)


def live_try(service, monkeypatch, tmp_path, **env):
    monkeypatch.setenv("TRY_MODE", "on")
    monkeypatch.setenv("TRY_DIR", str(tmp_path / "try"))
    for k, v in {**NO_LIMITS, **env}.items():
        monkeypatch.setenv(k, v)
    fake = Fake()
    client = NexconnClient(LIVE, http=httpx.Client(transport=httpx.MockTransport(fake)))
    ch = ChatApprovalChannel(service, LIVE, client, dispatch="inline")
    app = create_app(service, approver_token=TOKEN, agent_rate_limit=None, tools_rate_limit=None, nexconn=ch)
    ch.bootstrap()
    return TestClient(app), fake


def event(group, user, text, mid):
    return {"type": "message:send", "id": "evt", "time": 1, "data": [{
        "userId": user, "channelId": group, "channelType": 3, "messageType": "RC:TxtMsg",
        "content": text_content(text), "time": 1, "messageId": mid, "os": "Websocket"}]}


def test_live_sandbox_registers_the_visitor_and_its_own_group(service, monkeypatch, tmp_path):
    c, fake = live_try(service, monkeypatch, tmp_path)
    fake.calls.clear()
    s = c.post("/try/start", json={"name": "Ada"}).json()
    uid, gid = s["member"]["user_id"], f"grbtry{s['sandbox_id']}"
    assert s["chat_mode"] == "live"
    issued = [b for _, p, b in fake.calls if p == "/v4/auth/access-token/issue"]
    assert {"userId": uid, "name": "Ada"} in issued
    create = next(b for _, p, b in fake.calls if p == "/v4/group-channel/create")
    assert create["channelId"] == gid and create["owner"] == "grbagent" and create["userIds"] == [uid]
    welcome = [b for _, p, b in fake.calls if p == "/v4/group-channel/message/send"]
    assert welcome and welcome[-1]["toChannelIds"] == [gid]
    sess = c.post("/try/session", headers={"X-Try-Session": s["secret"]}).json()
    assert sess == {**sess, "mode": "live", "app_key": "KEY", "token": f"tok-{uid}", "group_id": gid}
    assert "SECRET" not in json.dumps(sess)
    h = c.get("/health").json()["playground"]
    assert h == {"mode": "on", "active_sandboxes": 1, "live_seats_left": 499, "chat_fallback": False}


def test_signed_webhook_routes_playground_rooms_to_their_sandbox(service, monkeypatch, tmp_path):
    c, fake = live_try(service, monkeypatch, tmp_path)
    s = c.post("/try/start", json={"name": "Ada"}).json()
    hs = {"X-Try-Session": s["secret"]}
    uid, gid = s["member"]["user_id"], f"grbtry{s['sandbox_id']}"
    c.post("/try/agent", headers=hs, json={"request": "Buy a 27-inch 4K monitor"})
    aid = c.get("/try/state", headers=hs).json()["approvals"][0]["id"]
    card = [b for _, p, b in fake.calls if p == "/v4/group-channel/message/send"][-1]
    assert card["toChannelIds"] == [gid] and json.loads(json.loads(card["content"])["extra"])["approval_id"] == aid

    main_aid = service.request_approval("P-1003", 1, "x")["approval_id"]
    # a stranger (e.g. the judges' approver) cannot decide in the sandbox room
    h, body = signed(event(gid, "alice", f"approve {aid}", "W-0"), nonce="n0")
    assert c.post("/nexconn/webhook", headers=h, content=body).json()["handled"][0]["result"] == "NOT_AN_APPROVER"
    # the sandbox owner decides in their room
    h, body = signed(event(gid, uid, f"approve {aid}", "W-1"), nonce="n1")
    r = c.post("/nexconn/webhook", headers=h, content=body)
    assert r.status_code == 200 and r.json()["handled"][0]["result"] == "AWAITING_PAYER_APPROVAL"
    assert c.get("/try/state", headers=hs).json()["approvals"][0]["status"] == "APPROVED"
    assert service.store.get("approvals", main_aid)["status"] == "PENDING"       # main data untouched
    # the visitor cannot decide a main-room approval from their room either (unknown there)
    h, body = signed(event(gid, uid, f"approve {main_aid}", "W-2"), nonce="n2")
    assert c.post("/nexconn/webhook", headers=h, content=body).json()["handled"][0]["result"] == "UNKNOWN_APPROVAL"
    # main-room behaviour unchanged
    h, body = signed(event("grbapprovals", "alice", f"approve {main_aid}", "W-3"), nonce="n3")
    assert c.post("/nexconn/webhook", headers=h, content=body).json()["handled"][0]["result"] == "ORDER_PLACED"
    # unknown playground group, and an unrelated group: ignored
    h, body = signed(event("grbtryffffffffffff", uid, f"approve {aid}", "W-4"), nonce="n4")
    assert c.post("/nexconn/webhook", headers=h, content=body).json()["handled"][0]["result"] == "IGNORED"
    h, body = signed(event("someothergroup", uid, f"approve {aid}", "W-5"), nonce="n5")
    assert c.post("/nexconn/webhook", headers=h, content=body).json()["handled"][0]["result"] == "IGNORED"
    # a retried delivery of the same message is de-duplicated in the sandbox's own inbound table
    h, body = signed(event(gid, uid, f"approve {aid}", "W-1"), nonce="n6")
    assert c.post("/nexconn/webhook", headers=h, content=body).json()["handled"][0]["result"] == "DUPLICATE"
    assert len(c.get("/try/state", headers=hs).json()["orders"]) == 1


def test_teammate_is_registered_and_added_to_the_group(service, monkeypatch, tmp_path):
    c, fake = live_try(service, monkeypatch, tmp_path)
    s = c.post("/try/start", json={"name": "Ada"}).json()
    fake.calls.clear()
    t = c.post("/try/join", json={"code": s["invite_url"].rsplit("/", 1)[1], "name": "Bob"}).json()
    paths = [(p, b) for _, p, b in fake.calls]
    assert ("/v4/auth/access-token/issue", {"userId": t["member"]["user_id"], "name": "Bob"}) in paths
    assert ("/v4/group-channel/join", {"channelId": f"grbtry{s['sandbox_id']}", "userIds": [t["member"]["user_id"]]}) in paths


def test_live_seat_cap_falls_back_to_the_offline_stand_in(service, monkeypatch, tmp_path):
    c, fake = live_try(service, monkeypatch, tmp_path, TRY_MAX_LIVE_USERS="1")
    assert c.post("/try/start", json={}).json()["chat_mode"] == "live"
    fake.calls.clear()
    s2 = c.post("/try/start", json={}).json()
    assert s2["chat_mode"] == "mock" and fake.calls == []                 # no Nexconn call at all
    sess = c.post("/try/session", headers={"X-Try-Session": s2["secret"]}).json()
    assert sess["mode"] == "mock" and sess["app_key"] == ""
    assert c.get("/try/state", headers={"X-Try-Session": s2["secret"]}).json()["chat_fallback"] is True
    assert c.get("/try/mock/messages", headers={"X-Nexconn-Token": sess["token"]}).status_code == 200
    assert c.get("/health").json()["playground"]["chat_fallback"] is True
    t = c.post("/try/join", json={"code": c.post("/try/start", json={}).json()["invite_url"].rsplit("/", 1)[1]})
    assert t.status_code == 200 and t.json()["chat_mode"] == "mock"


def test_capacity_error_falls_back_and_outage_answers_502_without_leftovers(service, monkeypatch, tmp_path):
    c, fake = live_try(service, monkeypatch, tmp_path)
    pg = c.app.state.playground
    fake.fail["/v4/auth/access-token/issue"] = httpx.Response(200, json={"code": 1008, "msg": "user quota exceeded"})
    s = c.post("/try/start", json={}).json()
    assert s["chat_mode"] == "mock" and pg.registry.counter("live_users_registered") == 0
    fake.fail["/v4/auth/access-token/issue"] = httpx.Response(503)
    r = c.post("/try/start", json={})
    assert r.status_code == 502 and "Nexconn" in r.json()["detail"]
    assert pg.registry.count_active() == 1 and pg.registry.counter("live_users_registered") == 0


# ---- stability --------------------------------------------------------------------------------
def test_health_live_and_ready(service, monkeypatch, tmp_path):
    c, _ = live_try(service, monkeypatch, tmp_path)
    assert c.get("/health/live").json() == {"ok": True}
    r = c.get("/health/ready")
    assert r.status_code == 200 and r.json()["checks"] == {"database": True, "nexconn": True, "playground": True}
    c.app.state.nexconn.ready = False
    r = c.get("/health/ready")
    assert r.status_code == 503 and r.json()["failed"] == ["nexconn"]
    c.app.state.playground.registry.close()
    r = c.get("/health/ready")
    assert r.status_code == 503 and r.json()["failed"] == ["nexconn", "playground"]


def test_ready_without_chat_or_playground(service):
    c = TestClient(create_app(service, approver_token=TOKEN, agent_rate_limit=None, tools_rate_limit=None))
    assert c.get("/health/ready").json() == {"ok": True, "checks": {"database": True}}
    service.store.conn.close()
    r = c.get("/health/ready")
    assert r.status_code == 503 and r.json()["failed"] == ["database"]


def test_unexpected_errors_hide_internals_and_name_a_reference(service, monkeypatch, caplog):
    app = create_app(service, approver_token=TOKEN, agent_rate_limit=None, tools_rate_limit=None)

    def boom(*a, **k):
        raise RuntimeError("secret internal detail /var/lib/x")
    monkeypatch.setattr(service, "spend_summary", boom)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/spend")
    assert r.status_code == 500 and "secret" not in r.text
    detail = r.json()["detail"]
    assert detail.startswith("Something went wrong on our side; nothing was bought or approved. Reference ")
    ref = detail.rsplit(" ", 1)[1]
    assert len(ref) == 8 and any(ref in rec.getMessage() for rec in caplog.records)
    assert c.get("/orders/nope").status_code == 404                      # HTTPException unchanged
    assert c.get("/spend/history?days=x").status_code == 422             # validation unchanged


@pytest.mark.parametrize("path", ["/try", "/try/state", "/try/mock/messages"])
def test_main_live_without_try_mode_has_no_playground(service, path):
    fake = Fake()
    client = NexconnClient(LIVE, http=httpx.Client(transport=httpx.MockTransport(fake)))
    ch = ChatApprovalChannel(service, LIVE, client, dispatch="inline")
    c = TestClient(create_app(service, approver_token=TOKEN, agent_rate_limit=None, tools_rate_limit=None, nexconn=ch))
    assert c.get(path).status_code == 404


def test_failed_teammate_registration_leaves_no_member_and_frees_the_seat(service, monkeypatch, tmp_path):
    c, fake = live_try(service, monkeypatch, tmp_path)
    pg = c.app.state.playground
    s = c.post("/try/start", json={}).json()
    seats = pg.registry.counter("live_users_registered")
    fake.fail["/v4/auth/access-token/issue"] = httpx.Response(503)
    r = c.post("/try/join", json={"code": s["invite_url"].rsplit("/", 1)[1]})
    assert r.status_code == 502
    assert pg.registry.member_count(s["sandbox_id"]) == 1 and pg.registry.counter("live_users_registered") == seats


def test_main_group_cannot_use_the_playground_prefix(service, monkeypatch, tmp_path):
    from app.main import ConfigError
    monkeypatch.setenv("NEXCONN_MODE", "mock")
    monkeypatch.setenv("NEXCONN_GROUP_ID", "grbtryroom")
    monkeypatch.setenv("TRY_MODE", "on")
    monkeypatch.setenv("TRY_DIR", str(tmp_path / "try"))
    with pytest.raises(ConfigError, match="grbtry"):
        create_app(service, approver_token=TOKEN)
