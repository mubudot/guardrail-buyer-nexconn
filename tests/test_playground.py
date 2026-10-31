"""The /try playground on the offline Nexconn stand-in (NEXCONN_MODE=mock): config, the first-run
flow end to end, isolation from the main data and between sandboxes, invites, reset, limits,
expiry and restart. No network."""
import json
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import ConfigError, create_app
from app.playground.config import PlaygroundConfig
from app.playground.manager import clean_name, new_user_id

TOKEN = "s3cret"
NO_LIMITS = {"TRY_CREATE_LIMIT": "off", "TRY_JOIN_LIMIT": "off", "TRY_AGENT_LIMIT": "off", "TRY_AGENT_LIMIT_IP": "off"}


def make(service, monkeypatch, tmp_path, limits=True, **env):
    monkeypatch.setenv("NEXCONN_MODE", "mock")
    monkeypatch.setenv("NEXCONN_APPROVER_IDS", "alice")
    monkeypatch.setenv("TRY_MODE", "on")
    monkeypatch.setenv("TRY_DIR", str(tmp_path / "try"))
    for k, v in {**(NO_LIMITS if limits else {}), **env}.items():
        monkeypatch.setenv(k, v)
    return TestClient(create_app(service, approver_token=TOKEN, agent_rate_limit=None, tools_rate_limit=None))


