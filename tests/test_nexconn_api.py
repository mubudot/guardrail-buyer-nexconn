"""Nexconn add-on over HTTP: config/startup, the mock room end to end, the signed webhook
(live mode, with a fake Nexconn server), the relay switch, and the live server-API client
(request signing, data-center failover, error codes). No network."""
import hashlib
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import ConfigError, create_app
from app.nexconn import ChatApprovalChannel, MockNexconn, NexconnClient, NexconnConfig, NexconnError
from app.nexconn.client import sign, text_content
from app.nexconn.config import NexconnConfigError

TOKEN = "s3cret"


def make(service, monkeypatch, mode="mock", **env):
    monkeypatch.setenv("NEXCONN_MODE", mode)
    monkeypatch.setenv("NEXCONN_APPROVER_IDS", "alice,bob")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return TestClient(create_app(service, approver_token=TOKEN, agent_rate_limit=None, tools_rate_limit=None))


# ---- off by default ------------------------------------------------------------------
def test_off_by_default_changes_nothing(service):
    c = TestClient(create_app(service, approver_token=TOKEN, agent_rate_limit=None, tools_rate_limit=None))
    assert c.get("/health").json()["nexconn"] == {"mode": "off"}
    assert c.get("/nexconn/config").status_code == 404
    assert c.get("/chat").status_code == 404


@pytest.mark.parametrize("env, msg", [
    ({"NEXCONN_MODE": "maybe"}, "NEXCONN_MODE"),
    ({"NEXCONN_MODE": "live"}, "NEXCONN_APP_KEY"),
    ({"NEXCONN_MODE": "mock", "NEXCONN_AREA": "XX"}, "NEXCONN_AREA"),
    ({"NEXCONN_MODE": "mock", "NEXCONN_GROUP_ID": "bad id!"}, "NEXCONN_GROUP_ID"),
    ({"NEXCONN_MODE": "mock", "NEXCONN_APPROVER_IDS": "grbagent"}, "bot user"),
    ({"NEXCONN_MODE": "mock", "NEXCONN_SDK_BASE": "http://evil.example"}, "NEXCONN_SDK_BASE"),
])
def test_bad_config_fails_fast(service, monkeypatch, env, msg):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ConfigError, match=msg):
        create_app(service, approver_token=TOKEN)


def test_public_config_never_leaks_secret():
    cfg = NexconnConfig.from_env({"NEXCONN_MODE": "live", "NEXCONN_APP_KEY": "k", "NEXCONN_APP_SECRET": "SECRET"})
    assert "SECRET" not in json.dumps(cfg.public()) and "SECRET" not in repr(cfg)
    with pytest.raises(NexconnConfigError):
        NexconnConfig.from_env({"NEXCONN_MODE": "mock", "NEXCONN_WEBHOOK_MAX_SKEW_S": "-1"})


