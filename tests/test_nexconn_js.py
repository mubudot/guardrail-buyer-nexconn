"""chat-transport.js in plain node with a fake fetch and a fake Nexconn SDK: the mock transport
reads and sends through /nexconn/mock/*, the live transport initializes the SDK with the App Key
and area, connects with the issued token, loads the group's history oldest-first, keeps only this
group's text messages, and sends text with the structured `extra` as a JSON string. Skipped
without node."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available")

HARNESS = r"""
const mod = await import(process.argv[1]);
const out = {};
out.extra = [mod.parseExtra('{"a":1}'), mod.parseExtra('[1]'), mod.parseExtra('{bad'), mod.parseExtra(null)];
// crafted metadata from any group member: non-primitive fields are dropped, prototype keys ignored
const crafted = mod.parseExtra('{"grb":{"toString":null},"approval_id":["APR-1A2B3C4D"],"ok":"yes","n":2,"b":true,"z":null,"__proto__":{"polluted":1}}');
out.crafted = {value: crafted, proto: Object.getPrototypeOf(crafted) === Object.prototype, polluted: crafted.polluted === undefined,
               render: `k-${crafted.grb ?? ""}`};

// ---- mock transport
const calls = [];
const fetchImpl = async (url, opts = {}) => {
  calls.push({url, method: opts.method || 'GET', headers: opts.headers, body: opts.body});
  if (url.startsWith('/nexconn/mock/messages')) return {ok: true, json: async () => ({next: 1, messages: [
    {messageId: 'M1', userId: 'grbagent', time: 5, content: JSON.stringify({content: 'card', extra: '{"grb":"approval_request","approval_id":"APR-1A2B3C4D"}'})}]})};
  return {ok: true, json: async () => ({message_id: 'M2'})};
};
const t = mod.createMockTransport({token: 'mtok'}, {fetchImpl, intervalMs: 1e9});
const got = [];
await t.start((l) => got.push(...l));
const sent = await t.send('approve APR-1A2B3C4D', {grb: 'decision'});
t.stop();
out.mock = {got, sent, calls};

// ---- live transport with a fake SDK
const log = [];
class GroupChannel { constructor(id) { this.identifier = {channelId: id, channelType: 3}; }
  async sendMessage(p) { log.push(['send', p.content]); return {code: 0, data: {messageId: 'SDK-9'}}; } }
class SendTextMessageParams { constructor(c) { this.content = c; } }
class MessageHandler { constructor(h) { Object.assign(this, h); } }
class ConnectionStatusHandler { constructor(h) { Object.assign(this, h); } }
let handler = null, statusHandler = null;
const CS = {CONNECTED: 0, CONNECTING: 1, DISCONNECTED: 2, KICKED_OFFLINE_BY_OTHER_CLIENT: 6,
            0: 'CONNECTED', 1: 'CONNECTING', 2: 'DISCONNECTED', 6: 'KICKED_OFFLINE_BY_OTHER_CLIENT'};
const sdk = {ConnectionStatus: CS, GroupChannel, SendTextMessageParams, MessageHandler, ConnectionStatusHandler, AreaCode: {SG: 2, NA: 3},
  NCEngine: {initialize: (o) => log.push(['init', o]), addConnectionStatusHandler: (id, h) => { statusHandler = h; },
    addMessageHandler: (id, h) => { handler = h; }, connect: async (o) => { log.push(['connect', o]); return {isOk: true, code: 0}; },
    disconnect() {}},
  BaseChannel: {createMessagesQuery: (p) => { log.push(['query', p.pageSize, p.isAscending]); return {loadNextPage: async () => ({isOk: true, data: {data: [
    {messageId: 'H2', senderUserId: 'grbagent', sentTime: 2, messageType: 'RC:TxtMsg', content: {text: 'newer'}},
    {messageId: 'H1', senderUserId: 'alice', sentTime: 1, messageType: 'RC:TxtMsg', content: {text: 'older', extra: '{"x":1}'}},
    {messageId: 'IMG', senderUserId: 'alice', sentTime: 1, messageType: 'RC:ImgMsg', content: {}}]}})}; }}};
