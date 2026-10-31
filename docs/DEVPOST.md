# Devpost submission draft — GuardRail Buyer

> Draft for the PayPal AI Hackathon form. Paste section by section. Contains no secrets.
> Repo and video links filled in on 2026-10-31.

**Project name:** GuardRail Buyer

**Tagline (≤ 200 chars):** AI agents that pay with PayPal, but only inside the spend guardrails you set: caps, human approval over limits, hard denies no model can talk past. Built-in agent or any MCP client.

**Suggested categories / prizes:** Best Use of Agentic Commerce · Best Use of PayPal + AI · Best Use of AG Grid

---

## Inspiration

AI agents are getting good at doing errands, and buying things is the obvious next errand. But today
the choice is all or nothing: either the agent can't pay, or it holds credentials that let it spend
anything. Small teams, office managers and ops people want the convenience of "just order what the
new hire needs" without waking up to a surprise bill or a gift-card scam the model fell for. We wanted
the same kind of controls a company card has (limits, approvals, blocked merchants) for an AI agent
that pays with PayPal.

## What it does

You describe what you need in plain language ("2 USB-C cables and a mechanical keyboard for our new
hire, and a $100 gift card as a welcome present"). The agent (Claude, using tool calls) finds the items
in a catalog, dry-runs the spend policy and places orders, and every order passes a **deterministic
policy engine on the server** before it reaches PayPal:

- **ALLOW** (under the per-order cap and today's budget) → the amount is **reserved** against the daily
  budget in the same database transaction as the check, then a real **PayPal Orders v2** order is created
  in the Sandbox. The buyer approves on PayPal, PayPal redirects back, and the app **captures** the payment.
- **NEEDS APPROVAL** (over a cap) → the agent files an approval request with a business justification;
  a human approves or rejects it in the dashboard (approver token required). Approval re-checks the
  deny rules before a PayPal order is created.
- **DENY** (blocked merchant or category, wrong currency, above the hard maximum) → refused, and a
  denial can never be escalated to approval.

The AG Grid dashboard shows pending approvals (with the agent's justification and the exact rule hit),
every order with its PayPal status and a **Pay with PayPal** button, and spend vs. caps (captured,
reserved, headroom). The **Audit** tab lists every guardrail decision (allow / approval / deny, the rule
hit, amount, merchant, request id) so a finance lead can filter, search and export them to CSV, and
click any row to see the quote, each rule evaluated and the payment timeline. The **Policy & spend** tab
lets the owner edit the caps and the merchant/category allowlists right in AG Grid (owner token, every
change validated again by the server and kept in a change history) and shows spend over time against the
daily cap (AG Charts).

**Bring your own agent (MCP).** The same guardrailed tools are an MCP server, so Claude Desktop, Codex or
any MCP client can shop through GuardRail: `search_catalog`, `check_policy`, `create_order`,
`request_approval`, `get_order_status`, `get_approval_status`, `get_spend_summary`. The MCP side never
gets PayPal credentials or the approver token, can only name a catalog product and a quantity (an extra
`price` or `merchant` argument is refused), has no approve/capture tool, and every money-moving call goes
through the same server-side policy gate. In a live run, a real model (via Codex) was told to follow a
prompt injection (buy a gift card from a blocked merchant, a $640 chair over the hard max, pass
`price=0.01`, approve itself): every attempt was refused by the server, while the legitimate order went
through.

*The demo video shows the dashboard flow with the built-in agent; the MCP server and the Policy & spend tab are in
the current build but are not demonstrated in the video (README: "Use it from any MCP agent" and the Policy & spend
tab, repro steps 10–11).*

## How we built it

- **Backend:** Python, FastAPI, SQLite. A pure guardrail engine (`evaluate(policy, order, spent_today)`)
  decides ALLOW / NEEDS_APPROVAL / DENY with human-readable reasons; `BuyerService` is the only code
  path that can create PayPal orders and re-evaluates the policy on every call.
- **PayPal:** Orders v2 in the Sandbox: create with `intent: CAPTURE` and `PayPal-Request-Id`
  idempotency, `payer-action` approval link, return/cancel URLs, live `GET` status, capture after
  approval, structured error handling (e.g. a capture before approval returns `422 ORDER_NOT_APPROVED`,
  which the app turns into `409 NOT_APPROVED_YET` without touching the order or the budget).
- **Spend accounting:** the budget check and the reservation run in one SQLite `BEGIN IMMEDIATE`
  transaction *before* PayPal is called, so concurrent requests can't all pass against the same budget.
  Order status changes are compare-and-set updates with a `CAPTURING` state that cancel/expiry can't
  override. Ambiguous PayPal outcomes (5xx, timeouts, capture `PENDING`) keep the reservation and are
  reconciled from PayPal's order status; only a confirmed `COMPLETED` capture counts as paid. Approvals are
  claimed once and bound to the exact quote (product, quantity, amount, currency, merchant).
- **AI:** Claude (`claude-sonnet-5-5`) through the Anthropic Messages API with four tools:
  `search_catalog`, `check_policy`, `create_order`, `request_approval`. The model never sees PayPal
  credentials. Bring your own model: `LLM_PROVIDER=openai` speaks OpenAI-compatible Chat Completions
  with tool calling (OpenAI, OpenRouter, DeepSeek, vLLM, Ollama), with the same tools behind the same policy.
- **MCP server:** `python -m app.mcp_server`, stdio JSON-RPC 2.0 (MCP revisions 2025-06-18 / 2025-03-26 /
  2024-11-05), no SDK dependency, checked against the official MCP Python SDK. Remote mode forwards each
  call to the GuardRail server (`POST /tools/{name}`), where a strict schema gateway (`GuardedTools`,
  `additionalProperties: false`, no coercion) hands it to `BuyerService`; embedded mode is a keyless
  offline sandbox. The process scrubs PayPal credentials and the approver token from its environment, and
  external orders are grouped server-side per policy day, so splitting a purchase (even across sessions)
  still counts as one purchase. We evaluated PayPal's Agent
  Toolkit / MCP server and chose to wrap Orders v2 ourselves: those tools give the agent PayPal authority
  and model-chosen amounts, which our gate exists to prevent (details in the README).
- **Dashboard:** plain HTML/JS with **AG Grid Community** 32.3.3 and **AG Charts Community** 10.3.3, vendored (approvals grid with action
  buttons, orders grid with PayPal links, spend-vs-caps grid, and an **Audit** grid of every guardrail
  decision: column filters, quick search, risk highlighting, CSV export, and a detail panel with the
  quote, every rule evaluated and the payment's status timeline; a **Policy & spend** tab with editable
  policy grids (`agTextCellEditor` / `agSelectCellEditor`, dirty-cell highlighting, owner-token save to
  `PUT /policy` with server-side validation and an `If-Match` revision check that answers 409 when another
  owner saved first, change history) and a spend-over-time chart).
- **Offline mode:** `AGENT_MODE=offline` replaces Claude with a small hand-written rule parser (clearly
  labelled, not AI) so anyone can run the full flow without an AI key.
- **Tests:** 400+ automated pytest tests (421 after the dashboard polish on 2026-10-30: 415 run with Python alone; 3 dashboard-logic tests need node, 2 headless UI
  tests need node + jsdom, 1 interop test needs the official MCP SDK): guardrails, payment flow, reservations/expiry, ambiguous PayPal outcomes and
  reconciliation, API, agent loop with a scripted fake LLM, offline mode, thread/barrier concurrency tests
  for budget races, double approvals and cancel-vs-capture, MCP protocol + end-to-end stdio sessions
  (embedded and against a running server, prompt-injection attempts included), policy-edit validation, plus
  a live Sandbox smoke script.

## Challenges we ran into

- **Making the limit real, not advisory.** A prompt that says "don't spend more than $50" isn't a
  control. We moved every decision server-side and gave the model tools only, then made sure approvals
  re-check deny rules in case the policy changed in between.
- **Unpaid orders vs. daily caps.** With a buyer-approval flow, money moves later than the order is
  created. Counting only captured payments would let an agent open ten $49 orders at once. We reserve
  at creation and release on cancel/expiry/capture failure.
- **PayPal edge cases:** idempotent capture on a refreshed return URL, a lost redirect (manual capture
  endpoint), captures attempted before approval, expiring stale orders, and responses lost after PayPal
  may already have captured.
- **Races.** An independent review reproduced concurrent requests passing the daily cap together, one
  approval producing two orders, and a cancel redirect erasing a capture in progress. We fixed each with
  atomic SQLite transactions and conditional status updates, and added concurrency regression tests.
- **LLM output quirks:** occasionally an empty final message after parallel tool calls; we added a
  deterministic summary built from the tool results.

## Accomplishments that we're proud of

- Natural-language request → policy → real PayPal Sandbox order works live, together with approvals
  and refusals in the same run; live Sandbox status, capture-before-approval refusal and cancel are
  verified (`docs/sandbox_smoke_run.txt`). The full loop was verified for real on 2026-10-30: Claude placed
  the order, a Sandbox personal test buyer logged in and paid on PayPal, PayPal redirected back, and the app
  captured it. App and PayPal both report `COMPLETED`, capture `8VA9873790040992M` (`docs/SANDBOX_E2E.md`,
  with screenshots).
- The guardrails are boring in the best way: deterministic and tested. The model can only call four
  tools; it never chooses prices or merchants (they come from the server catalog), cannot escalate a
  DENY, cannot reuse an approval or change its amount, and cannot get around the caps by firing
  requests in parallel or by splitting one item into several orders within a request.
- The demo video (2:18) is a live run of the current dashboard against the real PayPal Sandbox with Claude, and it
  ends with a clip from a second real run recorded earlier the same day and labelled on screen (order `ORD-F79F379F1E`
  → PayPal capture `14579671MH105231J`, `COMPLETED`): the Sandbox buyer logs in and pays on PayPal.
- Any MCP agent can now be given a budget instead of a wallet: a live Codex session that deliberately tried
  every injected instruction got `DENIED` / `INVALID_ARGUMENTS` from the server for each one
  (`docs/MCP_LIVE_SMOKE.md`).
- Judges can run everything locally in a few minutes, with or without an AI key and with or without a
  PayPal Sandbox app (a local mock covers the PayPal side).

**Known limitations (honest scope):** authentication is local-demo scope (one shared approver token that
fails closed when unset, a signed cancel callback and a simple per-IP agent rate limit, but no
per-requester identity or ownership, so don't expose it publicly); catalog merchants are labels and all
payments go to the single Sandbox merchant of the configured app; spend counts on the day it was reserved;
policy changes don't revoke orders already created; split-order aggregation covers only the same item
from the same merchant (within one built-in agent request, or one day for external MCP agents); mock PayPal state is in memory (one mock server per database, enforced by a lock file); the MCP/`/tools`
surface has the same local-demo scope (no per-client auth; split-order aggregation per SKU, merchant and
day); policy edits use one owner token. Details in the README.

## What we learned

- Agentic commerce needs the same primitives as corporate spend management (limits, approvals,
  allowlists, audit trail), and they belong outside the model.
- PayPal's Orders v2 buyer-approval step is a natural second safety layer: the agent can create an
  order, but a person still confirms the payment.

## What's next for GuardRail Buyer

- PayPal webhooks (`CHECKOUT.ORDER.APPROVED`, `PAYMENT.CAPTURE.COMPLETED`) instead of relying on the
  return redirect and on-demand reconciliation.
- Requester authentication, ownership and rate limits; mapping catalog merchants to real payees.
- Vaulted payment methods for trusted, pre-approved recurring purchases within caps; PayPal Agent Toolkit
  for read-only / merchant-side tasks behind the same gateway.
- Per-requester budgets, multiple approvers, and Slack/email approval links.
- Real merchant catalogs instead of the demo catalog.

## Built with

python · fastapi · sqlite · paypal-orders-api (v2, Sandbox) · anthropic-claude (Messages API, tool use) · model-context-protocol (MCP) · ag-grid · ag-charts · javascript · html/css · pytest · httpx · playwright (demo recording) · ffmpeg

## AI tools used (disclosure)

- **Claude (Anthropic), model `claude-sonnet-5-5`** — the runtime purchasing agent (tool use via the
  Messages API). It only calls our four tools; all spending decisions are made by deterministic code.
- **Hark** (an AI agent built on Claude) was used as a coding assistant to scaffold the backend,
  guardrail engine, tests, dashboard, docs and the demo-recording pipeline. All code was reviewed and
  verified by running the test suite, live Sandbox calls and live agent runs.
- **OpenAI Codex (CLI)** was used as an independent code reviewer (correctness, security, test gaps,
  docs drift); its review rounds and how each finding was fixed are in `docs/CODEX_REVIEW_*.md` and
  `docs/REVIEW_LOG.md`. It was also used as an MCP client (a real tool-calling model) for the live
  prompt-injection run against the MCP server (`docs/MCP_LIVE_SMOKE.md`).
- **Piper TTS** (offline, open source; voice trained on LibriTTS-R, CC BY 4.0) generated the demo
  video narration. No music is used.
- The optional `AGENT_MODE=offline` parser is hand-written rules, not an AI model.

## Testing instructions for judges

Everything runs locally; no account is needed for the default mode.

```bash
git clone https://github.com/mubudot/guardrail-buyer paypal-guardrail-buyer && cd paypal-guardrail-buyer
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Full flow without any key (mock PayPal + offline rule-based agent); see the README's offline block
export AGENT_MODE=offline PAYPAL_MODE=mock PAYPAL_MOCK_APPROVAL=1 APPROVER_TOKEN=demo GRB_NO_DOTENV=1
export DB_PATH="$(mktemp -d)/guardrail-demo.db"
python -m pytest -q                      # 418 passed, 3 skipped (2 jsdom UI tests, MCP SDK interop), no network
uvicorn app.main:app --port 8000
# open http://localhost:8000 , enter approver token "demo", then ask:
#   Buy 2 USB-C charging cables and a mechanical keyboard for our new hire, and also a $100 gift card
# → cables: order + "Pay with PayPal" (local mock checkout) · keyboard: approval request · gift card: refused
# Policy & spend tab: double-click "Per-order cap", type 100, Save policy (owner token blank = "demo")
```

- **From an MCP agent:** with the server running, add the MCP server to Claude Desktop or Codex with
  `GRB_API_URL=http://127.0.0.1:8000` (copy-paste config in the README, "Use it from any MCP agent").
  Without any client: the README's 4-line `printf … | python -m app.mcp_server` shows an order placed, a
  blocked merchant denied and a `price` override refused, fully offline.

- **Switching modes:** the block above exports `AGENT_MODE`, `PAYPAL_MODE` and `GRB_NO_DOTENV=1`, which
  override and disable `.env`. Stop the server and open a **new shell** before the two options below
  (or run the `unset …` line from the README's *Switching modes*).
- **With Claude:** put an Anthropic API key in `.env` (`ANTHROPIC_API_KEY`) and leave `AGENT_MODE`
  unset; free-form requests then work.
- **With your own model:** any OpenAI-compatible endpoint works too: `LLM_PROVIDER=openai`,
  `OPENAI_BASE_URL` (default `https://api.openai.com/v1`), `OPENAI_API_KEY`, `OPENAI_MODEL` (README:
  *Bring your own model*).
- **With the real PayPal Sandbox:** create a free Sandbox REST app at developer.paypal.com (Apps &
  Credentials → Sandbox), set `PAYPAL_MODE=sandbox`, `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` in
  `.env`, and pay with a Sandbox *personal* test account (Testing Tools → Sandbox Accounts).
  `python scripts/sandbox_smoke.py` checks the live Sandbox integration without a buyer login.
- No real money is involved anywhere; the PayPal side is Sandbox or a local mock.

**Repo:** `https://github.com/mubudot/guardrail-buyer` (MIT) · **Video:** `https://youtu.be/6pp8X-yFJwk`