# ---- mock room end to end ----------------------------------------------------------------
def test_mock_room_end_to_end(service, monkeypatch):
    c = make(service, monkeypatch)
    h = c.get("/health").json()["nexconn"]
    assert h["mode"] == "mock" and h["ready"] and h["approver_ids"] == ["alice", "bob"]
    assert c.get("/chat").status_code == 200
    assert c.post("/nexconn/session").status_code == 401                       # approver token required
    assert c.post("/nexconn/session?as_user=mallory", headers={"X-Approver-Token": TOKEN}).status_code == 403
    s = c.post("/nexconn/session?as_user=alice", headers={"X-Approver-Token": TOKEN}).json()
    assert s["user_id"] == "alice" and s["mode"] == "mock" and s["app_key"] == ""
    hdr = {"X-Nexconn-Token": s["token"]}
    assert c.get("/nexconn/mock/messages").status_code == 401

    a = c.post("/tools/request_approval", json={"arguments": {"product_id": "P-1003", "quantity": 1,
                                                              "justification": "new hire"}}).json()
    aid = a["result"]["approval_id"] if "result" in a and isinstance(a["result"], dict) else a["approval_id"]
    c.app.state.nexconn.drain(5)
    msgs = c.get("/nexconn/mock/messages", headers=hdr).json()
    card = msgs["messages"][-1]
    assert json.loads(json.loads(card["content"])["extra"])["approval_id"] == aid

    out = c.post("/nexconn/mock/send", headers=hdr, json={
        "text": f"approve {aid}", "extra": {"grb": "decision", "approval_id": aid, "decision": "approve",
                                            "evil": "x" * 10}}).json()
    assert out["handled"]["result"] == "ORDER_PLACED" and out["handled"]["decided_by"] == "alice"
    assert c.get("/approvals").json()[0]["status"] == "APPROVED"
    c.app.state.nexconn.drain(5)
    later = c.get(f"/nexconn/mock/messages?after={msgs['next']}", headers=hdr).json()["messages"]
    assert "evil" not in later[0]["content"]                       # only the structured decision is relayed
    assert json.loads(json.loads(later[-1]["content"])["extra"])["grb"] == "outcome"
    st = c.get("/nexconn/status", headers={"X-Approver-Token": TOKEN}).json()
    assert st["inbound"][0]["outcome"] == "ORDER_PLACED" and all(p["status"] == "SENT" for p in st["posts"])
    assert c.post("/nexconn/webhook", content=b"{}").status_code == 404        # no webhook in mock mode


