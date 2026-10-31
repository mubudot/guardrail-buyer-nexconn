// The approval room's message list, shared by the approver chat page (chat.js) and the /try playground
// (try.js): renders messages as text (never HTML), turns the agent's approval requests into cards with
// Approve / Reject, closes cards when their outcome arrives (on every rendered copy: an at-least-once re-send
// can post the same card twice), and reconciles with the server's approval list on join.
import {planReconcile, closedLabel} from "/static/chat-reconcile.js";

export function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null) n.append(kid instanceof Node ? kid : String(kid));
  return n;
}

const APR = /^APR-[0-9A-F]{8}$/;
export const COMMAND = /^\s*\/?(approve|reject)\s+(APR-[0-9A-F]{8})\s*[.!]?\s*$/i;  // same rule as the server
const KINDS = new Set(["approval_request", "outcome", "notice", "decision"]);  // known `extra.grb` kinds
export const isDecision = (text, extra) => (extra && extra.grb === "decision") || COMMAND.test(text);

// opts: list (the <ol>), older (the "older pending" box), session() -> the Chat SDK session,
//       send(text, extra) -> Promise, fetchApprovals() -> Promise<list|null>,
//       names: optional Map user id -> display name, onCard(aid, card): a new card was rendered.
export function createRoom(opts) {
  const state = {seen: new Set(), outcomes: new Map(), cards: new Map(),
                 closed: new Map()};   // approval id -> server status, for cards closed by syncApprovals()
  const $list = opts.list, $older = opts.older;
  const confirmFn = opts.confirm || ((m) => window.confirm(m));
  const alertFn = opts.alert || ((m) => window.alert(m));

  function who(uid) {
    const s = opts.session();
    if (!s) return uid;
    if (uid === s.bot_user_id) return "GuardRail agent";
    const name = opts.names?.get(uid) || uid;
    return uid === s.user_id ? `${name} (you)` : name;
  }

  function resolveCard(aid) {
    // every rendered copy of the card (an at-least-once re-send can post the same request twice)
    const cards = state.cards.get(aid), out = state.outcomes.get(aid);
    if (!cards || !out) return;
    const bad = !["AWAITING_PAYER_APPROVAL", "ORDER_PLACED", "PAYMENT_UNCONFIRMED"].includes(out.result);
    for (const card of cards) {
      card.querySelector(".card-actions")?.remove();   // decided: no buttons left to press
      const tag = card.querySelector(".card-state");
      tag.textContent = bad ? "Closed: not bought" : "Closed: approved";
      tag.className = `card-state ${bad ? "tone-bad" : "tone-ok"}`;
    }
  }

  async function decide(aid, decision, buttons) {
    const verb = decision === "approve" ? "Approve" : "Reject";
    if (!confirmFn(`${verb} ${aid}? This is posted in the room and runs the guardrail check now.`)) return;
    buttons.forEach((b) => { b.disabled = true; });
    try {
      await opts.send(`${decision} ${aid}`, {grb: "decision", approval_id: aid, decision});
    } catch (e) {
      buttons.forEach((b) => { b.disabled = false; });
      alertFn(`Could not send: ${e.message}`);
    }
  }

  function approvalCard(aid, text) {
    const approve = el("button", {class: "btn ok", type: "button"}, "Approve");
    const reject = el("button", {class: "btn bad", type: "button"}, "Reject");
    approve.addEventListener("click", () => decide(aid, "approve", [approve, reject]));
    reject.addEventListener("click", () => decide(aid, "reject", [approve, reject]));
    const card = el("div", {class: "approval-card", "data-approval": aid},
      el("div", {class: "card-head"}, el("span", {class: "card-id"}, aid),
         el("span", {class: "card-state tone-warn"}, "Waiting for a decision")),
      el("pre", {class: "msg-text"}, text),
      el("div", {class: "card-actions"}, approve, reject));
    state.cards.set(aid, [...(state.cards.get(aid) || []), card]);
    return card;
  }

  // On join the page checks the server's approval list (it loads only the latest 50 room messages):
  // pending approvals whose cards are older than that history are listed above the room with the same
  // buttons, so no open request is out of reach, and a card in view whose decision is further back in the
  // history is closed. Deciding still goes through the chat (decision message + signed webhook).
  function dropOlderPending(aid) {
    $older?.querySelector(`[data-older="${aid}"]`)?.remove();
    if ($older && !$older.querySelector("[data-older]")) $older.hidden = true;
  }

  function closeCard(aid, status, label) {
    state.closed.set(aid, status);   // later copies of this card (a re-sent delivery) arrive closed too
    const {text, tone} = label || closedLabel(status);
    for (const card of state.cards.get(aid) || []) {
      card.querySelector(".card-actions")?.remove();
      const tag = card.querySelector(".card-state");
      tag.textContent = text;
      tag.className = `card-state ${tone}`;
    }
  }

  async function syncApprovals() {
    let list = null;
    try { list = await opts.fetchApprovals(); } catch { return; }
    if (!$older || !Array.isArray(list)) return;
    const {older: missing, close, decided} = planReconcile(list, state.cards.keys(), state.outcomes.keys());
    decided.forEach(({id, status}) => state.closed.set(id, status));   // applied to card copies delivered later
    close.forEach(({id, status}) => closeCard(id, status));
    $older.replaceChildren();
    if (!missing.length) { $older.hidden = true; return; }
    $older.append(el("div", {class: "msg-meta"}, el("strong", {}, "Older pending approvals"),
                     " · their cards are further back in the room history"));
    for (const a of missing) {
      const text = `${a.quantity} × ${a.product_name} from ${a.merchant} = ${a.amount} ${a.currency}` +
                   (a.justification ? `\nAgent's reason: ${a.justification}` : "");
      $older.append(el("div", {class: "older-item", "data-older": a.id}, approvalCard(a.id, text)));
    }
    $older.hidden = false;
  }

  // The playground's "Sandbox was reset" notice: every card rendered so far belongs to the wiped data.
  function closeAllOpen() {
    for (const aid of state.cards.keys()) {
      if (!state.outcomes.has(aid)) closeCard(aid, "RESET", {text: "Closed: sandbox reset", tone: "tone-bad"});
    }
  }

  function renderMessage(m) {
    if (!m.id || state.seen.has(m.id)) return;
    state.seen.add(m.id);
    const x = m.extra || {};
    const s = opts.session();
    const mine = s && m.from === s.user_id;
    const bot = s && m.from === s.bot_user_id;
    const time = m.time ? new Date(m.time).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}) : "";
    const meta = el("div", {class: "msg-meta"}, el("strong", {}, who(m.from)), time ? ` · ${time}` : "");
    let body, card = null;
    if (bot && x.grb === "approval_request" && APR.test(x.approval_id || "")) {
      body = card = approvalCard(x.approval_id, m.text);
      dropOlderPending(x.approval_id);
      if (state.closed.has(x.approval_id)) closeCard(x.approval_id, state.closed.get(x.approval_id));
    } else {
      body = el("pre", {class: "msg-text"}, m.text);
    }
    if (bot && x.grb === "outcome" && APR.test(x.approval_id || "")) {
      state.outcomes.set(x.approval_id, x);
    }
    const kind = KINDS.has(x.grb) ? `k-${x.grb}` : "";
    const li = el("li", {class: `msg ${mine ? "mine" : ""} ${bot ? "bot" : ""} ${kind}`}, meta, body);
    $list.append(li);
    if (x.grb === "outcome") resolveCard(x.approval_id);
    if (x.grb === "approval_request") resolveCard(x.approval_id);
    if (bot && x.grb === "notice" && x.reset === true) closeAllOpen();
    if (card && opts.onCard) opts.onCard(x.approval_id, card, li);
  }

  function onMessages(list) {
    const atBottom = $list.scrollHeight - $list.scrollTop - $list.clientHeight < 60;
    list.sort((a, b) => (a.time || 0) - (b.time || 0)).forEach(renderMessage);
    if (atBottom || list.length > 1) $list.scrollTop = $list.scrollHeight;
  }

  function clear() {
    state.seen.clear(); state.cards.clear(); state.outcomes.clear(); state.closed.clear();
    $list.replaceChildren();
    if ($older) { $older.replaceChildren(); $older.hidden = true; }
  }

  return {state, renderMessage, onMessages, syncApprovals, clear};
}
