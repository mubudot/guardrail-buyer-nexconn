// /try playground page: start (or resume, or join) a private sandbox, ask the rule-based agent, approve or
// reject its requests in the sandbox's own Nexconn room, pay on the simulated checkout, invite a teammate.
// Every piece of server or room text is rendered as text, never HTML.
import {createTransport} from "/static/chat-transport.js";
import {createRoom, el} from "/static/chat-room.js";
import {describeResponse, friendlyError} from "/static/friendly-errors.js";
import {dailyProgress, money, parseLocation, safePayUrl, stepView} from "/static/try-model.js";
import {OFFLINE_TOOLTIP, offlineView, sdkRetryDelay, watchSdk} from "/static/offline-model.js";
import {updateOfflineNotice} from "/static/offline-notice.js";

const $ = (id) => document.getElementById(id);
const KEY = "grbTrySession";
const POLL_MS = 4000;

const state = {secret: localStorage.getItem(KEY) || "", sandbox: null, session: null, transport: null,
               names: new Map(), poll: null};
const loc = parseLocation(location.pathname, location.search);
let roomStarting = null, roomGen = 0;   // the pending room start (shared by concurrent callers), room generation
let roomFailures = 0, roomRetryAt = 0;   // a failed room start is retried from the state poll, with backoff
let sdkWatch = null, sdkFallbackPending = false;

const room = createRoom({
  list: $("messages"), older: $("older-pending"), session: () => state.session, names: state.names,
  send: (t, x) => send(t, x),
  fetchApprovals: async () => (await api("/try/state")).approvals,   // fresh, not a shared in-flight poll
  onCard: (aid, card) => {
    card.classList.add("fresh");
    card.addEventListener("animationend", () => card.classList.remove("fresh"), {once: true});
    refresh();
  },
});

// ---- small UI helpers ---------------------------------------------------------------
let toastTimer = null;
function toast(msg, tone = "") {
  const t = $("toast");
  t.textContent = msg; t.className = `toast ${tone}`; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 6000);
}
function setConn(s) {
  sdkWatch?.status(s);
  const c = $("conn"); c.hidden = false;
  const ok = s === "connected";   // exact: "disconnected" must not read as connected
  c.textContent = ok ? "Connected" : `Connection: ${s}`;
  c.className = `chip ${ok ? "env-sandbox" : "warn"}`;
  // the offline stand-in forgets room tokens when the server restarts (401 -> "session expired"): get a
  // fresh room session; a transient "reconnecting" just keeps polling
  if (s === "session expired" && state.session?.mode === "mock" && !roomStarting) restartRoom();
}
function setChatMode(mode) {
  const m = $("chat-mode"); m.hidden = false;
  const live = mode === "live";
  m.textContent = live ? `Nexconn live · ${state.session?.area || "SG"}` : "Offline stand-in";
  m.className = `chip ${live ? "env-sandbox" : "offline"}`;
  const upstream = offlineView(state.sandbox).visible;
  m.title = live ? "Your own user and room on Nexconn (Web Chat SDK)" : upstream ? OFFLINE_TOOLTIP :
    "In-process stand-in for Nexconn: no account, no network";
  $("fallback-note").hidden = !(state.sandbox?.chat_fallback && state.sandbox?.fallback?.reason === "capacity");
  updateOfflineNotice($("offline-banner"), state.sandbox);
  $("try-sub").textContent = `Your own sandbox · ${live ? "real Nexconn chat" : "offline chat stand-in"} · simulated PayPal`;
  $("invite-hint").textContent = live ? "They open the link, get their own Nexconn user and join this room as a second approver."
    : "They open the link and join this room as a second approver.";
}
async function api(path, {method = "GET", body, context = "session"} = {}) {
  let r;
  try {
    r = await fetch(path, {method, headers: {"Content-Type": "application/json", "X-Try-Session": state.secret},
                           body: body ? JSON.stringify(body) : undefined});
  } catch {
    throw Object.assign(new Error(friendlyError({offline: true})), {status: 0});
  }
  if (!r.ok) throw Object.assign(new Error(await describeResponse(r, context)), {status: r.status});
  return r.json();
}

