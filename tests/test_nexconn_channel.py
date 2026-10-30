"""Chat approval channel (Nexconn Build Challenge add-on), offline: MockNexconn, inline posting.

Covers: an approval request posts a card into the room; an approver's "approve APR-…" in the
room runs the same guarded BuyerService.approve and posts the outcome; rejects; non-approvers,
other rooms, the bot's own messages, duplicates and stale decisions change nothing; dashboard
decisions are mirrored into the room; a failed post never breaks the purchase and is retried.
"""
import json
import uuid

import pytest

from app.nexconn import ChatApprovalChannel, MockNexconn, NexconnConfig
from app.nexconn.protocol import parse_decision, request_card
from app.nexconn.client import text_content

CFG = NexconnConfig(mode="mock", group_id="grbapprovals", bot_user_id="grbagent",
                    approver_ids=("alice", "bob"))


@pytest.fixture
def chat(service):
    ch = ChatApprovalChannel(service, CFG, MockNexconn(), dispatch="inline")
    assert ch.bootstrap()
    return ch


def room(ch):
    out = []
    for m in ch.client.history(CFG.group_id):
        body = json.loads(m["content"])
        out.append({"from": m["userId"], "text": body["content"],
                    "extra": json.loads(body["extra"]) if "extra" in body else None, "id": m["messageId"]})
    return out


def say(ch, user, text, extra=None, mid=None, group=None, channel_type=3):
    """What a `message:send` webhook delivers for `user` saying `text` (also posted to the mock room)."""
    group = group or CFG.group_id
    if mid is None:
        mid = ch.client.send_group_text(user, group, text, extra) if group in ch.client.groups \
            else f"M-{uuid.uuid4().hex[:8]}"
    return ch.handle_message({"userId": user, "channelId": group, "channelType": channel_type,
                              "messageType": "RC:TxtMsg", "messageId": mid,
                              "content": text_content(text, extra)}, "test")


def ask(service):
    a = service.request_approval("P-1003", 1, "new hire keyboard")   # 89.00 > 50 cap
    assert a["result"] == "APPROVAL_REQUESTED"
    return a["approval_id"]


def test_request_posts_a_card(chat, service):
    aid = ask(service)
    msgs = room(chat)
    assert len(msgs) == 1 and msgs[0]["from"] == "grbagent"
    assert msgs[0]["extra"]["grb"] == "approval_request" and msgs[0]["extra"]["approval_id"] == aid
    assert aid in msgs[0]["text"] and "89.00 USD" in msgs[0]["text"] and f"approve {aid}" in msgs[0]["text"]
    assert chat.status()["posts"][0]["status"] == "SENT"


def test_approve_in_chat_runs_guarded_approve_and_posts_outcome(chat, service):
    aid = ask(service)
    out = say(chat, "alice", f"approve {aid}")
    assert out["result"] == "ORDER_PLACED" and out["decided_by"] == "alice"
    assert service.store.get("approvals", aid)["status"] == "APPROVED"
    assert str(service.spent_today()) == "89.00"
    last = room(chat)[-1]
    assert last["from"] == "grbagent" and last["extra"]["grb"] == "outcome"
    assert last["extra"]["via"] == "chat" and "alice" in last["text"] and "in chat" in last["text"]
    assert last["extra"]["order_id"] == out["order_id"]


def test_structured_button_decision_and_reject(chat, service):
    aid = ask(service)
    out = say(chat, "bob", f"reject {aid}", {"grb": "decision", "approval_id": aid, "decision": "reject"})
    assert out["result"] == "REJECTED"
    assert service.store.get("approvals", aid)["status"] == "REJECTED"
    assert service.store.list("orders") == []
    assert "rejected — nothing was bought" in room(chat)[-1]["text"]


def test_text_and_extra_must_agree(chat, service):
    aid = ask(service)
    out = say(chat, "alice", f"approve {aid}", {"grb": "decision", "approval_id": aid, "decision": "reject"})
    assert out["result"] == "IGNORED"
    assert service.store.get("approvals", aid)["status"] == "PENDING"


@pytest.mark.parametrize("text", ["ok", "yes", "approve", "approve it", "approve APR-1234", "please approve APR-ABCDEF12 now"])
def test_vague_messages_never_decide(chat, service, text):
    ask(service)
    assert say(chat, "alice", text)["result"] == "IGNORED"
    assert service.store.list("approvals", "PENDING")


def test_non_approver_cannot_decide(chat, service):
    aid = ask(service)
    out = say(chat, "mallory", f"approve {aid}")
    assert out["result"] == "NOT_AN_APPROVER"
    assert service.store.get("approvals", aid)["status"] == "PENDING"
    assert "not an approver" in room(chat)[-1]["text"]


def test_bot_and_other_rooms_are_ignored(chat, service):
    aid = ask(service)
    assert say(chat, "grbagent", f"approve {aid}")["result"] == "IGNORED"
    assert say(chat, "alice", f"approve {aid}", mid="M-other", group="elsewhere")["result"] == "IGNORED"
    assert say(chat, "alice", f"approve {aid}", mid="M-direct", channel_type=1)["result"] == "IGNORED"
    assert service.store.get("approvals", aid)["status"] == "PENDING"


