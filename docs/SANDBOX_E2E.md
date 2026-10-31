# Real PayPal Sandbox end-to-end run: agent → buyer approval → capture

- **Date:** 2026-10-30, 04:45–04:50 Asia/Taipei (2026-10-29 20:45–20:50 UTC), code at `d08f907`.
- **Result:** the order was created by the real Claude agent, a Sandbox **personal** test buyer logged in on
  `www.sandbox.paypal.com` and clicked **Pay**, PayPal redirected to the app's `/paypal/return`, and the app
  captured the payment. App and PayPal both report **`COMPLETED`**. It worked on the first attempt, with no retries and no manual capture call.

| | |
|---|---|
| App order | `ORD-CD1A3AB5C1` (request `REQ-98824ED24A`) |
| Item | 2 × USB-C Charging Cable 2m (P-1001, Sandbox Electronics, office-supplies), **$25.98 USD** |
| Policy | `ALLOW`: within caps, 25.98 ≤ 50.00 per order, today 25.98 ≤ 150.00 (all 8 rules PASS) |
| PayPal order | `34M919590A962473E` (Orders v2, `intent: CAPTURE`) |
| PayPal capture | **`8VA9873790040992M`**, status `COMPLETED`, `final_capture: true`, 25.98 USD |
| Payer | Sandbox personal test account (`sb-…@personal.example.com`, address omitted) |

## Configuration

`PAYPAL_MODE=sandbox PAYPAL_AUTH=proxy` (an egress proxy adds Basic auth for `api-m.sandbox.paypal.com`, so the
app held no PayPal credentials), `AGENT_MODE=claude MODEL=claude-sonnet-5-5` through a Messages-API-compatible
gateway (auth also added by the gateway), a random `APPROVER_TOKEN`, a fresh temporary `DB_PATH`, and `GRB_NO_DOTENV=1`.
The app ran on `127.0.0.1` behind a `cloudflared` quick tunnel, and `PUBLIC_BASE_URL` was set to the tunnel URL, so
PayPal's `return_url`/`cancel_url` reached it. The tunnel is gone now. No secret, token, or test-account password is
in this repo.

## Timeline (from the app's audit API, `GET /decisions/DEC-BE9338ACE6`)

| UTC | Transition | Note |
|---|---|---|
| 20:45:35 | policy check (`check_policy`, DEC-E71309867E) | ALLOW |
| 20:45:38 | → `CREATING` (DEC-BE9338ACE6, outcome RESERVED) | reserved 25.98 USD |
| 20:45:39 | `CREATING` → `PAYER_ACTION_REQUIRED` | PayPal order created, `payer-action` link returned |
| (buyer) | Sandbox login + review + **Pay** | see screenshots 1–2 |
| 20:49:38 | `PAYER_ACTION_REQUIRED` → `CAPTURING` | `GET /paypal/return?token=34M919590A962473E&PayerID=…` → 303 |
| 20:49:39 | `CAPTURING` → `COMPLETED` | capture `8VA9873790040992M` |

## Agent run (Claude, real model)

Request: *"Order 2 USB-C cables for the new hire's desk"*. 4 model turns, `stop_reason=end_turn`, 6,748 input +
217 output tokens. Tool calls: `search_catalog("USB-C cable")` → `check_policy(P-1001 × 2)` = ALLOW →
`create_order(P-1001 × 2)` = `AWAITING_PAYER_APPROVAL`. Full transcript:
[`sandbox_e2e_transcript.json`](sandbox_e2e_transcript.json).

## Verification after the redirect

- `GET /orders/ORD-CD1A3AB5C1` returned `status: COMPLETED`, `paypal_status: COMPLETED`, `paypal_live_status: COMPLETED`,
  `capture_id: 8VA9873790040992M`, `capture_unknown: 0`, `op_gen: 4`.
- `GET /spend` returned `captured: 25.98`, `reserved: 0.00`, `total: 25.98`, `remaining: 124.02`, `needs_reconciliation: 0`
  (the reservation became captured spend).
- `GET /decisions` returned 2 rows (policy check ALLOW, create order RESERVED → order status COMPLETED).
- Read straight from PayPal, `GET https://api-m.sandbox.paypal.com/v2/checkout/orders/34M919590A962473E` returned `status: COMPLETED`,
  `intent: CAPTURE`, `purchase_units[0].payments.captures[0]` = `8VA9873790040992M` / `COMPLETED` / 25.98 USD /
  `final_capture: true`, and payer = the Sandbox personal test account (address omitted).

## Screenshots (`docs/screenshots/`)

1. `sandbox_e2e_1_paypal_login.png`: the Sandbox checkout login with the test buyer's email entered, before the password.
2. `sandbox_e2e_2_review_pay.png`: the review page showing $25.98, Pay in full (fake Sandbox funding sources).
3. `sandbox_e2e_3_return_completed.png`: the app right after PayPal's redirect. Toast reads "Payment returned from PayPal: ORD-CD1A3AB5C1 → COMPLETED".
4. `sandbox_e2e_4_operations.png`: the Operations tab: order COMPLETED, capture id, spend cards ($25.98 captured, $0.00 reserved).
5. `sandbox_e2e_5_audit_detail.png`: the Audit tab with the decision detail (quote, verdict, reasons).
6. `sandbox_e2e_6_audit_timeline.png`: the decision detail scrolled to the rules and the payment timeline (CREATING → … → COMPLETED).