// ---- hero (start / join) -------------------------------------------------------------
function showHero(notice = "") {
  $("workspace").hidden = true;
  $("hero").hidden = false;
  if (loc.invite) {
    $("hero-title").textContent = "You're invited to approve purchases";
    $("hero-lede").textContent = "A teammate's AI purchasing agent asks before it spends over its limit. Join their " +
      "private Nexconn chat room as a second approver: you get your own Nexconn user, no sign-up.";
    $("start-btn").textContent = "Join as a teammate";
    if (!loc.code) notice = notice || "This invite link looks incomplete. Ask your teammate to copy it again.";
    $("start-btn").disabled = !loc.code;
  }
  $("hero-notice").textContent = notice;
  $("hero-notice").hidden = !notice;
}

async function start(ev) {
  ev.preventDefault();
  const err = $("start-error"); err.hidden = true;
  const btn = $("start-btn"); btn.disabled = true;
  const name = $("name").value.trim() || null;
  try {
    const out = loc.invite ? await api("/try/join", {method: "POST", body: {code: loc.code || "", name}, context: "join"})
                           : await api("/try/start", {method: "POST", body: {name}, context: "start"});
    state.secret = out.secret;
    localStorage.setItem(KEY, out.secret);
    history.replaceState(null, "", "/try");
    loc.invite = false;
    await enter();
  } catch (e) {
    err.textContent = e.message; err.hidden = false;
  } finally {
    btn.disabled = false;
  }
}

// ---- the sandbox --------------------------------------------------------------------------
async function enter() {
  const s = await refresh(true);
  if (!s) return;
  $("hero").hidden = true;
  $("workspace").hidden = false;
  $("reset-btn").hidden = s.you.role !== "owner";
  await startRoom();
  document.querySelector(".try-chip")?.focus();
  clearInterval(state.poll);
  state.poll = setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
}

let refreshing = null, refreshSeq = 0;
async function refresh(first = false) {
  if (refreshing) return refreshing;   // one /try/state call at a time; callers share its result
  const seq = ++refreshSeq;
  refreshing = (async () => {
    try {
      const s = await api("/try/state");
      if (seq !== refreshSeq) return null;   // the sandbox changed meanwhile (expired / new one): ignore
      state.sandbox = s;
      render(s);
      if (state.session && state.session.mode !== s.chat_mode) restartRoom();
      // A room start that failed (busy sandbox, Nexconn refusing) left no session: try again, backing off.
      else if (!state.session && !roomStarting && state.secret && Date.now() >= roomRetryAt) startRoom();
      return s;
    } catch (e) {
      if (seq !== refreshSeq) return null;
      if (e.status === 404 || e.status === 401) {     // expired or unknown: back to the start card
        expired(e.message);
      } else if (first) {
        showHero(e.message);
      }
      return null;
    } finally {
      if (seq === refreshSeq) refreshing = null;
    }
  })();
  return refreshing;
}

function expired(msg) {
  clearInterval(state.poll);
  refreshSeq++; refreshing = null;   // an in-flight /try/state of this sandbox no longer counts
  roomGen++; roomStarting = null;     // nor an in-flight room start
  roomFailures = 0; roomRetryAt = 0;
  sdkWatch?.stop(); sdkWatch = null;
  try { state.transport?.stop(); } catch { /* already stopped */ }
  state.transport = null; state.session = null; state.secret = "";
  localStorage.removeItem(KEY);
  room.clear();
  $("conn").hidden = true; $("chat-mode").hidden = true;
  showHero(msg);
  $("name").focus();
}

