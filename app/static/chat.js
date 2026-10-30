// Approval chat page: join the Nexconn approval room as an approver, see the agent's approval
// cards, approve / reject them in the chat. All message text is rendered as text, never HTML.
import {createTransport} from "/static/chat-transport.js";

const $ = (id) => document.getElementById(id);
function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null) n.append(kid instanceof Node ? kid : String(kid));
  return n;
}

const state = {cfg: null, session: null, transport: null, seen: new Set(), outcomes: new Map(), cards: new Map()};
const APR = /^APR-[0-9A-F]{8}$/;
const COMMAND = /^\s*\/?(approve|reject)\s+(APR-[0-9A-F]{8})\s*[.!]?\s*$/i;  // same rule as the server

function showError(msg) { const e = $("join-error"); e.textContent = msg; e.hidden = !msg; }
function setConn(s) {
  const c = $("conn"); c.hidden = false;
  const ok = s === "connected";   // exact: "disconnected" must not read as connected
  c.textContent = ok ? "Connected" : `Connection: ${s}`;
  c.className = `chip ${ok ? "env-sandbox" : "warn"}`;
}

function who(uid) {
  if (!state.session) return uid;
  if (uid === state.session.bot_user_id) return "GuardRail agent";
  return uid === state.session.user_id ? `${uid} (you)` : uid;
}

function resolveCard(aid) {
  const card = state.cards.get(aid), out = state.outcomes.get(aid);
  if (!card || !out) return;
  card.querySelector(".card-actions")?.remove();   // decided: no buttons left to press
  const tag = card.querySelector(".card-state");
  const bad = !["AWAITING_PAYER_APPROVAL", "ORDER_PLACED", "PAYMENT_UNCONFIRMED"].includes(out.result);
  tag.textContent = bad ? "Closed: not bought" : "Closed: approved";
  tag.className = `card-state ${bad ? "tone-bad" : "tone-ok"}`;
}

async function decide(aid, decision, buttons) {
  const verb = decision === "approve" ? "Approve" : "Reject";
  if (!window.confirm(`${verb} ${aid}? This is posted in the room and runs the guardrail check now.`)) return;
  buttons.forEach((b) => { b.disabled = true; });
  try {
    await send(`${decision} ${aid}`, {grb: "decision", approval_id: aid, decision});
  } catch (e) {
    buttons.forEach((b) => { b.disabled = false; });
    window.alert(`Could not send: ${e.message}`);
  }
}

function renderMessage(m) {
  if (!m.id || state.seen.has(m.id)) return;
  state.seen.add(m.id);
  const x = m.extra || {};
  const mine = state.session && m.from === state.session.user_id;
  const bot = state.session && m.from === state.session.bot_user_id;
  const time = m.time ? new Date(m.time).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}) : "";
  const meta = el("div", {class: "msg-meta"}, el("strong", {}, who(m.from)), time ? ` · ${time}` : "");
  let body;
  if (bot && x.grb === "approval_request" && APR.test(x.approval_id || "")) {
    const aid = x.approval_id;
    const approve = el("button", {class: "btn ok", type: "button"}, "Approve");
    const reject = el("button", {class: "btn bad", type: "button"}, "Reject");
    approve.addEventListener("click", () => decide(aid, "approve", [approve, reject]));
    reject.addEventListener("click", () => decide(aid, "reject", [approve, reject]));
    body = el("div", {class: "approval-card", "data-approval": aid},
      el("div", {class: "card-head"}, el("span", {class: "card-id"}, aid),
         el("span", {class: "card-state tone-warn"}, "Waiting for a decision")),
      el("pre", {class: "msg-text"}, m.text),
      el("div", {class: "card-actions"}, approve, reject));
    state.cards.set(aid, body);
  } else {
    body = el("pre", {class: "msg-text"}, m.text);
  }
  if (bot && x.grb === "outcome" && APR.test(x.approval_id || "")) {
    state.outcomes.set(x.approval_id, x);
  }
  const li = el("li", {class: `msg ${mine ? "mine" : ""} ${bot ? "bot" : ""} ${x.grb ? `k-${x.grb}` : ""}`}, meta, body);
  $("messages").append(li);
  if (x.grb === "outcome") resolveCard(x.approval_id);
  if (x.grb === "approval_request") resolveCard(x.approval_id);
}

function onMessages(list) {
  const box = $("messages");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  list.sort((a, b) => (a.time || 0) - (b.time || 0)).forEach(renderMessage);
  if (atBottom || list.length > 1) box.scrollTop = box.scrollHeight;
}

const isDecision = (text, extra) => (extra && extra.grb === "decision") || COMMAND.test(text);

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
    $("join").hidden = true;
    $("room").hidden = false;
    $("text").focus();
  } catch (e) {
    try { state.transport?.stop(); } catch { /* already stopped */ }
    state.transport = null;
    state.seen.clear(); state.cards.clear(); state.outcomes.clear();
    $("messages").replaceChildren();
    $("room").hidden = true;
    $("join").hidden = false;
    showError(e.message || String(e));
    $("join-btn").disabled = false;
  }
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
}

main();
