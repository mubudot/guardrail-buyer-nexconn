# Offline fallback: review log

Branch `offline-fallback` (based on 97108d6). Feature: when the Nexconn upstream is unavailable (credentials / plan refused,
timeouts, server errors), `/try` and `/chat` switch to the in-process stand-in room, label it on the page, and keep running
the real guardrail and approval code. `/health` and `/health/ready` report the upstream state.

## Implementation (02efcd4)
- Implemented by Codex from a written brief; the run stopped just before committing, the work was intact on disk and was
  committed after the full suite passed (586 + 3 skipped at that point).

## Maintainer review, round 1 (code + UI wording and look)
Changes made in 02efcd4 before committing:
- **Header and invite text were untrue offline.** `/try` header said "real Nexconn chat" and the invite hint "get their own
  Nexconn user" even on the stand-in. Both now follow the room's mode ("offline chat stand-in"; "join this room as a second
  approver").
- **A second tab could downgrade a live sandbox.** The SDK watchdog treated "kicked offline by other client" (the same member
  open in two tabs) as an upstream failure and would move the sandbox to the stand-in for good. Kicks no longer count, and the
  watchdog stops after the first successful connection (later drops from sleep or flaky Wi-Fi are not Nexconn outages).
- **An ended plan might never be detected.** Only auth / timeout / 5xx tripped the monitor; an unrecognised business code
  (how an ended plan may actually be reported) never would. Unrecognised refusals now count too, but need 3 in a row (any
  success resets the streak).
- **Forced mode said "the Nexconn connection failed".** With `NEXCONN_FALLBACK=force` the reason is now `manual`:
  "Why: offline mode was switched on for this server."
- Tests added for each (588 passed + 3 skipped).
- Checked in Chromium (desktop 1440 px and mobile 360 px, no horizontal overflow, no console errors): banner, chip and tooltip
  on `/try` and `/chat`, over-limit card and approve in the stand-in room, both in forced mode and with a real `403` from
  Nexconn for a fake app key (reason "auth").

## Independent Codex review, round 1
- P1: probes ran while holding the channel's transport lock, so a slow probe could exhaust the worker pool and block even
  `/health/live` (reproduced with 40 concurrent config requests).
- P2: a teammate join that failed during the fallback migration (409, sandbox in use) left the inserted member behind and
  could fill the sandbox.
- P2: reloading a fallback sandbox minted member tokens on the stand-in; the stand-in keeps 4 tokens per user, so cache
  reloads eventually revoked active browser sessions.
- Fixed in 5abb376 (Codex, 10 regression tests, each failing on the old code): network I/O moved outside the transport lock
  (the lock now only guards the transport swap); failed joins remove the inserted member and release any live seat; cache
  reloads keep the bot registered but no longer mint member tokens. 598 passed + 3 skipped.

## Independent Codex review, round 2 (fresh session)
- The three round-1 fixes verified.
- P1: a capacity refusal whose message mentions the app key ("User registration limit exceeded for this app key", HTTP 429)
  was classified as `auth`, so one visitor's registration could switch the whole site to the stand-in.
- P1: with `NEXCONN_RELAY=1`, an automatic rejoin on `/chat` while a live send was pending could make that decision skip the
  relay (the send checked the new session's mode). Not reachable on the deployment (relay off there), fixed anyway.
- Fixed in 62bc2f5 (Codex, regression tests failing on the old code): an HTTP status or business code now takes precedence
  over message wording (401/403 -> auth, 429 -> capacity, 5xx -> server); a pending `/chat` send keeps the transport,
  identity, approver token and relay setting it started with. 615 passed + 3 skipped.

## Maintainer review, round 2
- Read the R2 diff: classification order is status, then business code, then wording; a capacity answer outside
  registration only counts toward the 3-in-a-row threshold. Judgment call kept: three 429s in a row on non-registration
  calls do move the site to the stand-in (live chat would not work then either; the probe brings it back).
- Banner and chip re-checked in Chromium after every round (desktop 1440 px, mobile 360 px; forced mode; no console
  errors, no horizontal overflow, over-limit card approved in the stand-in room). Wording unchanged: it says what is
  replaced (only the chat transport), what is real (guardrails, approvals, limits, simulated payments) and why.
- Noted, not changed: on a phone the banner sits above the chat, below the sandbox guide; the header chip
  ("Offline stand-in") is visible at the top without scrolling.

## Independent Codex review, round 3 (fresh session)
- The R1 and R2 fixes verified.
- P2: if `/try/sdk-failed` answered 409 (sandbox busy) the browser only showed a toast and its one-shot watchdog had
  already stopped, so the page could stay on a dead live chat until reload.
- P2: a `200` from the token endpoint without a usable token was recorded as a success before `issue_token` rejected it,
  so repeated unusable answers could never trigger the fallback.
- Fixed (maintainer, 6 new tests, the Python ones failing on the old code): the browser retries `sdk-failed` on 409, 429
  and network errors with backoff (2 s doubling, honouring `Retry-After`, at most 60 s per wait, 6 attempts) and stops if
  the room restarted or the session changed; the token answer is validated inside the monitored call, so an unusable
  answer counts as an unrecognised failure. 621 passed + 3 skipped.

## Independent Codex review, round 4 (fresh session)
- The R1–R3 fixes verified; 621 passed + 3 skipped.
- P2: if the very first `/try/session` failed (e.g. 409 because the fallback migration found the sandbox busy), there was
  no session and no SDK watchdog, so later state polls showed the stand-in but never started its chat until a reload.
- Fixed (maintainer, node regression test running the real page script, failing on the old code): when a state poll
  finds no room session and no start in progress, it starts the room again, backing off 4 s doubling up to 60 s; the
  error toast is shown once per failure streak. Checked again in Chromium (forced mode, desktop and mobile, no console
  errors). 622 passed + 3 skipped.
- Stopped here: four review rounds, each finding fewer and smaller issues; this last fix was not re-reviewed by Codex.