function render(s) {
  setChatMode(s.chat_mode);
  for (const m of s.members) state.names.set(m.user_id, m.name);
  $("room-name").textContent = `Approval room · ${s.name}`;
  $("room-who").textContent = `${s.members.length} member${s.members.length === 1 ? "" : "s"} · you are ${s.you.name}`;
  const exp = new Date(s.expires_at);
  $("expires").textContent = Number.isNaN(exp.getTime()) ? "" :
    `kept until ${exp.toLocaleString([], {weekday: "short", hour: "2-digit", minute: "2-digit"})} if idle`;
  for (const v of stepView(s.steps)) {
    const li = document.querySelector(`[data-step="${v.key}"]`);
    if (!li) continue;
    li.classList.toggle("done", v.done);
    li.classList.toggle("next", v.next);
    li.setAttribute("aria-label", `${li.firstChild.textContent.trim()}: ${v.done ? "done" : v.next ? "next step" : "to do"}`);
  }
  const L = s.limits, cur = L.currency;
  $("lim-order").textContent = money(L.per_order_cap, cur);
  $("lim-hard").textContent = money(L.hard_max_per_order, cur);
  $("lim-daily").textContent = `${money(L.spent_today, cur)} of ${money(L.daily_cap, cur)}`;
  const {pct, over} = dailyProgress(L);
  $("daily-fill").style.width = `${pct}%`;
  $("daily-fill").className = over ? "over" : "captured";
  $("daily-bar").setAttribute("aria-valuenow", String(pct));
  $("invite-url").value = s.invite_url.startsWith("/") ? `${location.origin}${s.invite_url}` : s.invite_url;
  $("members").replaceChildren(...s.members.map((m) => el("li", {},
    el("span", {}, m.name, m.user_id === s.you.user_id ? " (you)" : ""), el("span", {class: "role"}, m.role))));
  renderPayBox(s.orders);
}

function renderPayBox(orders) {
  const unpaid = (orders || []).filter((o) => safePayUrl(o.pay_url));
  const box = $("pay-box");
  box.replaceChildren();
  if (!unpaid.length) { box.hidden = true; return; }
  box.append(el("span", {class: "pay-label"}, "Waiting for payment"));
  for (const o of unpaid.slice(0, 4)) {
    // same tab: the checkout returns to /try, which resumes this sandbox (a second tab would connect the same
    // Nexconn user twice, and the SDK signs the first connection out)
    box.append(el("a", {class: "btn primary", href: safePayUrl(o.pay_url)},
                  `Pay on simulated checkout · ${o.quantity} × ${o.product_name} · ${money(o.amount, o.currency)}`));
  }
  box.hidden = false;
}

// ---- agent -----------------------------------------------------------------------------------
async function ask(text) {
  const t = (text || "").trim();
  if (!t) return;
  const ans = $("answer");
  const controls = [...document.querySelectorAll(".try-chip"), $("ask-btn")];
  controls.forEach((b) => { b.disabled = true; });
  ans.hidden = false; ans.className = "try-answer"; ans.textContent = "The agent is checking your limits…";
  try {
    const out = await api("/try/agent", {method: "POST", body: {request: t}});
    ans.textContent = out.final_text.replace(/^\[offline rule-based mode, no AI\]\n/, "");
    $("ask").value = "";
    await refresh();
  } catch (e) {
    ans.className = "try-answer err"; ans.textContent = e.message;
    if (e.status === 404 || e.status === 401) expired(e.message);
  } finally {
    controls.forEach((b) => { b.disabled = false; });
  }
}

// ---- room --------------------------------------------------------------------------------------
function startRoom() {
  if (roomStarting) return roomStarting;
  const gen = roomGen;
  const stale = () => gen !== roomGen;   // the sandbox expired / was replaced while this start was running
  const p = (async () => {
    try {
      const session = await api("/try/session", {method: "POST"});
      if (stale()) return;
      state.session = session;
      roomFailures = 0; roomRetryAt = 0;
      setChatMode(session.mode);
      sdkWatch?.stop();
      sdkWatch = session.mode === "live" ? watchSdk(() => fallbackForSdk()) : null;
      const transport = await createTransport(session, {mockBase: "/try/mock"});
      if (stale()) { transport.stop(); return; }
      state.transport = transport;
      await transport.start((messages) => { if (!stale()) room.onMessages(messages); },
                            (status) => { if (!stale()) setConn(status); });
      if (stale()) { transport.stop(); return; }
      await room.syncApprovals();
      refresh();
    } catch (e) {
      if (stale()) return;
      if (e.status === 404 || e.status === 401) { expired(e.message); return; }
      sdkWatch?.fail();
      setConn("not connected");
      if (!state.session) {   // no room at all: the state poll retries (4 s doubling, at most 60 s apart)
        roomRetryAt = Date.now() + Math.min(POLL_MS * 2 ** roomFailures, 60000);
        roomFailures++;
      }
      if (roomFailures <= 1) toast(e.status ? e.message : friendlyError({status: 502}), "bad");
    }
  })().finally(() => { if (roomStarting === p) roomStarting = null; });
  roomStarting = p;
  return p;
}