const live = await mod.createNexconnTransport({group_id: 'grbapprovals', app_key: 'KEY', area: 'NA', token: 'tok'}, async () => sdk);
const lg = [], statuses = [];
await live.start((l) => lg.push(...l), (s) => statuses.push(s));
for (const st of [1, 0, 2, 6, 42]) statusHandler.onConnectionStatusChanged({status: st, code: 0});
handler.onMessageReceived({messages: [
  {messageId: 'N1', senderUserId: 'grbagent', sentTime: 3, messageType: 'RC:TxtMsg', content: {text: 'card'}, channelIdentifier: {channelId: 'grbapprovals'}},
  {messageId: 'N2', senderUserId: 'x', sentTime: 3, messageType: 'RC:TxtMsg', content: {text: 'elsewhere'}, channelIdentifier: {channelId: 'other'}},
  // crafted by a group member: object-typed fields must not throw (String({toString: null}) does)
  {messageId: 'N3', senderUserId: {toString: null}, sentTime: {toString: null}, messageType: 'RC:TxtMsg',
   content: {text: {toString: null}, extra: '{"grb":{"toString":null}}'}, channelIdentifier: {channelId: 'grbapprovals'}}]});
const ls = await live.send('reject APR-1A2B3C4D', {grb: 'decision', approval_id: 'APR-1A2B3C4D', decision: 'reject'});
const plain = await live.send('hello', null);
out.live = {log, lg, ls, plain, statuses};
out.labels = [mod.statusLabel(CS, 0), mod.statusLabel(CS, '0'), mod.statusLabel(null, 2), mod.statusLabel(CS, 'x')];
console.log(JSON.stringify(out));
"""


def test_chat_transports():
    src = (ROOT / "app" / "static" / "chat-transport.js").resolve()
    r = subprocess.run([NODE, "--input-type=module", "-e", HARNESS, str(src.as_uri())],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["extra"] == [{"a": 1}, None, None, None]
    assert out["crafted"] == {"value": {"ok": "yes", "n": 2, "b": True}, "proto": True, "polluted": True, "render": "k-"}
    m = out["mock"]
    assert m["got"] == [{"id": "M1", "from": "grbagent", "text": "card", "time": 5,
                         "extra": {"grb": "approval_request", "approval_id": "APR-1A2B3C4D"}}]
    assert m["sent"] == {"messageId": "M2"}
    assert m["calls"][0]["headers"]["X-Nexconn-Token"] == "mtok"
    assert m["calls"][1]["url"] == "/nexconn/mock/send" and m["calls"][1]["method"] == "POST"
    assert json.loads(m["calls"][1]["body"]) == {"text": "approve APR-1A2B3C4D", "extra": {"grb": "decision"}}
    lv = out["live"]
    assert lv["log"][0] == ["init", {"appKey": "KEY", "areaCode": 3}]
    assert lv["log"][1] == ["connect", {"token": "tok"}]
    assert [x["id"] for x in lv["lg"]] == ["H1", "H2", "N1", "N3"]     # history oldest-first, then live; no image, no other room
    assert lv["lg"][3] == {"id": "N3", "from": "", "time": 0, "text": "", "extra": {}}  # crafted fields dropped, no throw
    assert lv["lg"][0]["extra"] == {"x": 1}
    assert lv["ls"] == {"messageId": "SDK-9"}
    sends = [x[1] for x in lv["log"] if x[0] == "send"]
    assert sends[0]["text"] == "reject APR-1A2B3C4D" and json.loads(sends[0]["extra"])["decision"] == "reject"
    assert sends[1] == {"text": "hello"}
    # SDK status codes become words (the page never shows "Connection: 1"); "connected" only for 0
    assert lv["statuses"] == ["connected", "connecting", "connected", "disconnected",
                              "kicked offline by other client", "status 42"]
    assert out["labels"] == ["connected", "connected", "status 2", "status x"]


def test_chat_page_connection_chip_and_closed_cards():
    """chat.js: the chip is green only for exactly "connected" (a "disconnected" status used to match
    /connected/), and a decided card loses its Approve / Reject buttons instead of keeping them greyed,
    on every rendered copy of it (an at-least-once re-send can post the same request card twice); pending
    approvals older than the loaded history stay reachable."""
    js = (ROOT / "app" / "static" / "chat.js").read_text()
    assert 'const ok = s === "connected";' in js
    assert "/connected|^0$/" not in js
    assert 'card.querySelector(".card-actions")?.remove();' in js
    assert "state.cards.set(aid, [...(state.cards.get(aid) || []), card]);" in js
    assert "for (const card of cards)" in js
    # The page loads the latest 50 messages; a still-pending approval whose card is older is listed above
    # the room (GET /approvals?status=PENDING) with the same Approve / Reject buttons, which send the
    # decision through the chat like any card; it disappears when its card shows up in the history.
    html = (ROOT / "app" / "static" / "chat.html").read_text()
    assert 'id="older-pending"' in html
    assert 'fetch("/approvals")' in js
    assert "planReconcile(list, state.cards.keys(), state.outcomes.keys())" in js
    assert "close.forEach(({id, status}) => closeCard(id, status));" in js
    assert "await syncApprovals();" in js
    # a card copy delivered after join (an at-least-once re-send) for an approval the sync closed arrives closed
    assert "state.closed.set(aid, status);" in js
    assert "decided.forEach(({id, status}) => state.closed.set(id, status));" in js
    assert "if (state.closed.has(x.approval_id)) closeCard(x.approval_id, state.closed.get(x.approval_id));" in js
    assert "dropOlderPending(x.approval_id);" in js
    assert js.count("approvalCard(") == 3   # definition, history card, older-pending card


RECONCILE = r"""
const m = await import(process.argv[1]);
const A = (id, status) => ({id, status, quantity: 1, product_name: 'x', merchant: 'm', amount: '1.00', currency: 'USD'});
const approvals = [A('APR-00000001', 'PENDING'), A('APR-00000002', 'PENDING'), A('APR-00000003', 'REJECTED'),
                   A('APR-00000004', 'APPROVED'), A('APR-00000005', 'REJECTED'), A('APR-00000006', 'EXECUTING'),
                   A('bad-id', 'PENDING'), {id: 'APR-00000007'}, null];
