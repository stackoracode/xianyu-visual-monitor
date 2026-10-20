# Configuration

There are three layers of configuration:

| Layer | Where | Who changes it | Examples |
|---|---|---|---|
| Process settings | private env file (`/etc/xianyu-monitor/monitor.env`, see `.env.example`) | root/owner, restart needed | DB URL, secret key, SMTP account and password, recipient, capture/OCR/input backends |
| Runtime settings | MySQL `settings`, edited on the **Settings** page | admin, effective within ~5 s | capture interval, thresholds, retention, guard term lists |
| Safety switches | MySQL `settings`, changed only on the **Overview** page with typed confirmation | admin | monitor mode, dry-run, new-customer emails, reply mode, reply sending |

Calibration profiles are stored in MySQL (`calibration_profiles`, versioned, one active).

## Safety switches and defaults

| Switch | Default | Notes |
|---|---|---|
| Monitor mode | `baseline` | Observed names become historical customers; no alerts. `active` needs `ENABLE`. |
| Dry run | **on** | Emails are rendered and recorded but not sent; replies run read-only prechecks only. Turning it off needs `ENABLE`. |
| New-customer emails | off | Turning on needs `ENABLE`. |
| Reply mode | `off` | `review` creates reply intents for owner approval; `auto` (needs `ENABLE`) queues matched replies. |
| Reply sending | off | Required for *any* desktop input, including manually approved replies. Needs `ENABLE`. |
| Automatic replies pause | not paused | Set automatically on challenges, layout or capture failures, unverifiable sends and uncertain recovery; only the owner resumes. |

## Runtime settings

| Key | Default | Meaning |
|---|---|---|
| `capture_interval_seconds` | 6 | Time between screenshots (1-600). Measure CPU on the board before lowering it. |
| `full_validation_every_n_frames` | 10 | Re-OCR all rows periodically even without pixel changes. |
| `ocr_min_confidence` | 0.80 | Below this, names make an identity `uncertain` and previews are ignored. |
| `name_fuzzy_similarity` | 0.80 | An unknown name this similar to a known alias goes to review, not "new". |
| `avatar_match_distance` / `avatar_conflict_distance` | 10 / 22 | dHash bit distances for "same avatar" and "conflicting avatar". |
| `preview_change_similarity` | 0.90 | Lower similarity counts as a preview change. |
| `new_customer_max_age_minutes` | 120 | A new name whose time label is older goes to review instead of email. |
| `layout_failure_pause_after`, `capture_failure_pause_after` | 3, 3 | Consecutive failures before replies pause and an alert is sent. |
| `challenge_probe_every_n_frames` | 5 | Frequency of the login/verification keyword probe (also run on every layout mismatch). |
| `system_conversation_names` | platform assistant names | Excluded from alerts and replies. |
| `ignored_customer_names` | - | Never alert or reply. |
| `challenge_keywords` | 安全验证, 验证码, 扫码登录, ... | Text that pauses replies and alerts the owner. |
| `alert_emails_enabled` | on | Emails for challenges, pauses and low disk (deduplicated per episode). |
| `email_language` | en | `en` or `zh` for notification text. Customer text is always the original. |
| `email_max_attempts`, `email_retry_base_seconds` | 5, 60 | Retry budget with exponential backoff (definite failures only). |
| `reply_min_confidence` | 0.90 | Events below this confidence never reply automatically. |
| `reply_verify_timeout_seconds`, `reply_step_delay_seconds` | 8, 1 | Send verification window and pause between input steps. |
| `reply_global_min_interval_seconds` | 30 | Minimum time between any two sent replies. |
| `reply_max_per_customer_per_day` | 3 | Hard cap per customer per 24 h. |
| `reply_text_match_similarity` | 0.85 | OCR similarity required for trigger/bubble verification. |
| `sensitive_terms`, `attachment_markers`, `negation_terms` | built-in lists | Force review (price, refund, contact details, attachments, negation). |
| `save_frames` | `changes` | `none`, `changes` or `all`. Saved frames are private screenshots. |
| `frames_max_count`, `frames_max_mb` | 200, 300 | Bounded diagnostic frame history. |
| `frame_rows_retention_days`, `events_retention_days`, `jobs_retention_days` | 14, 90, 180 | Database retention. Email jobs are kept because they back deduplication. |
| `min_free_disk_mb` | 1024 | Below this, frame saving stops and an alert is sent. |

## Calibration

A profile describes pixel boxes `[left, top, right, bottom]` in the captured frame
(origin at the upper left). Row fields are relative to each row's origin. Example:
[`src/xianyu_monitor/examples/calibration.example.json`](../src/xianyu_monitor/examples/calibration.example.json),
derived from the 1280x720 reference layout. Treat it as a starting point; it must be checked
against your own frames.

| Field | Purpose |
|---|---|
| `frame_width`, `frame_height` | A frame of any other size is a `layout_mismatch` (never rescaled). |
| `row.first_top`, `row.height`, `row.count` | Visible conversation rows (only fully visible rows). |
| `row.fields.avatar/badge/name/preview/time` | Per-row regions. The badge area is neutralized in the avatar fingerprint. |
| `overall_unread_badge` | Informational only. |
| `conversation_title`, `message_pane`, `composer`, `composer_click`, `composer_placeholder` | Required for replies and for direction checks on the open conversation. |
| `challenge_probe` | Region OCR'd for login/verification keywords. |
| `anchors` | Regions that look the same in the normal layout (list header, site logo). Their `dhash` is captured from a known-good frame. |
| `colors` | Measured from reference frames (16-bit color): badge red ≈ (248,76,72), selected row ≈ (232,236,232), incoming bubble ≈ (240,240,240), outgoing bubble ≈ (248,228,8), pane background ≈ (248,252,248). |

Procedure:

1. Start the worker; even uncalibrated it writes `latest.png` every interval.
2. Load the example profile (`xianyu-monitor load-calibration ...` or paste it on the Calibration page).
3. Open *Frame with region overlay* and check that every box sits on the right element.
4. Offline check with real OCR on a saved frame (on the board):
   `xianyu-monitor analyze-image /var/lib/xianyu-monitor/frames/latest.png --profile my-profile.json --pane`
5. Adjust, save (validation rejects boxes outside the frame), then press *Capture anchors from
   latest frame* while the chat page shows its normal layout.
6. Re-calibrate after changing RDP resolution, browser zoom or window position. Until then the
   worker reports `layout_mismatch` and pauses replies.

Do not let the owner's other windows cover the chat page. A covered page shows up as an anchor
mismatch (`unknown/unhealthy`), not as "no new messages".

## Keyword rules

| Field | Meaning |
|---|---|
| Priority | Higher wins. Equal priority with different templates → review. |
| Match type / mode | `contains`, `exact` or `regex`; `any` or `all` patterns. |
| Exclude patterns | If any matches, the rule is skipped. |
| Template | Literal text; only `{customer_name}` is substituted. |
| Cooldown | Per customer and rule; counts queued/sent replies. |
| Applies to | all, new customers, or known/baseline customers. |
| Allow sensitive topics | Off by default: price, refund, contact details and similar force review. |
| Always require review | The rule never sends automatically. |

Rules imported with `xianyu-monitor import-rules` are disabled unless `--keep-enabled` is given.
Use the rule tester on the Rules page before enabling anything.