def test_duplicate_delivery_is_applied_once(chat, service):
    aid = ask(service)
    first = say(chat, "alice", f"approve {aid}", mid="MSG-1")
    again = say(chat, "alice", f"approve {aid}", mid="MSG-1")   # webhook retry of the same message
    assert first["result"] == "ORDER_PLACED" and again["result"] == "DUPLICATE"
    assert len(service.store.list("orders")) == 1 and str(service.spent_today()) == "89.00"


def test_second_decision_is_refused(chat, service):
    aid = ask(service)
    say(chat, "alice", f"reject {aid}")
    out = say(chat, "bob", f"approve {aid}")
    assert out["result"] == "NOT_PENDING"
    assert service.store.get("approvals", aid)["status"] == "REJECTED"
    assert service.store.list("orders") == []
    assert "already REJECTED" in room(chat)[-1]["text"]


def test_approve_twice_replays_without_second_order(chat, service):
    aid = ask(service)
    say(chat, "alice", f"approve {aid}")
    out = say(chat, "bob", f"approve {aid}")
    assert out["result"] == "ORDER_PLACED" and len(service.store.list("orders")) == 1
    assert "already approved" in room(chat)[-1]["text"]


def test_unknown_approval(chat):
    assert say(chat, "alice", "approve APR-00000000")["result"] == "UNKNOWN_APPROVAL"


def test_hard_rules_still_apply_after_chat_approval(chat, service):
    aid = ask(service)
    p = service.catalog.get("P-1003")
    service.update_policy({"blocked_merchants": [p["merchant"]],
                           "allowed_merchants": [m for m in service.policy.allowed_merchants
                                                 if m.lower() != p["merchant"].lower()]})
    out = say(chat, "alice", f"approve {aid}")
    assert out["result"] == "DENIED"
    assert service.store.list("orders") == []
    assert "hard rule" in room(chat)[-1]["text"]


def test_dashboard_decisions_are_mirrored_into_the_room(chat, service):
    aid = ask(service)
    service.approve(aid)                          # e.g. POST /approvals/{id}/approve
    last = room(chat)[-1]
    assert last["extra"] == {"grb": "outcome", "approval_id": aid, "result": "ORDER_PLACED", "via": "dashboard",
                             "order_id": last["extra"]["order_id"]}
    assert "on the dashboard" in last["text"]


def test_failed_post_never_breaks_purchase_and_is_retried(service):
    client = MockNexconn()
    ch = ChatApprovalChannel(service, CFG, client, dispatch="inline")
    ch.bootstrap()
    client.fail_next_send = 1
    aid = ask(service)                            # the agent still gets its approval id
    assert room(ch) == []
    assert ch.status()["posts"][0]["status"] == "FAILED"
    assert ch.retry_failed() == 1
    assert room(ch)[0]["extra"]["approval_id"] == aid
    assert ch.status()["posts"][0]["status"] == "SENT"


def test_listener_exception_does_not_break_service(service):
    service.add_approval_listener(lambda *a: 1 / 0)
    assert service.request_approval("P-1003", 1, "x")["result"] == "APPROVAL_REQUESTED"


def test_threaded_dispatch_drains(service):
    ch = ChatApprovalChannel(service, CFG, MockNexconn(), dispatch="thread")
    ch.bootstrap()
    aid = ask(service)
    assert ch.drain(5)
    assert room(ch)[0]["extra"]["approval_id"] == aid


def test_parse_decision_shapes():
    lower = parse_decision("RC:TxtMsg", text_content("Approve apr-1a2b3c4d"))   # case-insensitive
    assert lower and lower.approval_id == "APR-1A2B3C4D"
    assert parse_decision("RC:TxtMsg", text_content("approve APR-1A2B3C4G")) is None  # not hex
    d = parse_decision("RC:TxtMsg", text_content("/approve APR-1A2B3C4D."))
    assert d and d.approval_id == "APR-1A2B3C4D" and d.decision == "approve"
    assert parse_decision("RC:ImgMsg", text_content("approve APR-1A2B3C4D")) is None
    assert parse_decision("RC:TxtMsg", "not json") is None
    assert parse_decision("RC:TxtMsg", {"content": "x", "extra": "{bad"}) is None


def test_card_text_is_bounded():
    text, extra = request_card({"id": "APR-1A2B3C4D", "quantity": 1, "product_name": "K", "merchant": "M",
                                "amount": "1.00", "currency": "USD", "justification": "j" * 5000,
                                "reasons": json.dumps(["per-order cap"])})
    assert len(text) < 1000 and extra["approval_id"] == "APR-1A2B3C4D"


