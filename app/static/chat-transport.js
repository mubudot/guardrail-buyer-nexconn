// Chat transports for the approval room. Both have the same shape:
//   start(onMessages)  -> Promise; onMessages(list of normalized messages), called for history and new ones
//   send(text, extra)  -> Promise<{messageId}>
//   stop()
// A normalized message: {id, from, text, extra (object|null), time (ms)}.
//
// live: the Nexconn Web Chat SDK (@nexconn/chat), connected with the access token the server
//       issued for this approver; messages go to and come from the Nexconn group channel.
// mock: the server's in-memory room (NEXCONN_MODE=mock), polled every 1.5 s.

export function parseExtra(raw) {
  if (!raw || typeof raw !== "string" || raw.length > 2048) return null;
  try { const v = JSON.parse(raw); return v && typeof v === "object" && !Array.isArray(v) ? v : null; }
  catch { return null; }
}

export async function createTransport(session) {
  return session.mode === "live" ? createNexconnTransport(session) : createMockTransport(session);
}

export function createMockTransport(session, {fetchImpl = fetch, intervalMs = 1500} = {}) {
  let next = 0, timer = null, stopped = false;
  const headers = {"X-Nexconn-Token": session.token, "Content-Type": "application/json"};
  const norm = (m) => {
    let body = {};
    try { body = JSON.parse(m.content); } catch { /* not JSON: shown empty */ }
    return {id: m.messageId, from: m.userId, text: String(body.content ?? ""), extra: parseExtra(body.extra),
            time: m.time};
  };
  async function poll(onMessages) {
    const r = await fetchImpl(`/nexconn/mock/messages?after=${next}`, {headers});
    if (!r.ok) throw new Error(`room: HTTP ${r.status}`);
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
        try { await poll(onMessages); onStatus("connected"); } catch { onStatus("reconnecting"); }
        timer = setTimeout(loop, intervalMs);
      };
      timer = setTimeout(loop, intervalMs);
    },
    async send(text, extra) {
      const r = await fetchImpl("/nexconn/mock/send", {method: "POST", headers, body: JSON.stringify({text, extra})});
      if (!r.ok) throw new Error(`send: HTTP ${r.status}`);
      const data = await r.json();
      return {messageId: data.message_id};
    },
    stop() { stopped = true; clearTimeout(timer); },
  };
}

export async function createNexconnTransport(session, sdkLoader = () => import("@nexconn/chat")) {
  const sdk = await sdkLoader();
  const {NCEngine, GroupChannel, SendTextMessageParams, MessageHandler, ConnectionStatusHandler,
         BaseChannel, AreaCode} = sdk;
  const channel = new GroupChannel(session.group_id);
  const norm = (m) => ({
    id: m.messageId, from: m.senderUserId, time: m.sentTime,
    text: String(m.content?.text ?? m.content?.content ?? ""), extra: parseExtra(m.content?.extra),
  });
  const mine = (m) => m.channelIdentifier?.channelId === session.group_id && m.messageType === "RC:TxtMsg";
  return {
    kind: "live",
    async start(onMessages, onStatus = () => {}) {
      NCEngine.initialize({appKey: session.app_key, areaCode: AreaCode[session.area] ?? AreaCode.SG});
      NCEngine.addConnectionStatusHandler("grb-approvals", new ConnectionStatusHandler({
        onConnectionStatusChanged({status}) { onStatus(String(status)); },
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