async function restartRoom() {
  roomGen++; roomStarting = null;
  sdkWatch?.stop(); sdkWatch = null;
  try { state.transport?.stop(); } catch { /* already stopped */ }
  state.transport = null; state.session = null;
  room.clear();
  await startRoom();
}

async function fallbackForSdk() {
  if (sdkFallbackPending) return;
  const gen = roomGen, secret = state.secret, current = () => gen === roomGen && secret === state.secret;
  sdkFallbackPending = true;
  try {
    // The watchdog fires once; a busy sandbox (409) or the rate limit (429) must not leave a dead live
    // chat behind, so retry those with backoff (honouring Retry-After) while this room is still current.
    for (let attempt = 0; ; attempt++) {
      let r = null;
      try {
        r = await fetch("/try/sdk-failed", {method: "POST",
          headers: {"Content-Type": "application/json", "X-Try-Session": secret}});
      } catch { /* network: retried below as status 0 */ }
      if (!current()) return;
      if (r?.ok) {
        const s = await r.json();
        if (!current()) return;
        state.sandbox = s; render(s);
        if (s.chat_mode === "mock") await restartRoom();
        return;
      }
      const status = r ? r.status : 0;
      const wait = sdkRetryDelay(status, r?.headers.get("Retry-After"), attempt);
      if (wait === null) {
        toast(r ? await describeResponse(r, "session") : friendlyError({offline: true}), "bad");
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, wait));
      if (!current()) return;
    }
  } finally { sdkFallbackPending = false; }
}

async function send(text, extra) {
  if (!state.transport) throw new Error("not connected to the room yet");
  const {messageId} = await state.transport.send(text, extra);
  if (state.session.mode === "live") {   // the SDK does not echo our own message back
    room.renderMessage({id: messageId, from: state.session.user_id, text, extra, time: Date.now()});
    const box = $("messages"); box.scrollTop = box.scrollHeight;
  }
  setTimeout(refresh, 800);   // the decision runs server side; show its result in the guide
}

// ---- wiring -------------------------------------------------------------------------------------
function returnToast() {
  if (loc.paid) toast(loc.status === "COMPLETED" ? `Paid (simulated): order ${loc.paid}` : `Order ${loc.paid}: ${loc.status}`,
                      loc.status === "COMPLETED" ? "ok" : "");
  else if (loc.cancelled) toast(`Checkout cancelled: order ${loc.cancelled} released, nothing was paid`);
  if (loc.paid || loc.cancelled) history.replaceState(null, "", "/try");
}

function wire() {
  $("new-live-btn").addEventListener("click", () => {
    if (!window.confirm("Leave this sandbox and start a new live sandbox? Your teammates stay in this sandbox.")) return;
    expired(""); loc.invite = false;
    $("start-form").requestSubmit();
  });
  $("start-form").addEventListener("submit", start);
  document.querySelectorAll(".try-chip").forEach((b) => b.addEventListener("click", () => ask(b.dataset.ask)));
  $("ask-form").addEventListener("submit", (ev) => { ev.preventDefault(); ask($("ask").value); });
  $("composer").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const t = $("text").value.trim();
    if (!t) return;
    try { await send(t, null); $("text").value = ""; } catch (e) { toast(`Could not send: ${e.message}`, "bad"); }
  });
  $("copy-btn").addEventListener("click", async () => {
    const input = $("invite-url");
    try { await navigator.clipboard.writeText(input.value); } catch { input.select(); document.execCommand?.("copy"); }
    $("copy-btn").textContent = "Copied";
    setTimeout(() => { $("copy-btn").textContent = "Copy"; }, 1800);
  });
  $("reset-btn").addEventListener("click", async () => {
    if (!window.confirm("Reset your sandbox? Orders, approvals and today's spend are cleared. Your room and teammates stay.")) return;
    try {
      await api("/try/reset", {method: "POST"});
      $("answer").hidden = true; $("answer").textContent = "";
      toast("Sandbox reset: fresh daily cap.", "ok");
      await refresh();
    }
    catch (e) { toast(e.message, "bad"); }
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.sandbox) refresh(); });
}

async function main() {
  wire();
  returnToast();
  if (loc.invite || !state.secret) { showHero(); $("name").focus(); return; }
  await enter();
}

main();