# ---- Codex review 1 regressions ----------------------------------------------------------
def test_unsent_posts_are_recovered_on_start(service):
    client = MockNexconn()
    ch = ChatApprovalChannel(service, CFG, client, dispatch="inline")
    ch.bootstrap()
    with service.store.tx() as c:         # a previous run queued a post and stopped before sending it
        c.execute("INSERT INTO nexconn_posts (approval_id, kind, text, extra, status, created_at) "
                  "VALUES ('APR-00000001', 'request', 'hi', '{}', 'QUEUED', 'now')")
    ch.start()
    assert [m["content"] for m in client.history(CFG.group_id)] == [text_content("hi", {})]
    assert ch.status()["posts"][0]["status"] == "SENT"


def test_a_post_is_sent_once_even_if_two_senders_race(service):
    client = MockNexconn()
    ch = ChatApprovalChannel(service, CFG, client, dispatch="inline")
    ch.bootstrap()
    with service.store.tx() as c:
        pid = c.execute("INSERT INTO nexconn_posts (approval_id, kind, text, extra, status, created_at) "
                        "VALUES ('APR-00000001', 'request', 'hi', '{}', 'QUEUED', 'now')").lastrowid
    ch._send(pid)
    ch._send(pid)
    assert len(client.history(CFG.group_id)) == 1


def test_stale_inbound_claim_is_taken_over(chat, service):
    aid = ask(service)
    assert chat._claim_inbound("M-dead", "alice", aid, "approve", "webhook") == "new"   # handler died here
    from app.nexconn.channel import InboundBusy
    with pytest.raises(InboundBusy):          # in progress: not acknowledged as a duplicate
        say(chat, "alice", f"approve {aid}", mid="M-dead")
    import time as _t
    chat._clock = lambda: _t.time() + 1000
    assert say(chat, "alice", f"approve {aid}", mid="M-dead")["result"] == "ORDER_PLACED"
    assert say(chat, "alice", f"approve {aid}", mid="M-dead")["result"] == "DUPLICATE"


def test_abandoned_decision_is_finished_without_redelivery(chat, service):
    aid = ask(service)
    chat._claim_inbound("M-crash", "alice", aid, "approve", "webhook")      # process died after claiming
    assert chat.recover_inbound() == 0                                       # not stale yet
    import time as _t
    chat._clock = lambda: _t.time() + 1000
    assert chat.recover()["decisions"] == 1
    assert service.store.get("approvals", aid)["status"] == "APPROVED"
    assert chat.status()["inbound"][0]["state"] == "DONE"


def test_automatic_retry_respects_claims_and_attempt_cap(service):
    client = MockNexconn()
    ch = ChatApprovalChannel(service, CFG, client, dispatch="inline")
    ch.bootstrap()
    import time as _t
    with service.store.tx() as c:
        c.execute("INSERT INTO nexconn_posts (approval_id, kind, text, extra, status, claimed_at, created_at) "
                  "VALUES ('APR-00000001', 'request', 'sending', '{}', 'SENDING', ?, '2026-01-01T00:00:00+00:00')",
                  (_t.time(),))
        c.execute("INSERT INTO nexconn_posts (approval_id, kind, text, extra, status, attempts, created_at) "
                  "VALUES ('APR-00000002', 'request', 'gave up', '{}', 'FAILED', 5, '2026-01-01T00:00:00+00:00')")
    assert ch.retry_failed(auto=True) == 0          # one is being sent right now, the other hit the cap
    ch._clock = lambda: _t.time() + 1000
    assert ch.retry_failed(auto=True) == 1          # the sender died: picked up
    assert ch.retry_failed() == 1                   # manual retry has no attempt cap
    assert len(client.history(CFG.group_id)) == 2


def test_listener_snapshot_failure_is_contained(service, monkeypatch):
    service.add_approval_listener(lambda *a: None)
    real_get = service.store.get
    monkeypatch.setattr(service.store, "get", lambda t, i: (_ for _ in ()).throw(RuntimeError("x"))
                        if t == "approvals" else real_get(t, i))
    assert service.request_approval("P-1003", 1, "x")["result"] == "APPROVAL_REQUESTED"


# ---- Codex review 3 regression -----------------------------------------------------------
@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_outcome_is_posted_even_if_the_process_died_before_the_listener(chat, service, monkeypatch, decision):
    aid = ask(service)
    chat._claim_inbound("M-x", "alice", aid, decision, "webhook")
    monkeypatch.setattr(service, "_approval_listeners", [])        # "crash" right after the decision commits
    (service.approve if decision == "approve" else service.reject)(aid)
    monkeypatch.undo()
    import time as _t
    chat._clock = lambda: _t.time() + 1000
    out = chat.recover()
    assert out["decisions"] == 1 and out["outcomes"] == 1
    last = room(chat)[-1]
    assert last["extra"]["grb"] == "outcome" and last["extra"]["approval_id"] == aid
    assert last["extra"]["result"] == ("ORDER_PLACED" if decision == "approve" else "REJECTED")
    assert "decided by alice in chat" in last["text"]
    assert chat.recover()["outcomes"] == 0                          # posted once