def test_sdk_redirect_is_pinned(service, monkeypatch):
    c = make(service, monkeypatch)
    r = c.get("/nexconn/sdk/chat.mjs", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == \
        "https://cdn.jsdelivr.net/npm/@nexconn/chat@26.2.1/index.mjs"
    assert c.get("/nexconn/sdk/other.mjs", follow_redirects=False).status_code == 404


# ---- live mode: fake Nexconn server API + signed webhooks -------------------------------
class FakeNexconnServer:
    """httpx transport standing in for api.sg-light-api.com; checks every request's signature."""

    def __init__(self, secret="SECRET"):
        self.secret, self.calls, self.fail_primary = secret, [], False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        h = request.headers
        assert h["App-Key"] == "KEY" and len(h["Nonce"]) <= 18
        assert h["Signature"] == hashlib.sha1(f"{self.secret}{h['Nonce']}{h['Timestamp']}".encode()).hexdigest()
        body = json.loads(request.content)
        self.calls.append((request.url.host, request.url.path, body))
        if self.fail_primary and request.url.host == "api.sg-light-api.com":
            return httpx.Response(503)
        if request.url.path == "/v4/auth/access-token/issue":
            return httpx.Response(200, json={"code": 0, "result": {"userId": body["userId"],
                                                                  "accessToken": f"tok-{body['userId']}"}})
        if request.url.path == "/v4/group-channel/create":
            return httpx.Response(200, json={"code": 25409, "msg": "exists"})
        if request.url.path == "/v4/group-channel/join":
            return httpx.Response(200, json={"code": 0})
        if request.url.path == "/v4/group-channel/message/send":
            return httpx.Response(200, json={"code": 0, "result": {"messageIds": [
                {"channelId": body["toChannelIds"][0], "messageId": f"SRV-{len(self.calls)}"}]}})
        return httpx.Response(404)


LIVE = NexconnConfig(mode="live", app_key="KEY", app_secret="SECRET", approver_ids=("alice",))


def live_app(service, fake, relay=False, monkeypatch=None):
    client = NexconnClient(LIVE, http=httpx.Client(transport=httpx.MockTransport(fake)))
    ch = ChatApprovalChannel(service, LIVE, client, dispatch="inline")
    if relay:
        monkeypatch.setenv("NEXCONN_RELAY", "1")
    app = create_app(service, approver_token=TOKEN, agent_rate_limit=None, tools_rate_limit=None, nexconn=ch)
    ch.bootstrap()
    return TestClient(app), ch


def signed(body: dict, secret="SECRET", nonce="n1", ts=None, key="KEY"):
    ts = str(ts if ts is not None else int(time.time() * 1000))
    return {"AppKey": key, "Nonce": nonce, "Timestamp": ts, "Signature": sign(secret, nonce, ts),
            "Content-Type": "application/json"}, json.dumps(body).encode()


def decision_event(aid, user="alice", mid="WS-1", text=None):
    return {"type": "message:send", "id": "evt-1", "time": 1, "data": [{
        "userId": user, "channelId": "grbapprovals", "channelType": 3, "messageType": "RC:TxtMsg",
        "content": text_content(text or f"approve {aid}"), "time": 1, "messageId": mid, "os": "Websocket"}]}


def test_live_bootstrap_and_card_use_signed_server_api(service):
    fake = FakeNexconnServer()
    c, ch = live_app(service, fake)
    assert ch.ready
    paths = [p for _, p, _ in fake.calls]
    assert paths[:2] == ["/v4/auth/access-token/issue"] * 2
    assert "/v4/group-channel/create" in paths and "/v4/group-channel/join" in paths   # 25409 -> join
    aid = service.request_approval("P-1003", 1, "x")["approval_id"]
    host, path, body = fake.calls[-1]
    assert path == "/v4/group-channel/message/send" and body["fromUserId"] == "grbagent"
    assert body["toChannelIds"] == ["grbapprovals"] and body["messageType"] == "RC:TxtMsg"
    assert json.loads(json.loads(body["content"])["extra"])["approval_id"] == aid
    s = c.post("/nexconn/session", headers={"X-Approver-Token": TOKEN}).json()
    assert s == {**s, "mode": "live", "app_key": "KEY", "token": "tok-alice", "area": "SG"}
    assert "SECRET" not in json.dumps(s) and "SECRET" not in c.get("/nexconn/config").text


def test_live_failover_to_backup_host(service):
    fake = FakeNexconnServer()
    fake.fail_primary = True
    c, ch = live_app(service, fake)
    assert ch.ready and fake.calls[-1][0] == "api-b.sg-light-api.com"
    fake.fail_primary = False
    service.request_approval("P-1003", 1, "x")
    assert fake.calls[-1][0] == "api-b.sg-light-api.com"       # sticks to the host that answered


def test_signed_webhook_approves(service):
    c, ch = live_app(service, FakeNexconnServer())
    aid = service.request_approval("P-1003", 1, "x")["approval_id"]
    h, body = signed(decision_event(aid))
    r = c.post("/nexconn/webhook", headers=h, content=body)
    assert r.status_code == 200 and r.json()["handled"][0]["result"] == "ORDER_PLACED"
    assert service.store.get("approvals", aid)["status"] == "APPROVED"
    again = c.post("/nexconn/webhook", headers=h, content=body)          # replay of a handled delivery
    assert again.status_code == 200 and again.json()["duplicate_delivery"]
    h2, _ = signed({}, nonce="n2")
    retry = c.post("/nexconn/webhook", headers=h2, content=body)         # Nexconn retry, new nonce
    assert retry.json()["handled"][0]["result"] == "DUPLICATE"
    assert len(service.store.list("orders")) == 1


@pytest.mark.parametrize("tamper, status", [
    ({"secret": "WRONG"}, 401), ({"key": "OTHER"}, 401), ({"ts": 1}, 401),
])
def test_bad_webhooks_are_refused(service, tamper, status):
    c, _ = live_app(service, FakeNexconnServer())
    aid = service.request_approval("P-1003", 1, "x")["approval_id"]
    h, body = signed(decision_event(aid), **tamper)
    assert c.post("/nexconn/webhook", headers=h, content=body).status_code == status
    assert service.store.get("approvals", aid)["status"] == "PENDING"


def test_webhook_needs_headers_and_json(service):
    c, _ = live_app(service, FakeNexconnServer())
    assert c.post("/nexconn/webhook", content=b"{}").status_code == 401
    h, _ = signed({})
    assert c.post("/nexconn/webhook", headers=h, content=b"nope").status_code == 400
    h, body = signed({"type": "user:connection_status", "id": "x", "time": 1, "data": [{}]}, nonce="n9")
    assert c.post("/nexconn/webhook", headers=h, content=body).json()["handled"] == []


def test_webhook_from_non_approver_changes_nothing(service):
    c, _ = live_app(service, FakeNexconnServer())
    aid = service.request_approval("P-1003", 1, "x")["approval_id"]
    h, body = signed(decision_event(aid, user="mallory"))
    assert c.post("/nexconn/webhook", headers=h, content=body).json()["handled"][0]["result"] == "NOT_AN_APPROVER"
    assert service.store.get("approvals", aid)["status"] == "PENDING"


def test_relay_is_off_unless_enabled(service, monkeypatch):
    c, _ = live_app(service, FakeNexconnServer())
    aid = service.request_approval("P-1003", 1, "x")["approval_id"]
    body = {"message_id": "SDK-1", "user_id": "alice", "text": f"reject {aid}"}
    assert c.post("/nexconn/relay", json=body, headers={"X-Approver-Token": TOKEN}).status_code == 404


def test_relay_when_enabled_needs_token_and_dedupes_with_webhook(service, monkeypatch):
    c, _ = live_app(service, FakeNexconnServer(), relay=True, monkeypatch=monkeypatch)
    aid = service.request_approval("P-1003", 1, "x")["approval_id"]
    body = {"message_id": "SDK-1", "user_id": "alice", "text": f"reject {aid}"}
    assert c.post("/nexconn/relay", json=body).status_code == 401
    assert c.post("/nexconn/relay", json=body, headers={"X-Approver-Token": TOKEN}).json()["result"] == "REJECTED"
    h, ev = signed(decision_event(aid, mid="SDK-1", text=f"reject {aid}"))
    assert c.post("/nexconn/webhook", headers=h, content=ev).json()["handled"][0]["result"] == "DUPLICATE"


# ---- client details --------------------------------------------------------------------
def test_client_raises_on_business_error_and_bad_json():
    def handler(request):
        if request.url.path.endswith("issue"):
            return httpx.Response(200, json={"code": 1002, "msg": "signature"})
        return httpx.Response(200, content=b"<html>")
    cl = NexconnClient(LIVE, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(NexconnError) as e:
        cl.issue_token("alice", "alice")
    assert e.value.code == 1002
    with pytest.raises(NexconnError, match="not JSON"):
        cl.send_group_text("grbagent", "g", "hi")


def test_client_all_hosts_down():
    def handler(request):
        raise httpx.ConnectTimeout("t")
    cl = NexconnClient(LIVE, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(NexconnError, match="ConnectTimeout"):
        cl.issue_token("alice", "alice")


def test_bootstrap_failure_is_reported_not_raised(service):
    def handler(request):
        return httpx.Response(200, json={"code": 1002})
    ch = ChatApprovalChannel(service, LIVE, NexconnClient(LIVE, http=httpx.Client(
        transport=httpx.MockTransport(handler))), dispatch="inline")
    assert ch.bootstrap() is False and "1002" in ch.last_error
    aid = service.request_approval("P-1003", 1, "x")["approval_id"]
    assert ch.status()["posts"][0]["status"] == "FAILED" and ch.status()["posts"][0]["approval_id"] == aid


def test_sign_matches_docs_formula():
    assert sign("s", "14314", "1408710653000") == hashlib.sha1(b"s143141408710653000").hexdigest()


def test_mock_client_unknown_group():
    with pytest.raises(NexconnError):
        MockNexconn().send_group_text("a", "nogroup", "hi")
