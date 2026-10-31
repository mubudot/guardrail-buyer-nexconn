// Chat transports for the approval room. Both have the same shape:
//   start(onMessages)  -> Promise; onMessages(list of normalized messages), called for history and new ones
//   send(text, extra)  -> Promise<{messageId}>
//   stop()
// A normalized message: {id, from, text, extra (object|null), time (ms)}.
//
// live: the Nexconn Web Chat SDK (@nexconn/chat), connected with the access token the server
//       issued for this approver; messages go to and come from the Nexconn group channel.
// mock: the server's in-memory room (NEXCONN_MODE=mock), polled every 1.5 s.

// Any group member can attach `extra`, so only flat primitive fields survive: an object or null where the
// page expects a string (e.g. {"grb": {"toString": null}}) would throw while rendering history.
export function parseExtra(raw) {
  if (!raw || typeof raw !== "string" || raw.length > 2048) return null;
  let v;
  try { v = JSON.parse(raw); } catch { return null; }
  if (!v || typeof v !== "object" || Array.isArray(v)) return null;
  const out = {};
  for (const [k, val] of Object.entries(v)) {
    if (k !== "__proto__" && ["string", "number", "boolean"].includes(typeof val)) out[k] = val;
  }
  return out;
}

// Message fields come from any group member; anything that is not a plain string / finite number is dropped
// (an object such as {"toString": null} would throw on String() and stop the whole history from loading).
export const textOf = (v) => (typeof v === "string" ? v : Number.isFinite(v) ? String(v) : "");
const timeOf = (v) => (Number.isFinite(v) ? v : 0);

// The SDK reports connection changes as numeric ConnectionStatus codes (0 = CONNECTED, 1 = CONNECTING,
// 2 = DISCONNECTED, 6 = KICKED_OFFLINE_BY_OTHER_CLIENT, ...). The page shows a word, never a bare number:
// "connected", or the enum's name in lower case ("connecting", "kicked offline by other client").
export function statusLabel(statusEnum, status) {
  if (status === 0 || status === "0") return "connected";
  const name = statusEnum && typeof status === "number" ? statusEnum[status] : null;
  if (typeof name === "string" && name) return name === "CONNECTED" ? "connected" : name.toLowerCase().replace(/_/g, " ");
  return `status ${String(status)}`;
}

export async function createTransport(session, {mockBase = "/nexconn/mock"} = {}) {
  return session.mode === "live" ? createNexconnTransport(session) : createMockTransport(session, {base: mockBase});
}

// `base`: the mock room's routes (/nexconn/mock for the approver chat, /try/mock for the playground).
export function createMockTransport(session, {fetchImpl = fetch, intervalMs = 1500, base = "/nexconn/mock"} = {}) {
  let next = 0, timer = null, stopped = false;
  const headers = {"X-Nexconn-Token": session.token, "Content-Type": "application/json"};
  const norm = (m) => {
    let body = {};
    try { body = JSON.parse(m.content); } catch { /* not JSON: shown empty */ }
    return {id: textOf(m.messageId), from: textOf(m.userId), text: textOf(body?.content), extra: parseExtra(body?.extra),
            time: timeOf(m.time)};
  };
  async function poll(onMessages) {
    const r = await fetchImpl(`${base}/messages?after=${next}`, {headers});
    if (!r.ok) { const e = new Error(`room: HTTP ${r.status}`); e.status = r.status; throw e; }
    const data = await r.json();
    next = data.next;
    if (data.messages.length) onMessages(data.messages.map(norm));
  }
  return {
    kind: "mock",
    async start(onMessages, onStatus = () => {}) {
      await poll(onMessages);
      onStatus("connected");
      const loop = async () => {
        if (stopped) return;
        try { await poll(onMessages); onStatus("connected"); } catch (e) { onStatus(e?.status === 401 ? "session expired" : "reconnecting"); }
        timer = setTimeout(loop, intervalMs);
      };
      timer = setTimeout(loop, intervalMs);
    },
    async send(text, extra) {
      const r = await fetchImpl(`${base}/send`, {method: "POST", headers, body: JSON.stringify({text, extra})});
      if (!r.ok) { const e = new Error(`send: HTTP ${r.status}`); e.status = r.status; throw e; }
      const data = await r.json();
      return {messageId: data.message_id};
    },
    stop() { stopped = true; clearTimeout(timer); },
  };
}

export async function createNexconnTransport(session, sdkLoader = () => import("@nexconn/chat")) {
  const sdk = await sdkLoader();
  const {NCEngine, GroupChannel, SendTextMessageParams, MessageHandler, ConnectionStatusHandler,
         BaseChannel, AreaCode, ConnectionStatus} = sdk;
  const channel = new GroupChannel(session.group_id);
  const norm = (m) => ({
    id: textOf(m.messageId), from: textOf(m.senderUserId), time: timeOf(m.sentTime),
    text: textOf(m.content?.text ?? m.content?.content), extra: parseExtra(m.content?.extra),
  });
  const mine = (m) => m.channelIdentifier?.channelId === session.group_id && m.messageType === "RC:TxtMsg";
  return {
    kind: "live",
    async start(onMessages, onStatus = () => {}) {
      NCEngine.initialize({appKey: session.app_key, areaCode: AreaCode[session.area] ?? AreaCode.SG});
      NCEngine.addConnectionStatusHandler("grb-approvals", new ConnectionStatusHandler({
        onConnectionStatusChanged({status}) { onStatus(statusLabel(ConnectionStatus, status)); },
      }));
      NCEngine.addMessageHandler("grb-approvals", new MessageHandler({
        onMessageReceived({messages}) { const ours = messages.filter(mine); if (ours.length) onMessages(ours.map(norm)); },
      }));
      const res = await NCEngine.connect({token: session.token});
      if (!res.isOk) throw new Error(`Nexconn connect failed (code ${res.code})`);
      onStatus("connected");
      const query = BaseChannel.createMessagesQuery({channelIdentifier: channel.identifier, startTime: Date.now(),
                                                     pageSize: 50, isAscending: false});
      const page = await query.loadNextPage();
      if (page.isOk && page.data) {
        const list = (page.data.data ?? page.data.list ?? page.data) || [];
        if (Array.isArray(list)) onMessages(list.filter((m) => m.messageType === "RC:TxtMsg").map(norm).reverse());
      }
    },
    async send(text, extra) {
      const params = new SendTextMessageParams(extra ? {text, extra: JSON.stringify(extra)} : {text});
      const res = await channel.sendMessage(params);
      if (res.code !== 0) throw new Error(`Nexconn send failed (code ${res.code})`);
      return {messageId: res.data?.messageId};
    },
    stop() { NCEngine.disconnect?.(); },
  };
}