## UI notes from this first real-browser pass (1920×1440 viewport)

- In the Operations → Orders grid, the **Payment / capture** column is slightly too narrow, so the capture id is cut off
  (`8VA98737900409…`). The Item column also shortens "USB-C Charging …".
- The Audit grid is wider than its card. Outcome and Payment status sit off to the right and need a horizontal scroll that
  is easy to miss, and opening the detail panel hides them completely.
- The header badge says "approver token on" while the Approver token input is empty. This is correct (the server
  requires a token and the user has not typed one), but a first-time viewer may find it confusing.
- The detail panel scrolls internally without problems, auto-refresh works ("Snapshot …" updates), the empty state looks
  right, and nothing overlaps. The browser console was not inspected.
- The agent's summary line says "Bought:" for an order that is still waiting for the payer. It is accurate after capture
  but early before it.

### Follow-up: UI fixes for the notes above

- Orders grid: Order, PayPal order and Payment / capture columns are wide enough for the full ids (a 17-character
  capture id such as `8VA9873790040992M` and the mock's 18-character ones); Item wraps instead of truncating, and ids
  have tooltips. The page is up to 1840 px wide on large screens.
- Audit grid: columns flex to the card (12 px text, minimum widths), long values wrap, Request id / Decision id are
  hidden by default ("show ids" brings them back; search and CSV always include them). The detail panel sits beside
  the grid at 1800 px and wider and below it otherwise, so Outcome and Payment status stay visible with it open.
  Checked headless at 1920×1440 and 1280×800: no truncated cell, no horizontal scroll, no hidden column.
- Header: the approver pill now reads "approval requires token · enter it to approve" while the box is empty and
  "approval requires token · token entered" after typing (tooltip explains); approvals disabled / open demo mode are
  shown in a red pill.
- Agent summary: "Bought" is used only for a `COMPLETED` capture. The Claude prompt says so, and if the model still
  claims a purchase while any order of the run is not `COMPLETED`, the summary is replaced by the deterministic one
  built from the tool results ("order created, awaiting buyer approval", "payment outcome unconfirmed", "PayPal order
  failed", or "bought and paid"). Tests in `tests/test_agent.py`.

## Second run: the demo-video recording (2026-10-30, ~08:08–08:09 Asia/Taipei)

Recorded end to end by `scripts/video/record_demo.py` in headless Chromium against the real Sandbox, with the
Sandbox buyer's email and password passed only as environment variables (`SANDBOX_BUYER_EMAIL` /
`SANDBOX_BUYER_PASSWORD`; never printed, logged or written to a file). This is the footage in the demo video's
scenes 6–7. Same configuration as above (`PAYPAL_AUTH=proxy`, Claude via the gateway), except that the app ran on
`localhost` and the recording browser was the buyer, so no tunnel was needed for PayPal's redirect.

| | |
|---|---|
| Request | *"Buy 2 USB-C charging cables and a mechanical keyboard for our new hire, and also a $100 gift card as a welcome present."* (`REQ-F53FDA2D24`) |
| Policy | cables `ALLOW` (DEC-07F47682C2), keyboard `NEEDS_APPROVAL` / `per_order_cap` (DEC-715B880197), gift card `DENY` / `merchant_blocklist, category_allowlist` (DEC-E6E3FCE941) |
| App order (cables) | `ORD-F79F379F1E`, 2 × P-1001, **$25.98 USD**, decision DEC-C23427B143 (RESERVED) |
| PayPal order | `92G59687LY4126609` |
| PayPal capture | **`14579671MH105231J`**, `COMPLETED`, `final_capture: true`, 25.98 USD |
| Payer | Sandbox personal test account (`sb-…@personal.example.com`, blurred in the video) |
| Keyboard | approval `APR-4791ACF9` approved with the approver token → `ORD-062B118B1C` (PayPal `6PL1130222787443G`), then the buyer used "Return to Test Store" → app `CANCELLED`, reservation released (PayPal keeps that order at `PAYER_ACTION_REQUIRED`; it is never captured) |

Timeline (app `order_events`, Asia/Taipei): 08:08 `CREATING` (reserved 25.98) → `PAYER_ACTION_REQUIRED` →
08:09 buyer login + **Pay** on `www.sandbox.paypal.com` → `/paypal/return` → `CAPTURING` → `COMPLETED`.
Afterwards the app's DB showed `status: COMPLETED`, `paypal_status: COMPLETED`, `capture_unknown: 0`, `op_gen: 4`, and
`GET https://api-m.sandbox.paypal.com/v2/checkout/orders/92G59687LY4126609` returned `status: COMPLETED`, `intent: CAPTURE`,
capture `14579671MH105231J` / `COMPLETED` / 25.98 USD / `final_capture: true`.

A rehearsal just before it (same day, ~07:44 Asia/Taipei, request *"Order 2 USB-C charging cables for the new hire's desk"*)
also ended `COMPLETED` on both sides: app `ORD-3AD9607A27`, PayPal order `2WS49113MS447012F`, capture `4PY775905X147260A`
(25.98 USD). In that rehearsal the first redirect back to `localhost` was sent through the HTTP proxy by Chromium (loopback
is proxied unless the bypass list has `<-loopback>`, now fixed in `record_demo.py`), so the return URL was re-opened directly;
the app captured on that request.
