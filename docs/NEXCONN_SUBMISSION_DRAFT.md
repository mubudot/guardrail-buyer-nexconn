# Nexconn Build Challenge 2026 — submission draft (GuardRail Buyer: approve AI purchases in chat)

*Draft, not submitted. Placeholders in `<…>` must be filled by the team leader before submitting on
reskilll.com/hack/nexconn. Deadline: Nov 22, 23:59 AoE (= Nov 23, 19:59 Taipei).*

## Form fields

| Field | Value |
|---|---|
| Project name | GuardRail Buyer — approve AI purchases in chat |
| Track | Open Innovation (closest example track: "Human agent dispatch center") |
| SDK used | Chat SDK (Web, `@nexconn/chat` 26.2.1) + Platform Chat API (server) — Call SDK not used |
| Runnable app link | https://guardrail-buyer-204-44-110-122.sslip.io/chat (dashboard: https://guardrail-buyer-204-44-110-122.sslip.io/ ; webhook: https://guardrail-buyer-204-44-110-122.sslip.io/nexconn/webhook, event `message:send`). Deployed 2026-10-31 from commit 5d96da4; until the App Secret / PayPal Sandbox keys are filled on the server it runs Nexconn **mock** + PayPal **mock** + offline agent. Approver token required — decide how judges get one before submitting. |
| Public repo | `<https://github.com/<owner>/<repo>` — repository is private; branch `nexconn`>` |
| Demo video | `<YouTube / Loom link — not recorded yet>` |
| Write-up PDF | export of the "Write-up" section below, with the App Key filled in |
| Nexconn App Key | `<App Key from console.nexconn.ai → Key Management — account not created yet>` |
| Traction (optional) | `<active users on the Nexconn app / screenshots, if any>` |

## Write-up (for the PDF)

### Why we built it

AI agents are starting to spend money. The safe pattern is a hard policy between the model and the wallet,
with a human approving anything above a cap. In practice the weak link is the approval itself: requests sit
in a dashboard nobody has open, so people either rubber-stamp them later or raise the caps until approvals
never happen. Approvals need to reach the people who decide, in the place where the team already talks.

### What it does

GuardRail Buyer is an AI purchasing agent that pays with PayPal (Sandbox) only inside spend guardrails a
deterministic policy engine enforces on the server: per-order cap, daily cap, hard maximum, merchant and
category allowlists. Anything over a soft cap needs a human.

For this challenge we added a **Nexconn approval room**:

1. When the agent hits a cap, the server posts an **approval card** into a Nexconn group channel as the
   agent's bot user: what, how much, which rule fired, and the agent's justification.
2. Approvers open the **approval chat** (`/chat`, built on the Nexconn Web Chat SDK), see the card in real time
   and tap **Approve** or **Reject** — or just type `approve APR-1A2B3C4D`.
3. The decision comes back to the server through Nexconn's signed `message:send` webhook and runs through the
   **same guarded code path as the dashboard**: hard rules are re-checked, the approval is bound to its price
   quote and can be claimed only once. Approval waives only the soft caps.
4. The outcome — PayPal order created, paid, refused by a hard rule, or rejected — is posted back into the
   room, with who decided and where. Decisions made on the dashboard are mirrored into the room too, so the chat
   shows every approval request and how it ended (the dashboard's audit log stays the full record of all
   guardrail decisions, including automatic in-policy purchases and denials).

### Features that use Nexconn

| Feature | Nexconn product / API |
|---|---|
| Approval room members: bot user + approver users, registered on startup | Platform Chat API `POST /v4/auth/access-token/issue` |
| The approval room itself | Platform Chat API `POST /v4/group-channel/create`, `/v4/group-channel/join` |
| Approval cards and outcome messages (text + structured `extra`) | Platform Chat API `POST /v4/group-channel/message/send` |
| Approver chat page: connect, live messages, history, Approve / Reject, free chat | **Web Chat SDK** `NCEngine.initialize / connect`, `MessageHandler.onMessageReceived`, `GroupChannel.sendMessage(SendTextMessageParams)`, `BaseChannel.createMessagesQuery` |
| Decisions back to the guardrail engine | Webhook `message:send`, verified with `sha1(AppSecret + Nonce + Timestamp)` |

### What existed before and what we built for this challenge

*Existing (built earlier for the PayPal AI Hackathon, main branch):* the guardrail engine, the PayPal Orders v2
Sandbox integration, the Claude / offline agents, the MCP server and the AG Grid dashboard.

*Built during the Nexconn challenge (branch `nexconn`, new package `app/nexconn/` + `app/static/chat*`):*
the Nexconn server API client (request signing, data-center failover), the webhook receiver (signature, time
window, single-use nonces), the approval channel (cards, decision handling, approver allowlist, duplicate-safe,
outbox with retry), the approver chat page on the Web Chat SDK, an offline mock of the room for tests and demos,
and 68 new automated tests (489 in total). The only change to the existing core is an observer hook on approval events.

### Safety properties

- Only configured approvers can decide; a decision must name the approval id; the bot cannot approve.
- Every Nexconn message is acted on at most once (webhook retries and replays are absorbed).
- The App Secret never leaves the server; the browser gets the App Key and a per-approver token only.
- A Nexconn outage never blocks or changes a purchase; failed posts are kept and can be re-sent.
- With the channel off, the app behaves exactly as before (all earlier tests still pass).

### Business potential

Every company letting agents buy (procurement, ad spend, cloud resources, travel) needs a fast human
approval loop with an audit trail. A chat-native approval room is that loop; Nexconn's SDKs make the same
room available on web, iOS, Android and Flutter, so an approver can decide from a phone in seconds.

### How to run it

See the README section "Approve in chat (Nexconn)": an offline mock mode needs no account; live mode needs a
free Nexconn app (App Key + App Secret) and the webhook URL `https://<host>/nexconn/webhook` with the
`message:send` event.

### AI tools used

Code written with AI coding assistants (Hark agent, OpenAI Codex for independent review passes); every
change is covered by tests.

## Demo video outline (~2 min)

1. 0:00 Problem: agents spend money; approvals stuck in dashboards.
2. 0:15 Agent request: "a keyboard and a 4K monitor for the new hire" → both over the $50 cap.
3. 0:30 Two approval cards appear live in the Nexconn approval room (/chat).
4. 0:50 Approver taps Approve on the keyboard → outcome posted: PayPal order created, decided by … in chat.
5. 1:10 Types `reject APR-…` for the monitor → closed, nothing bought.
6. 1:25 Someone not on the approver list tries to approve → "not an approver", nothing changes.
7. 1:40 Dashboard shows the same state; pay the approved order with PayPal Sandbox.
8. 1:55 Close: safety properties + tests.
