# Architecture

## Processes

| Process | Count | Privileges | Responsibilities |
|---|---|---|---|
| `xianyu-monitor worker` | **exactly one per desktop** | needs X access to the owner's display | capture, analysis, persistence, job dispatch, email, reply input |
| `xianyu-monitor web` | 1..N uvicorn workers | unprivileged `xvm` user | admin UI and JSON status; reads/writes MySQL and the latest frame file |
| MySQL 8 | 1 | - | source of truth: customers, states, events, reviews, settings, outbox jobs |

The two processes communicate **only through MySQL** (settings, pause flags, calibration
profiles, heartbeat/health) and the `latest.png` frame file. The web app never captures,
runs OCR or sends input, so several HTTP workers cannot cause duplicate capture.

Duplicate workers are prevented twice: by an `flock` file lock on the host, and by a MySQL
`worker_leases` row that is renewed every 10 s and expires after 60 s. A worker that loses its
lease stops itself.

## Worker pipeline

```
capture loop ──(BoundedQueue frames, size 2, policy=block)──▶ analysis loop
                                                                 │
             FrameAnalyzer: size check → anchor check → challenge probe (periodic)
                            → per-row digests → OCR only for changed rows (process pool)
                                                                 │
             FrameProcessor (one MySQL transaction per frame):
                 identity match → tracker transition → message event (unique dedup key)
                 → review item (unique key) | new customer → email outbox job (unique key)
                 → reply planner → reply intent (unique per event) → reply job (unique key)
                                                                 │
dispatcher ── claims due jobs (optimistic conditional UPDATE + lease) ──▶ BoundedQueue email_jobs / reply_jobs
                                                                 │
             email runner: SMTP connect → phase=submitting (committed) → DATA → result
             reply runner (holds desktop lock): precheck → click → verify target → phase=typing
                                               (committed) → type + Enter → verify sent bubble
```

### Backpressure

In-memory queues are bounded transport, never storage. The frame queue blocks the capture loop
when analysis is slow, so capture is delayed rather than frames being dropped silently; the
heartbeat reports queue high-water marks and blocked puts. Job queues are filled only up
to their free capacity, and anything unclaimed stays `pending` in MySQL.

### Change detection and OCR budget

Each visible row is cropped and hashed exactly (`blake2b` over its pixels). If every row digest
and the overall badge digest equal the previous frame's, the frame is `unchanged` and no OCR
runs. Changed rows are re-read; rows seen before (same digest) reuse cached OCR. Every
`full_validation_every_n_frames` frames the cache is cleared and all rows are re-read.

OCR runs in a `ProcessPoolExecutor` (default one process) that loads the RapidOCR models once.
Crops are sent as PNG bytes. A crashed OCR process is recreated on the next frame.

### Detection rules

Implemented in `detection/tracker.py` and derived from the controlled reference captures:

| Observation | Result |
|---|---|
| Row badge appears or increases | `incoming` event (also when the preview text is identical, i.e. repeated text) |
| Row badge disappears, preview unchanged | `read_elsewhere`: a phone/web read, not a message |
| Preview changes or time label refreshes, no badge increase | `possible` (direction unknown), or `outgoing`/`incoming` if the open conversation pane shows the latest bubble's direction |
| Relative time label ages (`刚刚` → `1分钟前`) | no event |
| Overall unread total changes or stays the same | never used as a trigger |
| Badge present but unreadable, and its pixels change | `possible` with `badge_changed_unreadable` |

Event dedup keys hash `(customer, previous state revision, previous digest, new digest)`.
Since the previous state is read from MySQL, re-processing the same screen after a restart
produces the same key, which the unique index rejects.

### Identity

`detection/identity.py` matches rows to customers:

1. Low OCR confidence → `uncertain`.
2. Configured system conversation names → `system` (excluded from alerts and replies).
3. Unique exact alias match → `matched`, unless a *distinctive* stored avatar strongly conflicts.
4. Several customers share the alias → resolved only by a close avatar; otherwise `uncertain (duplicate_nickname)`.
5. A similar alias exists (OCR error or rename) → `uncertain`.
6. The avatar matches exactly one known customer (possible rename) → `uncertain`.
7. Otherwise → `new`.

Avatars shared by several customers (platform default pictures, loading placeholders) are
treated as uninformative. The same new name in two rows of one frame is `uncertain`.

### New-customer decision

In **active** mode, a `new` row creates a customer with status `new`. An email job
(`new_customer:<uuid>`, unique) is created only when there is incoming evidence (an unread
badge), the time label is recent or unreadable, and email notifications are enabled.
Otherwise a review item explains why: no badge (could be an outgoing message or read elsewhere),
or old activity (possibly a historical customer outside the baseline). In **baseline** mode
every new name becomes a baseline customer without events or emails.

### Email outbox semantics

States: `pending → leased → succeeded | failed | uncertain | cancelled`.

* Connection or auth problems happen before submission: they are retried with exponential
  backoff (connect errors, 4xx) or failed (auth, 5xx) without risk of duplicates.
* The phase `submitting` is committed before `DATA`. A timeout, disconnect, or worker crash
  after that point leaves the job **uncertain**. It is not retried automatically; the owner
  can mark it as received, retry it, or cancel it.
* Message-IDs are deterministic per notification, which helps mail clients collapse a manual resend.
* SMTP acceptance is recorded as `accepted_by_smtp_server`, not as "delivered".

### Reply execution

See `replies/executor.py`. Each step re-captures and re-checks the screen. Prechecks happen
before any input: healthy layout, exactly one visible row with the customer's alias,
no conflicting avatar, and the row preview still matching the triggering text. After clicking,
the worker checks that the conversation title matches, the latest bubble is incoming and
matches the trigger, and the composer is empty (the owner is not typing). The `typing` phase
is committed before any keystroke. Afterwards the newest bubble must be outgoing and match the
reply text; otherwise the intent is `sent_unverified`, the job becomes `uncertain`, and
automatic replies pause.

Dry-run replies perform only the read-only precheck and send no input at all.

## Data model

`customers`, `customer_aliases`, `customer_avatars`, `conversation_states`, `frames`,
`message_events`, `review_items`, `jobs`, `job_attempts`, `keyword_rules`, `reply_intents`,
`settings`, `system_state`, `calibration_profiles`, `admin_users`, `audit_log`,
`worker_leases`. The schema is created by Alembic (`migrations/versions/0001_initial.py`); a
test asserts the migration matches the ORM models.

## Repository layout

```
src/xianyu_monitor/
  capture/      X display discovery, scrot/command/replay backends
  vision/       calibration profile, image ops, frame analyzer, bubble segmentation
  ocr/          engines (RapidOCR, Tesseract, fake) and the process pool
  detection/    text normalization, relative time, identity matching, state tracker
  queueing/     bounded in-memory queue, durable MySQL job queue
  notify/       SMTP sender and email rendering/handler
  replies/      rules, planner, executor, desktop input adapters
  worker/       pipeline (frame persistence), service (orchestration), retention
  web/          FastAPI app, templates, static CSS, admin services, security
  i18n/         English (default) and Chinese catalogs
  migrations/   Alembic environment and versions
  examples/     calibration and rule examples
```
