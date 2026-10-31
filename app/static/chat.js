// Approval chat page: join the Nexconn approval room as an approver, see the agent's approval
// cards, approve / reject them in the chat. All message text is rendered as text, never HTML.
// The room itself (message list, approval cards, reconcile on join) lives in chat-room.js, shared
// with the /try playground.
import {createTransport} from "/static/chat-transport.js";
import {createRoom, el, isDecision} from "/static/chat-room.js";

const $ = (id) => document.getElementById(id);

const state = {cfg: null, session: null, transport: null};
const room = createRoom({
  list: $("messages"), older: $("older-pending"), session: () => state.session, send: (t, x) => send(t, x),
  fetchApprovals: async () => { const r = await fetch("/approvals"); return r.ok ? r.json() : null; },
});
const {renderMessage, onMessages, syncApprovals} = room;

function showError(msg) { const e = $("join-error"); e.textContent = msg; e.hidden = !msg; }
function setConn(s) {
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
  ev.preventDefault();
  showError("");
  $("join-btn").disabled = true;
  try {
    const r = await fetch(`/nexconn/session?as_user=${encodeURIComponent($("as-user").value)}`,
                          {method: "POST", headers: authHeaders()});
    if (!r.ok) {
      const detail = (await r.json().catch(() => ({}))).detail;
      throw new Error(r.status === 401 ? "Wrong approver token." : `Could not join (HTTP ${r.status}${detail ? `: ${detail}` : ""}).`);
    }
    state.session = await r.json();
    localStorage.setItem("approverToken", $("token").value);
    state.transport = await createTransport(state.session);
    $("room-name").textContent = `Approval room · ${state.session.group_id}`;
    $("room-who").textContent = `You are ${state.session.user_id}. Approvers: ${state.session.approver_ids.join(", ")}`;
    await state.transport.start(onMessages, setConn);   // show the room only once connected
    await syncApprovals();
    $("join").hidden = true;
    $("room").hidden = false;
    $("text").focus();
  } catch (e) {
    try { state.transport?.stop(); } catch { /* already stopped */ }
    state.transport = null;
    room.clear();
    $("room").hidden = true;
    $("join").hidden = false;
    showError(e.message || String(e));
    $("join-btn").disabled = false;
  }
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
  const m = $("chat-mode");
  m.textContent = state.cfg.mode === "live" ? `Nexconn live · ${state.cfg.area}` : "Nexconn mock (offline)";
  m.className = `chip ${state.cfg.mode === "live" ? "env-sandbox" : "env-mock"}`;
  m.title = state.cfg.mode === "live" ? `Nexconn Web Chat SDK ${state.cfg.sdk_version}` :
    "In-memory stand-in for Nexconn: no account, no network";
  for (const a of state.cfg.approver_ids) $("as-user").append(el("option", {value: a}, a));
  $("token").value = localStorage.getItem("approverToken") || "";
  $("join-form").addEventListener("submit", join);
  $("composer").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const t = $("text").value.trim();
    if (!t) return;
    try { await send(t, null); $("text").value = ""; } catch (e) { window.alert(`Could not send: ${e.message}`); }
  });
  showTryLink();
}

main();