// in view: card 2 (pending), card 3 (rejected on the dashboard, its outcome is older than the history; e.g. a
// retried card delivery), card 5 (rejected, outcome in view), card 6 (being executed); not in view: 1 and 4
const plan = m.planReconcile(approvals, new Set(['APR-00000002', 'APR-00000003', 'APR-00000005', 'APR-00000006']).keys(),
                             new Map([['APR-00000005', {}]]).keys());
console.log(JSON.stringify({older: plan.older.map((a) => a.id), close: plan.close, decided: plan.decided,
  none: m.planReconcile(null, [], []), labels: ['APPROVED', 'REJECTED', 'DENIED_BY_POLICY', 'QUOTE_CHANGED',
  'EXECUTING', 'SOMETHING_NEW'].map(m.closedLabel)}));
"""


def test_chat_reconcile_with_server_status():
    """Rejoining the room: a card in view whose approval was already decided (its outcome message is
    further back than the 50 loaded messages) loses its buttons and shows the server's status; pending
    approvals without a card in view are listed above the room; cards whose outcome is in view are left to
    the outcome message. Codex submission review, round 3."""
    src = (ROOT / "app" / "static" / "chat-reconcile.js").resolve()
    r = subprocess.run([NODE, "--input-type=module", "-e", RECONCILE, str(src.as_uri())],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["older"] == ["APR-00000001"]
    assert out["close"] == [{"id": "APR-00000003", "status": "REJECTED"}, {"id": "APR-00000006", "status": "EXECUTING"}]
    # every decided approval (rendered or not; not the one whose outcome is in view) is remembered, so a card
    # delivered after join (a retried delivery of a card that never reached the room) is rendered closed
    assert out["decided"] == [{"id": "APR-00000003", "status": "REJECTED"}, {"id": "APR-00000004", "status": "APPROVED"},
                              {"id": "APR-00000006", "status": "EXECUTING"}]
    assert out["none"] == {"older": [], "close": [], "decided": []}
    assert [x["text"] for x in out["labels"]] == ["Closed: approved", "Closed: rejected", "Closed: refused by a hard rule",
                                                   "Closed: price changed", "Approved: placing the order",
                                                   "Closed: something new"]
    assert [x["tone"] for x in out["labels"]] == ["tone-ok", "tone-bad", "tone-bad", "tone-bad", "tone-ok", "tone-bad"]
