// Approval chat page: join the Nexconn approval room as an approver, see the agent's approval
// cards, approve / reject them in the chat. All message text is rendered as text, never HTML.
// The room itself (message list, approval cards, reconcile on join) lives in chat-room.js, shared
// with the /try playground.
import {createTransport} from "/static/chat-transport.js";
import {createRoom, el, isDecision} from "/static/chat-room.js";
import {OFFLINE_TOOLTIP, offlineView, watchSdk} from "/static/offline-model.js";
import {updateOfflineNotice} from "/static/offline-notice.js";

const $ = (id) => document.getElementById(id);

const state = {cfg: null, session: null, transport: null, offlineFallback: null};
let sdkWatch = null, roomGen = 0;
const room = createRoom({
  list: $("messages"), older: $("older-pending"), session: () => state.session, send: (t, x) => send(t, x),
  fetchApprovals: async () => { const r = await fetch("/approvals"); return r.ok ? r.json() : null; },
});
const {renderMessage, onMessages, syncApprovals} = room;

function showError(msg) { const e = $("join-error"); e.textContent = msg; e.hidden = !msg; }
function setConn(s) {
  sdkWatch?.status(s);
  const c = $("conn"); c.hidden = false;
  const ok = s === "connected";   // exact: "disconnected" must not read as connected
  c.textContent = ok ? "Connected" : `Connection: ${s}`;
  c.className = `chip ${ok ? "env-sandbox" : "warn"}`;
}

async function relay(messageId, text, extra) {
  // live mode, local run without a public webhook: tell our server about the decision we sent
  const r = await fetch("/nexconn/relay", {method: "POST", headers: authHeaders(),
    body: JSON.stringify({message_id: messageId, user_id: state.session.user_id, text, extra: extra || null})});
  if (!r.ok) {
    const detail = (await r.json().catch(() => ({}))).detail;
    throw new Error(`the decision was posted in the room but the server did not take it (HTTP ${r.status}` +
                    `${detail ? `: ${detail}` : ""}); try again`);
  }
  return r.json();
}

async function send(text, extra) {
  const {messageId} = await state.transport.send(text, extra);
  if (state.session.mode === "live") {   // the SDK does not echo our own message back
    renderMessage({id: messageId, from: state.session.user_id, text, extra, time: Date.now()});
    const box = $("messages"); box.scrollTop = box.scrollHeight;
  }
  if (state.session.mode === "live" && state.cfg.relay && isDecision(text, extra)) {
    await relay(messageId, text, extra);
  }
}

function authHeaders() {
  return {"Content-Type": "application/json", "X-Approver-Token": $("token").value};
}

async function join(ev) {
  ev?.preventDefault();
  const gen = ++roomGen, stale = () => gen !== roomGen;
  sdkWatch?.stop(); sdkWatch = null;
  try { state.transport?.stop(); } catch { /* already stopped */ }
  state.transport = null;
  room.clear();
  showError("");
  $("join-btn").disabled = true;
  try {
    const r = await fetch(`/nexconn/session?as_user=${encodeURIComponent($("as-user").value)}`,
                          {method: "POST", headers: authHeaders()});
    if (!r.ok) {
      const detail = (await r.json().catch(() => ({}))).detail;
      throw new Error(r.status === 401 ? "Wrong approver token." : `Could not join (HTTP ${r.status}${detail ? `: ${detail}` : ""}).`);
    }
    const session = await r.json();
    if (stale()) return;
    state.session = session;
    await pollConfig(false);
    if (stale()) return;
    localStorage.setItem("approverToken", $("token").value);
    sdkWatch = state.session.mode === "live" ? watchSdk(() => probeForSdk()) : null;
    const transport = await createTransport(state.session);
    if (stale()) { transport.stop(); return; }
    state.transport = transport;
    $("room-name").textContent = `Approval room · ${state.session.group_id}`;
    $("room-who").textContent = `You are ${state.session.user_id}. Approvers: ${state.session.approver_ids.join(", ")}`;
    await transport.start((messages) => { if (!stale()) onMessages(messages); },
                          (status) => { if (!stale()) setConn(status); });
    if (stale()) { transport.stop(); return; }
    await syncApprovals();
    $("join").hidden = true;
    $("room").hidden = false;
    $("text").focus();
  } catch (e) {
    if (stale()) return;
    sdkWatch?.fail();
    try { state.transport?.stop(); } catch { /* already stopped */ }
    state.transport = null;
    room.clear();
    $("room").hidden = true;
    $("join").hidden = false;
    showError(e.message || String(e));
    $("join-btn").disabled = false;
  }
}

function renderMode() {
  const cfg = state.cfg;
  if (!cfg) return;
  if (cfg.fallback?.active) state.offlineFallback = cfg.fallback;
  // Keep an open stand-in honest after recovery: its room stays here until Reconnect.
  const data = state.session?.mode === "mock" && cfg.configured_mode === "live" && state.offlineFallback ?
    {...cfg, mode: "mock", fallback: {...state.offlineFallback, upstream: cfg.upstream?.state}} : cfg;
  const offline = offlineView(data).visible, live = data.mode === "live";
  const m = $("chat-mode");
  m.textContent = offline ? "Offline stand-in (Nexconn unreachable)" : live ? `Nexconn live · ${cfg.area}` : "Nexconn mock (offline)";
  m.className = `chip ${live ? "env-sandbox" : "env-mock"}`;
  m.title = offline ? OFFLINE_TOOLTIP : live ? `Nexconn Web Chat SDK ${cfg.sdk_version}` :
    "In-memory stand-in for Nexconn: no account, no network";
  updateOfflineNotice($("offline-banner"), data);
}

async function pollConfig(rejoin = true) {
  try {
    const r = await fetch("/nexconn/config");
    if (!r.ok) return;
    state.cfg = await r.json();
    renderMode();
    if (rejoin && state.session?.mode === "live" && state.cfg.mode === "mock") await join();
  } catch { /* retry on the next poll */ }
}

async function probeForSdk() {
  try { await fetch("/nexconn/probe", {method: "POST"}); } catch { /* config still has the last real observation */ }
  await pollConfig();
}

async function showTryLink() {
  // "No approver token? Try your own sandbox" only when the playground is on
  try {
    const h = await (await fetch("/health")).json();
    if (h.playground?.mode === "on") $("try-link").hidden = false;
  } catch { /* stays hidden */ }
}

async function main() {
  const r = await fetch("/nexconn/config").catch(() => null);
  if (!r || !r.ok) { $("off").hidden = false; $("join").hidden = true; $("chat-mode").textContent = "Chat off"; return; }
  state.cfg = await r.json();
  renderMode();
  for (const a of state.cfg.approver_ids) $("as-user").append(el("option", {value: a}, a));
  $("token").value = localStorage.getItem("approverToken") || "";
  $("join-form").addEventListener("submit", join);
  $("reconnect-btn").addEventListener("click", () => location.reload());
  $("composer").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const t = $("text").value.trim();
    if (!t) return;
    try { await send(t, null); $("text").value = ""; } catch (e) { window.alert(`Could not send: ${e.message}`); }
  });
  showTryLink();
  setInterval(() => { if (!document.hidden) pollConfig(); }, 5000);
}

main();
