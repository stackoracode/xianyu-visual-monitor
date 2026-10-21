# Operations

## Health states

| Status | Meaning | Detection continues? | Replies |
|---|---|---|---|
| `ok` | Layout validated, rows read | yes | allowed if enabled and not paused |
| `unchanged` (frames) | Pixels identical to previous frame; no OCR needed | yes | - |
| `layout_mismatch` | Frame size differs or anchors do not match (covered window, scrolled/zoomed page, dialog) | no events from these frames | paused after N consecutive frames |
| `blank` | Black/empty frame (locked screen, display off) | no | paused after N frames |
| `challenge` | Login/verification keywords visible | no | paused immediately; alert email |
| `capture_failed` | Screenshot command failed (display gone, permissions) | no | paused after N failures |
| `not_calibrated` | No valid active calibration profile | no | - |
| `paused` | Monitoring paused by an admin | no | - |

The worker heartbeat is shown on the Overview page. If it is older than 60 s, the UI shows a
banner: the absence of alerts then means nothing. `/healthz` returns `{"web": "ok", "worker":
"fresh"|"stale"}` without authentication for external watchdogs.

## Pausing and resuming

* **Pause monitoring** stops capture and analysis (e.g. while you use the desktop heavily).
* **Pause automatic replies** stops reply jobs; queued replies return to *review* when they come up.
* Automatic pauses (challenge, layout, capture, unverified send, recovered uncertain job) set
  the reply pause with a reason and send one alert email per episode. Only the owner can
  resume replies, after checking the screen. Resuming does not clear a verification prompt
  that is still visible.
* The monitor never interacts with login or verification pages. Solve them yourself on the
  desktop or phone.

## Bursts and the selected conversation

Automatic reply jobs wait `reply_debounce_seconds` (default 3 seconds) before becoming
eligible. A newer observed incoming message replaces older jobs for that customer while
they are still before typing. Rules are evaluated against the latest observed preview;
an unmatched or review-only newer question also cancels the older automatic response.
The older intent is shown as `superseded`, not as a failed delivery. This does not
reconstruct intermediate messages that were never visible to a capture.

If the trigger changes after opening a conversation, no text is pasted. The worker
samples the open pane again and plans from the newer incoming message. If it changes
after pasting, Enter is withheld, the draft is retained (`draft_review`), and automatic
replies pause for owner inspection. These drafts have no automatic resend action.
After inspecting/clearing the draft on the seller desktop, review the latest intent
and resume replies explicitly; do not re-submit an uncertain send without checking delivery.

After a reply attempt that opened a customer, the worker samples that conversation
before leaving. With `park_system_conversation` enabled in the calibration profile
(default), it selects only a uniquely identified, visible excluded system conversation,
then verifies its title. It never uses another customer as a parking target. An owner
draft or unavailable system row prevents parking. Pixel changes before the parking
click cancel that click. Make a system conversation visible to improve unread badges.

The selected conversation is monitored even without a red badge: its title must match
the identified customer, its latest bubble must have a confident incoming direction,
and its text must agree with the row preview for automatic confidence. Persisted pane
evidence detects observable bubble changes across restarts. Screenshots queued before
a reply action are discarded as `superseded` rather than rolling back customer state.

## Recovery table

| Situation | What happens |
|---|---|
| Worker crash or restart | The lease and file lock are re-acquired. Jobs leased by the previous process become `pending` again, unless they had reached an irreversible phase (`submitting`, `typing`, `verifying_send`); those become `uncertain` and replies pause. Detection resumes from the persisted per-customer state, so the same screen produces no new events. |
| MySQL unavailable | The frame transaction fails and is logged; health turns stale. No events are fabricated, and the next successful frame is compared against the last persisted state. |
| OCR process crash | The pool is recreated on the next frame; that frame is retried on the next interval. |
| Display number changes | Capture fails, the session is rediscovered on the next attempt; with several displays, set `XVM_CAPTURE_DISPLAY`. |
| Network outage on the board | The page may show stale content; the monitor reports whatever is visible. Email jobs retry with backoff. |
| Two workers started | The second one exits (`worker lease ... held by ...`, exit code 3). |

## Notification outcomes

| Job status | Meaning | Owner action |
|---|---|---|
| `succeeded` | Accepted by the SMTP server (or recorded in dry-run) | none |
| `pending` | Waiting for retry after a definite failure | none |
| `failed` | Permanent failure or retry budget exhausted | fix configuration, then *Retry* |
| `uncertain` | The connection dropped while the message was being submitted | check the mailbox; *Mark as received* or *Retry* |
| `cancelled` | Cancelled by an admin | - |

## Logs and diagnostics

* The worker logs JSON to journald and to `/var/lib/xianyu-monitor/logs/worker.log` (rotated,
  5 x 5 MB). Emails, IPs, URL credentials and `password=`-style values are redacted;
  customers are referred to by pseudonymous IDs.
* *Diagnostics → Download bundle* exports settings without secrets, health state, frame
  timings, event statistics (pseudonymized, no message text) and the redacted log tail. The
  *masked frames* option includes only unread badges and anchor regions. This bundle is
  what an optional offline reviewer (e.g. a code assistant) should receive. Detection does
  not depend on any external reviewer, and no reviewer is scheduled automatically.
* Saved diagnostic frames (`/var/lib/xianyu-monitor/frames`) are **private screenshots**. Keep
  the directory permissions as installed (root:xvm, 2770) and do not share it.
