# Xianyu Visual Monitor

[中文说明](README.zh-CN.md)

A local, **screenshot + local OCR** monitor for an already-open Xianyu (闲鱼) web chat on a
desktop session. It is built for a LubanCat RK3588 board (ARM64, Ubuntu 20.04, glibc 2.31,
XFCE on Xvnc). It runs alongside the owner's existing session and never starts another browser or desktop.

It watches the visible conversation list. It keeps a historical customer baseline and emails
the owner **once** when a conversation from a customer outside that baseline is first
observed. It also provides **owner-enabled** keyword replies with conversation targeting and
send verification. Everything is administered from a small authenticated web UI (English by
default, Chinese available).

> **What it does not do.** The monitor never parses the page DOM, uses browser debugging
> ports, intercepts network traffic, calls private APIs, copies cookies, solves or bypasses
> CAPTCHAs, or modifies the browser to avoid detection. It only reads pixels of the screen that
> is already displayed and, if you enable replies, clicks and types like a person would.

## Features

| Area | What you get |
|---|---|
| Capture | `scrot` (or any PNG-to-stdout command) on the existing X display, with automatic Xvnc display/Xauthority discovery, configurable interval, and a bounded diagnostic frame history |
| Change detection | Exact per-row pixel digests. OCR runs only on rows that changed, plus a periodic full re-read. |
| OCR | RapidOCR (PaddleOCR models on ONNX Runtime, CPU) by default, Tesseract as an alternative. OCR runs in a separate process, outside the event loop. |
| Detection | Per-conversation state from the visible name, avatar fingerprint, preview, relative time, position and unread badge. It handles phone-side reads that clear web badges and never relies on the overall unread total. |
| Identity | Internal customer UUIDs with name aliases and avatar fingerprints. Duplicate nicknames, similar names, possible renames and low OCR confidence go to a **review queue**; nothing is merged automatically. |
| New-customer email | Baseline seeding mode, then a one-time email per newly observed customer through a durable MySQL outbox, with retries and an explicit *uncertain* state. Dry-run is on by default. |
| Keyword replies | Prioritized rules, templates, exclusions, cooldowns, a daily cap, negation/sensitive-topic/attachment/truncation guards, review mode, and automatic mode. Sending is **off by default** and needs typed confirmation to enable. |
| Safety | Pauses on login/verification prompts, layout changes, capture failures and unverifiable sends. Replies are only resumed by the owner. |
| Admin UI | Overview/health, calibration with region overlay, customers and identity corrections, review queue, observations, notifications, rules, replies, settings, and a redacted diagnostics export |
| Operations | systemd units, single-worker lease, restart recovery, graceful shutdown, retention and disk guard, redacted JSON logs |

## Quick look at the architecture

```
 existing Xvnc desktop (Chrome with the chat page, owned by the owner)
        │ scrot screenshot every N s                ▲ xdotool click/type (only if enabled)
        ▼                                            │
 ┌──────────── capture worker (1 per desktop, asyncio) ────────────┐
 │ capture → bounded queue → analyzer (OCR process pool) → tracker │
 │   → MySQL transaction: frame, customers, states, events,        │
 │     review items, outbox jobs (email / reply)                   │
 │ dispatcher → bounded job queues → email sender / reply executor │
 └─────────────────────────────────────────────────────────────────┘
        ▲ settings, pause flags, calibration      │ health, heartbeat, history
        │                                         ▼
 ┌──────────── FastAPI admin UI (N uvicorn workers, unprivileged) ─┐
 └─────────────────────────────────────────────────────────────────┘
```

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Getting started

1. Read [docs/LIMITATIONS.md](docs/LIMITATIONS.md). Screenshot monitoring has blind spots.
2. Deploy on the board following [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): Conda Python 3.10,
   MySQL 8, the private env file and the systemd units.
3. Calibrate regions against a real frame ([docs/CONFIGURATION.md](docs/CONFIGURATION.md)).
4. Seed the customer baseline in **baseline mode**, then switch to **active mode**.
5. Configure SMTP, test it from the Notifications page, then disable dry-run and enable
   new-customer emails.
6. Optional: create keyword rules, try them in the rule tester, use *review* mode first, and
   enable automatic sending only when you are satisfied.

## Development

```bash
conda create -n xvm python=3.10 && conda activate xvm
pip install -r requirements/dev.txt && pip install --no-deps -e .
pytest -q                      # SQLite
XVM_TEST_MYSQL_URL='mysql+aiomysql://user:pass@127.0.0.1:3306/xvm_test?charset=utf8mb4' pytest -q
python scripts/privacy_scan.py # before publishing anything
```

Tests use synthetic frames drawn from scratch, a content-addressed fake OCR engine, a fake SMTP
server and a fake desktop that reacts to clicks and typing. No real screenshots, accounts or
messages are involved. See [docs/TESTING.md](docs/TESTING.md).

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Deployment on the board](docs/DEPLOYMENT.md) · [中文部署指南](docs/zh-CN/DEPLOYMENT.md)
- [Configuration and calibration](docs/CONFIGURATION.md)
- [Operations: pausing, recovery, alerts](docs/OPERATIONS.md)
- [Limitations and blind spots](docs/LIMITATIONS.md)
- [Compatibility (ARM64 / Ubuntu 20.04)](docs/COMPATIBILITY.md)
- [Testing](docs/TESTING.md)
- [Security](SECURITY.md)

## License

MIT. See [LICENSE](LICENSE). MIT is a permissive default that lets the owner reuse and adapt
the code freely; the owner may relicense their own copy. Platform terms of service are a
separate matter: screenshot automation is not automatically permitted by a site's terms, so
review them before enabling automatic replies.
