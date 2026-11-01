# GuardRail Buyer — approve AI purchases in chat (Nexconn Build Challenge 2026 write-up)

*The text of the submitted write-up PDF (which adds the App Key, links and screenshots). Live end-to-end tests (main room and `/try`): [NEXCONN_LIVE_E2E.md](NEXCONN_LIVE_E2E.md).*

## Why we built it

AI agents are starting to spend money. The safe pattern is a hard policy between the model and the wallet,
with a human approving anything above a cap. In practice the weak link is the approval itself: requests sit
in a dashboard nobody has open, so people either rubber-stamp them later or raise the caps until approvals
never happen. Approvals need to reach the people who decide, in the place where the team already talks.

## What it does

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
4. The outcome of the decision — PayPal order created, refused by a hard rule, or rejected — is posted back
   into the room, with who decided and where (later payment events are tracked on the dashboard). Decisions made on the dashboard are mirrored into the room too, so the chat
   shows every approval request and how it ended (the dashboard's audit log stays the full record of all
   guardrail decisions, including automatic in-policy purchases and denials).

**Try it in one click (`/try`).** Anyone can open a private sandbox with no account and no token: in live mode
the server registers the visitor as a real Nexconn user, creates a private approval group for them, and gives them their own
policy and database (simulated checkout, no money). An invite link adds a teammate as a second approver, so two
people can see the same card and one of them decides it in the chat. Sandboxes are rate-limited, capped, expire
after 24 idle hours (their orders and approvals are deleted) and can be reset; past the live-registration cap, new
sandboxes use an offline stand-in for the chat, and the main room is never affected.

## Features that use Nexconn

| Feature | Nexconn product / API |
|---|---|
| Approval room members: bot user + approver users, registered on startup | Platform Chat API `POST /v4/auth/access-token/issue` |
| The approval room itself | Platform Chat API `POST /v4/group-channel/create`, `/v4/group-channel/join` |
| Approval cards and outcome messages (text + structured `extra`) | Platform Chat API `POST /v4/group-channel/message/send` |
| Approver chat page: connect, live messages, history, Approve / Reject, free chat | **Web Chat SDK** `NCEngine.initialize / connect`, `MessageHandler.onMessageReceived`, `GroupChannel.sendMessage(SendTextMessageParams)`, `BaseChannel.createMessagesQuery` |
| Decisions back to the guardrail engine | Webhook `message:send`, verified with `sha1(AppSecret + Nonce + Timestamp)` |
| `/try` playground (live mode): each visitor and invited teammate is a real Nexconn user in their own private group | Platform Chat API (token issue, group create / join) + Web Chat SDK in the shared room view |

## What existed before and what we built for this challenge

*Existing (built Oct 29–30, 2026 for the PayPal AI Hackathon; public at github.com/mubudot/guardrail-buyer):* the guardrail engine, the PayPal Orders v2
Sandbox integration, the Claude / offline agents, the MCP server and the AG Grid dashboard.

*Built during the Nexconn challenge (new packages `app/nexconn/` and `app/playground/`, `app/static/chat*` and `try*`):*
the Nexconn server API client (request signing, data-center failover), the webhook receiver (signature, time
window, single-use nonces), the approval channel (cards, decision handling, approver allowlist, duplicate-safe,
outbox with retry), the approver chat page on the Web Chat SDK, an offline mock of the room for tests and demos,
the `/try` playground (per-visitor sandboxes, invites, limits, expiry, health checks) and 118 new automated
tests (539 in total). Existing files also got smaller edits: an observer hook on approval events, the wiring for the new routes, an
"Approval chat" link and style on the dashboard, configuration examples, the MCP credential scrub, test fixtures and docs;
later fixes from the PayPal repo were merged in.

## Safety properties

- Only configured approvers can decide; a decision must name the approval id; the bot cannot approve.
- Every Nexconn message is acted on at most once (webhook retries and replays are absorbed).
- The App Secret never leaves the server; the browser gets the App Key and a per-approver token only.
- A failed chat post never rolls back or changes a purchasing decision; failed posts are kept and can be re-sent. Deciding in chat needs Nexconn to be reachable; the dashboard can always decide.
- With the channel off, the app behaves exactly as before (all earlier tests still pass).

## Business potential

Every company letting agents buy (procurement, ad spend, cloud resources, travel) needs a fast human
approval loop with an audit trail. A chat-native approval room is that loop; Nexconn's SDKs make the same
room available on web, iOS, Android and Flutter, so an approver can decide from a phone in seconds.

## How to run it

See the README section "Approve in chat (Nexconn)": an offline mock mode needs no account; live mode needs a
free Nexconn app (App Key + App Secret) and the webhook URL `https://<host>/nexconn/webhook` with the
`message:send` event.

## AI tools used

Code written with AI coding assistants (Hark agent, OpenAI Codex for independent review passes); every
change is covered by tests.
