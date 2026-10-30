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
  {messageId: 'N2', senderUserId: 'x', sentTime: 3, messageType: 'RC:TxtMsg', content: {text: 'elsewhere'}, channelIdentifier: {channelId: 'other'}}]});
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
    assert [x["id"] for x in lv["lg"]] == ["H1", "H2", "N1"]           # history oldest-first, then live; no image, no other room
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
    on every rendered copy of it (an at-least-once re-send can post the same request card twice)."""
    js = (ROOT / "app" / "static" / "chat.js").read_text()
    assert 'const ok = s === "connected";' in js
    assert "/connected|^0$/" not in js
    assert 'card.querySelector(".card-actions")?.remove();' in js
    assert "state.cards.set(aid, [...(state.cards.get(aid) || []), body]);" in js
    assert "for (const card of cards)" in js