def start(c, name="Ada"):
    r = c.post("/try/start", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()


def H(s):
    return {"X-Try-Session": s["secret"]}


def room(c, sess):
    """Join the sandbox's room (mock transport): returns (session, messages)."""
    chat = c.post("/try/session", headers=H(sess)).json()
    msgs = c.get("/try/mock/messages", headers={"X-Nexconn-Token": chat["token"]}).json()["messages"]
    return chat, msgs


def extra(m):
    return json.loads(json.loads(m["content"]).get("extra") or "{}")


def ask(c, sess, text):
    r = c.post("/try/agent", headers=H(sess), json={"request": text})
    assert r.status_code == 200, r.text
    return r.json()


# ---- config ---------------------------------------------------------------------------------
@pytest.mark.parametrize("env, msg", [
    ({"TRY_MODE": "maybe"}, "TRY_MODE"),
    ({"TRY_MODE": "on"}, "NEXCONN_MODE"),
    ({"TRY_TTL_HOURS": "0"}, "TRY_TTL_HOURS"),
    ({"TRY_MAX_ACTIVE": "-3"}, "TRY_MAX_ACTIVE"),
    ({"TRY_MAX_MEMBERS": "40"}, "TRY_MAX_MEMBERS"),
    ({"TRY_CREATE_LIMIT": "5/x"}, "TRY_CREATE_LIMIT"),
])
def test_config_is_validated_at_startup(env, msg):
    with pytest.raises(ConfigError, match=msg):
        PlaygroundConfig.from_env(env, nexconn_mode="mock" if env.get("TRY_MODE") != "on" else "off")


def test_config_defaults():
    cfg = PlaygroundConfig.from_env({}, "off")
    assert (cfg.mode, cfg.ttl_hours, cfg.max_active, cfg.max_live_users, cfg.max_members) == ("off", 24, 200, 500, 4)
    assert cfg.create_limit == (5, 3600.0) and cfg.agent_limit == (20, 600.0) and cfg.agent_limit_ip == (30, 600.0)
    assert PlaygroundConfig.from_env({"TRY_MODE": "on", "TRY_MAX_LIVE_USERS": "0"}, "live").max_live_users == 0


def test_off_by_default_changes_nothing(service, monkeypatch):
    monkeypatch.setenv("NEXCONN_MODE", "mock")
    c = TestClient(create_app(service, approver_token=TOKEN, agent_rate_limit=None, tools_rate_limit=None))
    assert c.get("/try").status_code == 404 and c.post("/try/start", json={}).status_code == 404
    assert c.get("/health").json()["playground"] == {"mode": "off"}
    assert c.app.state.playground is None


def test_names_and_ids():
    assert clean_name("  Ada   Lovelace ") == "Ada Lovelace"
    for bad in ("<script>", "x" * 25, "", None, "名字"):
        assert clean_name(bad).startswith("Guest ")
    uid = new_user_id()
    assert len(uid) == 16 and uid[0] == "t" and uid.isalnum() and uid == uid.lower()


# ---- the first-run flow -------------------------------------------------------------------------
def test_first_run_flow_end_to_end(service, monkeypatch, tmp_path):
    c = make(service, monkeypatch, tmp_path)
    assert c.get("/try").status_code == 200 and c.get("/try/join/abc").status_code == 200
    assert c.get("/try/start").status_code == 405                         # creating needs a POST
    s = start(c)
    assert s["chat_mode"] == "mock" and s["member"]["role"] == "owner" and s["member"]["name"] == "Ada"
    assert "/try/join/" in s["invite_url"] and len(s["secret"]) >= 40
    st = c.get("/try/state", headers=H(s)).json()
    assert st["chat_fallback"] is False
    assert st["steps"] == {"joined": False, "auto_bought": False, "approval_requested": False, "decided": False,
                           "paid": False, "denied": False, "invited": False}
    assert st["limits"]["per_order_cap"] == "50.00" and st["limits"]["spent_today"] == "0.00"

    chat, msgs = room(c, s)
    assert chat["mode"] == "mock" and chat["group_id"] == f"grbtry{s['sandbox_id']}" and chat["app_key"] == ""
    assert chat["approver_ids"] == [s["member"]["user_id"]]
    assert "Welcome to your GuardRail sandbox, Ada" in json.loads(msgs[0]["content"])["content"]

    under = ask(c, s, "Buy 2 USB-C charging cables")
    assert len(under["pay_links"]) == 1 and under["pay_links"][0]["url"].startswith(f"/try/s/{s['sandbox_id']}/paypal/")
    over = ask(c, s, "Buy a 27-inch 4K monitor")
    assert "APR-" in over["final_text"] and not over["pay_links"]
    gift = ask(c, s, "Buy a $100 gift card")
    assert "1 × Gift Card $100: refused" in gift["final_text"] and "P-5001" not in gift["final_text"]
    st = c.get("/try/state", headers=H(s)).json()
    aid = st["approvals"][0]["id"]
    assert st["approvals"][0]["status"] == "PENDING"
    assert st["steps"]["joined"] and st["steps"]["auto_bought"] and st["steps"]["approval_requested"]
    assert st["steps"]["denied"] and not st["steps"]["decided"]

    tok = {"X-Nexconn-Token": chat["token"]}
    msgs = c.get("/try/mock/messages", headers=tok).json()["messages"]
    assert extra(msgs[-1]) == {**extra(msgs[-1]), "grb": "approval_request", "approval_id": aid}
    out = c.post("/try/mock/send", headers=tok, json={
        "text": f"approve {aid}", "extra": {"grb": "decision", "approval_id": aid, "decision": "approve"}}).json()
    assert out["handled"]["result"] == "AWAITING_PAYER_APPROVAL" and out["handled"]["decided_by"] == s["member"]["user_id"]
    outcome = c.get("/try/mock/messages", headers=tok).json()["messages"][-1]
    text = json.loads(outcome["content"])["content"]
    assert extra(outcome)["grb"] == "outcome" and "simulated checkout" in text and "decided by Ada in chat" in text

    st = c.get("/try/state", headers=H(s)).json()
    order = next(o for o in st["orders"] if o["approval_id"] == aid)
    pay = order["pay_url"]
    page = c.get(pay)
    assert page.status_code == 200 and "Pay now" in page.text and f"/try/s/{s['sandbox_id']}/paypal/mock-approve" in page.text
    r = c.get(pay.replace("mock-checkout", "mock-approve"), follow_redirects=True)
    assert r.status_code == 200 and f"paid={order['id']}" in str(r.url) and "status=COMPLETED" in str(r.url)
    st = c.get("/try/state", headers=H(s)).json()
    assert all(st["steps"][k] for k in ("joined", "auto_bought", "approval_requested", "decided", "paid", "denied"))
    assert st["limits"]["spent_today"] == "354.98"                            # 25.98 reserved + 329.00 paid


def test_reject_in_chat_and_cancel_on_checkout(service, monkeypatch, tmp_path):
    c = make(service, monkeypatch, tmp_path)
    s = start(c)
    chat, _ = room(c, s)
    ask(c, s, "Buy a mechanical keyboard")
    aid = c.get("/try/state", headers=H(s)).json()["approvals"][0]["id"]
    tok = {"X-Nexconn-Token": chat["token"]}
    out = c.post("/try/mock/send", headers=tok, json={"text": f"reject {aid}"}).json()
    assert out["handled"]["result"] == "REJECTED"
    st = c.get("/try/state", headers=H(s)).json()
    assert st["approvals"][0]["status"] == "REJECTED" and st["steps"]["decided"] and not st["orders"]
    # cancelling an auto-bought order on the simulated checkout needs the signed state
    url = ask(c, s, "Buy 2 USB-C charging cables")["pay_links"][0]["url"]
    page = c.get(url).text
    cancel = re.search(r'href="([^"]*/paypal/cancel[^"]*)"', page).group(1).replace("&amp;", "&")
    assert c.get(cancel.split("&state=")[0]).status_code == 403
    r = c.get(cancel, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/try?cancelled=")


def test_stranger_and_bad_sessions(service, monkeypatch, tmp_path):
    c = make(service, monkeypatch, tmp_path)
    assert c.get("/try/state").status_code == 401
    assert c.get("/try/state", headers={"X-Try-Session": "nope"}).status_code == 401
    assert c.post("/try/agent", headers={"X-Try-Session": "nope"}, json={"request": "Buy a mouse"}).status_code == 401
    assert c.get("/try/mock/messages", headers={"X-Nexconn-Token": "nope"}).status_code == 401
    assert c.get("/try/s/zzz/paypal/mock-checkout?token=x").status_code == 404
    assert c.get("/try/s/0123456789ab/paypal/mock-checkout?token=x").status_code == 404


# ---- isolation ----------------------------------------------------------------------------------
def test_sandboxes_are_isolated_from_main_data_and_each_other(service, monkeypatch, tmp_path):
    c = make(service, monkeypatch, tmp_path)
    main_session = c.post("/nexconn/session?as_user=alice", headers={"X-Approver-Token": TOKEN}).json()
    a, b = start(c, "A"), start(c, "B")
    chat_a, _ = room(c, a)
    chat_b, _ = room(c, b)
    ask(c, a, "Buy a 27-inch 4K monitor")
    ask(c, a, "Buy 2 USB-C charging cables")
    # main data and the judges' room are untouched
    assert c.get("/approvals").json() == [] and c.get("/orders").json() == []
    main_room = c.get("/nexconn/mock/messages", headers={"X-Nexconn-Token": main_session["token"]}).json()
    assert main_room["messages"] == []
    assert service.store.list("decisions") == []
    # B sees nothing of A
    sb = c.get("/try/state", headers=H(b)).json()
    assert sb["approvals"] == [] and sb["orders"] == [] and sb["limits"]["spent_today"] == "0.00"
    msgs_b = c.get("/try/mock/messages", headers={"X-Nexconn-Token": chat_b["token"]}).json()["messages"]
    assert all(extra(m).get("grb") != "approval_request" for m in msgs_b)
    # a member of B cannot decide in A: B's token posts into B's room only, so A's approval is unknown there
    aid = c.get("/try/state", headers=H(a)).json()["approvals"][0]["id"]
    out = c.post("/try/mock/send", headers={"X-Nexconn-Token": chat_b["token"]}, json={"text": f"approve {aid}"}).json()
    assert out["handled"]["result"] == "UNKNOWN_APPROVAL"
    assert c.get("/try/state", headers=H(a)).json()["approvals"][0]["status"] == "PENDING"
    # the main room's token is not a playground token, and a playground token is not a main-room token
    assert c.get("/try/mock/messages", headers={"X-Nexconn-Token": main_session["token"]}).status_code == 401
    assert c.get("/nexconn/mock/messages", headers={"X-Nexconn-Token": chat_a["token"]}).status_code == 401
    # a non-member message in A's room (e.g. via the live webhook) is refused by the channel
    pg = c.app.state.playground
    item = {"userId": chat_b["user_id"], "channelId": chat_a["group_id"], "channelType": 3, "messageType": "RC:TxtMsg",
            "messageId": "X-1", "content": json.dumps({"content": f"approve {aid}"})}
    assert pg.handle_message(item, "webhook")["result"] == "NOT_AN_APPROVER"
    assert pg.handle_message({**item, "channelId": "grbtryffffffffffff", "messageId": "X-2"}, "webhook")["result"] == "IGNORED"


# ---- invites ------------------------------------------------------------------------------------
def test_invite_join_second_approver_and_member_cap(service, monkeypatch, tmp_path):
    c = make(service, monkeypatch, tmp_path, TRY_MAX_MEMBERS="2")
    s = start(c)
    code = s["invite_url"].rsplit("/", 1)[1]
    assert c.post("/try/join", json={"code": "bad"}).status_code == 404
    t = c.post("/try/join", json={"code": code, "name": "Bob"}).json()
    assert t["member"]["role"] == "teammate" and t["sandbox_id"] == s["sandbox_id"]
    assert c.post("/try/join", json={"code": code}).status_code == 409                # cap of 2 members
    st = c.get("/try/state", headers=H(t)).json()
    assert [m["role"] for m in st["members"]] == ["owner", "teammate"] and st["steps"]["invited"]
    assert st["invite_url"] == s["invite_url"] and st["you"]["name"] == "Bob"
    ask(c, s, "Buy a 27-inch 4K monitor")
    aid = c.get("/try/state", headers=H(s)).json()["approvals"][0]["id"]
    chat_t, msgs = room(c, t)
    assert sorted(chat_t["approver_ids"]) == sorted([s["member"]["user_id"], t["member"]["user_id"]])
    assert any("Bob joined as an approver" in json.loads(m["content"])["content"] for m in msgs)
    out = c.post("/try/mock/send", headers={"X-Nexconn-Token": chat_t["token"]}, json={"text": f"approve {aid}"}).json()
    assert out["handled"]["result"] == "AWAITING_PAYER_APPROVAL" and out["handled"]["decided_by"] == t["member"]["user_id"]
    assert c.post("/try/reset", headers=H(t)).status_code == 403                       # owner only


# ---- reset ---------------------------------------------------------------------------------------
def test_reset_clears_spend_and_approvals_and_posts_a_notice(service, monkeypatch, tmp_path):
    c = make(service, monkeypatch, tmp_path)
    s = start(c)
    chat, _ = room(c, s)
    ask(c, s, "Buy 2 USB-C charging cables")
    ask(c, s, "Buy a 27-inch 4K monitor")
    assert c.get("/try/state", headers=H(s)).json()["limits"]["spent_today"] == "25.98"
    assert c.post("/try/reset", headers=H(s)).json() == {"ok": True}
    st = c.get("/try/state", headers=H(s)).json()
    assert st["approvals"] == [] and st["orders"] == [] and st["limits"]["spent_today"] == "0.00"
    assert st["steps"]["joined"] and not st["steps"]["auto_bought"]                    # room and members kept
    last = c.get("/try/mock/messages", headers={"X-Nexconn-Token": chat["token"]}).json()["messages"][-1]
    assert "Sandbox was reset" in json.loads(last["content"])["content"] and extra(last)["reset"] is True


# ---- limits ----------------------------------------------------------------------------------------
def test_rate_limits_answer_429_with_retry_after(service, monkeypatch, tmp_path):
    c = make(service, monkeypatch, tmp_path, limits=False, TRY_CREATE_LIMIT="2/3600", TRY_JOIN_LIMIT="1/3600",
             TRY_AGENT_LIMIT="2/600", TRY_AGENT_LIMIT_IP="off")
    s = start(c)
    start(c)
    r = c.post("/try/start", json={})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0 and "going fast" in r.json()["detail"]
    code = s["invite_url"].rsplit("/", 1)[1]
    assert c.post("/try/join", json={"code": code}).status_code == 200
    assert c.post("/try/join", json={"code": code}).status_code == 429
    ask(c, s, "Buy a wireless mouse")
    ask(c, s, "Buy a wireless mouse")
    r = c.post("/try/agent", headers=H(s), json={"request": "Buy a wireless mouse"})
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_agent_limit_per_ip(service, monkeypatch, tmp_path):
    c = make(service, monkeypatch, tmp_path, limits=False, TRY_CREATE_LIMIT="off", TRY_JOIN_LIMIT="off",
             TRY_AGENT_LIMIT="off", TRY_AGENT_LIMIT_IP="1/600")
    a, b = start(c), start(c)
    ask(c, a, "Buy a wireless mouse")
    assert c.post("/try/agent", headers=H(b), json={"request": "Buy a wireless mouse"}).status_code == 429


def test_max_active_answers_503(service, monkeypatch, tmp_path):
    c = make(service, monkeypatch, tmp_path, TRY_MAX_ACTIVE="1")
    start(c)
    r = c.post("/try/start", json={})
    assert r.status_code == 503 and "capacity" in r.json()["detail"]


# ---- expiry and restart -------------------------------------------------------------------------------
def test_expiry_sweep_deletes_the_db_and_the_secret_reads_expired(service, monkeypatch, tmp_path):
    c = make(service, monkeypatch, tmp_path)
    s = start(c)
    code = s["invite_url"].rsplit("/", 1)[1]
    pg = c.app.state.playground
    db = tmp_path / "try" / f"{s['sandbox_id']}.db"
    assert db.exists()
    with pg.registry.tx() as x:
        x.execute("UPDATE sandboxes SET last_seen=last_seen-? WHERE id=?", (25 * 3600, s["sandbox_id"]))
    assert pg.sweep(force=True) == [s["sandbox_id"]]
    assert not db.exists() and not (tmp_path / "try" / f"{s['sandbox_id']}.db-wal").exists()
    r = c.get("/try/state", headers=H(s))
    assert r.status_code == 404 and "expired" in r.json()["detail"]
    assert c.post("/try/join", json={"code": code}).status_code == 404
    assert pg.registry.stats()["expired"] == 1 and pg.registry.stats()["sandboxes_created"] == 1


def test_restart_resumes_by_secret_and_recovers_a_queued_room_post(service, policy, catalog, clock, monkeypatch,
                                                                   tmp_path):
    from app.paypal_client import MockPayPal
    from app.service import BuyerService
    c = make(service, monkeypatch, tmp_path)
    s = start(c)
    pg = c.app.state.playground
    pg.mock.fail_next_send = 1                          # the card's first send fails ...
    ask(c, s, "Buy a 27-inch 4K monitor")
    db = sqlite3.connect(tmp_path / "try" / f"{s['sandbox_id']}.db")
    db.execute("UPDATE nexconn_posts SET status='QUEUED' WHERE kind='request'")   # ... as if the process died
    db.commit()
    db.close()
    pg.close()                                          # shutdown

    service2 = BuyerService(policy, catalog, MockPayPal(), ":memory:", clock=clock)
    c2 = TestClient(create_app(service2, approver_token=TOKEN, agent_rate_limit=None, tools_rate_limit=None))
    st = c2.get("/try/state", headers=H(s)).json()     # the same secret resumes the sandbox
    assert st["sandbox_id"] == s["sandbox_id"] and st["approvals"][0]["status"] == "PENDING"
    chat, msgs = room(c2, s)
    cards = [m for m in msgs if extra(m).get("grb") == "approval_request"]
    assert len(cards) == 1 and extra(cards[0])["approval_id"] == st["approvals"][0]["id"]
    c2.app.state.playground.close()


def test_lru_eviction_reloads_from_disk_and_keeps_simulated_orders(service, monkeypatch, tmp_path):
    c = make(service, monkeypatch, tmp_path, TRY_CACHE_SIZE="1")
    a, b = start(c, "A"), start(c, "B")
    url = ask(c, a, "Buy 2 USB-C charging cables")["pay_links"][0]["url"]
    c.get("/try/state", headers=H(b))                   # evicts A
    assert list(c.app.state.playground._cache) == [b["sandbox_id"]]
    r = c.get(url.replace("mock-checkout", "mock-approve"), follow_redirects=False)
    assert r.status_code == 303
    assert c.get(r.headers["location"], follow_redirects=False).headers["location"].startswith("/try?paid=")
    assert c.get("/try/state", headers=H(a)).json()["steps"]["paid"]


def test_admin_cli(service, monkeypatch, tmp_path, capsys):
    from app.playground import admin
    c = make(service, monkeypatch, tmp_path)
    s = start(c)
    c.app.state.playground.close()
    d = str(tmp_path / "try")
    assert admin.main(["--dir", d, "stats"]) == 0
    assert json.loads(capsys.readouterr().out)["active"] == 1
    assert admin.main(["--dir", d, "expire-now", "--all"]) == 0
    assert not (tmp_path / "try" / f"{s['sandbox_id']}.db").exists()
    assert admin.main(["--dir", d, "purge-expired"]) == 0
    capsys.readouterr()
    admin.main(["--dir", d, "stats"])
    out = json.loads(capsys.readouterr().out)
    assert out["active"] == 0 and out["expired"] == 0 and out["sandboxes_created"] == 1


# ---- Codex cross-review R1 (2026-11-01): memory caps, lock map, idle TTL on every access ------------
def test_idle_session_cannot_revive_its_sandbox_before_the_sweep(service, monkeypatch, tmp_path):
    c = make(service, monkeypatch, tmp_path)
    s = start(c)
    chat, _ = room(c, s)
    pg = c.app.state.playground
    with pg.registry.tx() as x:     # idle past the TTL, but the 10-minute sweep has not run yet
        x.execute("UPDATE sandboxes SET last_seen=last_seen-? WHERE id=?", (25 * 3600, s["sandbox_id"]))
    for r in (c.post("/try/session", headers=H(s)), c.get("/try/state", headers=H(s))):
        assert r.status_code == 404 and "expired" in r.json()["detail"]
    assert pg.registry.get(s["sandbox_id"])["status"] == "EXPIRED"
    assert not (tmp_path / "try" / f"{s['sandbox_id']}.db").exists()
    tok = {"X-Nexconn-Token": chat["token"]}
    assert c.get("/try/mock/messages", headers=tok).status_code == 401     # the room token is revoked
    assert c.get(f"/try/s/{s['sandbox_id']}/paypal/mock-checkout?token=x").status_code == 404


def test_unknown_checkout_ids_leave_no_per_sandbox_state(service, monkeypatch, tmp_path):
    c = make(service, monkeypatch, tmp_path)
    pg = c.app.state.playground
    for i in range(50):
        assert c.get(f"/try/s/{i:012x}/paypal/mock-checkout?token=x").status_code == 404
    assert pg._load_locks == {} and len(pg._cache) == 0


def test_mock_room_sends_are_bounded(service, monkeypatch, tmp_path):
    c = make(service, monkeypatch, tmp_path)
    s = start(c)
    chat, _ = room(c, s)
    tok = {"X-Nexconn-Token": chat["token"]}
    big = {"grb": "decision", "approval_id": "APR-0001", "decision": "x" * 65536}
    r = c.post("/try/mock/send", headers=tok, json={"text": "hi", "extra": big})
    assert r.status_code == 200
    assert all(len(m["content"]) < 2000 for m in c.get("/try/mock/messages", headers=tok).json()["messages"])
    codes = [c.post("/try/mock/send", headers=tok, json={"text": "hi"}).status_code for _ in range(40)]
    assert 429 in codes and codes.count(200) <= 30
    pg = c.app.state.playground
    assert pg.mock.max_group_messages and pg.mock.max_total_messages is None   # per-room caps only


def test_mock_room_message_cap_answers_429():
    from app.nexconn.client import MockNexconn, NexconnError
    m = MockNexconn(max_group_messages=3)
    m.ensure_group("g", "g", "bot", ["bot"])
    for _ in range(3):
        m.send_group_text("bot", "g", "x")
    with pytest.raises(NexconnError) as e:
        m.send_group_text("bot", "g", "x")
    assert e.value.status == 429
    toks = [m.issue_token("u", "u") for _ in range(6)]
    assert m.user_for_token(toks[0]) is None and m.user_for_token(toks[-1]) == "u"
    m.revoke_user("u")
    assert m.user_for_token(toks[-1]) is None


# ---- Codex cross-review R2 (2026-11-01): conditional expiry, per-room (not shared) message budget ------
def test_expiry_from_a_stale_check_never_deletes_a_sandbox_that_was_just_used(service, monkeypatch, tmp_path):
    c = make(service, monkeypatch, tmp_path)
    s = start(c)
    pg = c.app.state.playground
    sid = s["sandbox_id"]
    with pg.registry.tx() as x:      # what a slow request saw: idle past the TTL ...
        x.execute("UPDATE sandboxes SET last_seen=last_seen-? WHERE id=?", (25 * 3600, sid))
    stale = pg.registry.get(sid)
    with pg.registry.tx() as x:      # ... but another request has used the sandbox since
        x.execute("UPDATE sandboxes SET last_seen=? WHERE id=?", (pg.clock(), sid))
    pg._gone(stale)                  # the slow request's expiry attempt
    assert pg.sweep(force=True) == []
    assert pg.registry.get(sid)["status"] == "ACTIVE"
    assert (tmp_path / "try" / f"{sid}.db").exists()
    assert c.get("/try/state", headers=H(s)).status_code == 200


def test_a_full_room_affects_only_itself_and_keeps_slots_for_the_bot(service, monkeypatch, tmp_path):
    from app.playground import manager as M
    monkeypatch.setattr(M, "MOCK_SEND_LIMIT", None)
    c = make(service, monkeypatch, tmp_path)
    pg = c.app.state.playground
    pg.mock_send_limiter = None
    a, b = start(c, "Ann"), start(c, "Bob")
    ta = {"X-Nexconn-Token": room(c, a)[0]["token"]}
    tb = {"X-Nexconn-Token": room(c, b)[0]["token"]}
    codes = [c.post("/try/mock/send", headers=ta, json={"text": "spam"}).status_code
             for _ in range(M.MOCK_ROOM_USER_MESSAGES + 5)]
    assert codes[-1] == 429 and "full" in c.post("/try/mock/send", headers=ta, json={"text": "x"}).json()["detail"]
    assert pg.mock.group_count(pg.registry.get(a["sandbox_id"])["group_id"]) == M.MOCK_ROOM_USER_MESSAGES
    assert c.post("/try/mock/send", headers=tb, json={"text": "hi"}).status_code == 200    # other room unaffected
    r = c.post("/try/agent", headers=H(a), json={"request": "buy the USB-C hub"})          # bot posts still fit
    assert r.status_code == 200
