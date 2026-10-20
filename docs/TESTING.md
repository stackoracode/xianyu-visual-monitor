# Testing

```bash
pip install -r requirements/dev.txt && pip install --no-deps -e .
pytest -q
XVM_TEST_MYSQL_URL='mysql+aiomysql://user:pass@127.0.0.1:3306/xvm_test?charset=utf8mb4' pytest -q   # disposable DB!
```

When `XVM_TEST_MYSQL_URL` is set, **every** database test runs against MySQL, and tables are
dropped and recreated per test. Never point it at a real database.

## Test doubles

* `tests/synth.py` draws complete synthetic desktop frames from the calibration geometry:
  panels, patterned avatars, red badges with numerals, selected-row shading, incoming and
  outgoing bubbles, a composer, and covering dialogs. Text is drawn as a short hash so that
  distinct strings give distinct pixels; the true string is registered with the
  content-addressed `FakeOcrEngine` through the analyzer's own crop functions.
* `FakeDesktop` models the chat page: clicking a row opens it and clears its badge, the composer
  accepts typing, and Enter appends an outgoing bubble. Flags simulate a misrouted click and a
  dropped send.
* `FakeSMTP` simulates success, refused connections, auth failures, 4xx/5xx rejections and
  disconnects/timeouts during DATA.
* No test touches a real board, mailbox, account or browser.

## Coverage of required scenarios

| Scenario | Tests |
|---|---|
| Mobile-read transitions (badge cleared, preview kept) | `test_tracker.py::test_reference_sequence...`, `test_pipeline.py::test_mobile_read_transition_is_not_a_message` |
| Unchanged global unread with a changed row | `test_pipeline.py::test_unchanged_overall_unread_does_not_hide_new_message` |
| Repeated text | `test_tracker.py::test_repeated_identical_text_with_badge_increase_is_new_message` |
| Existing vs new customers, baseline | `test_identity.py`, `test_pipeline.py::test_baseline_*`, `test_new_customer_notified_once_*` |
| OCR uncertainty, duplicate nicknames, renames | `test_identity.py`, `test_pipeline.py::test_duplicate_nickname_and_low_confidence_are_review_items`, `test_tracker.py::test_low_confidence_preview_does_not_flap_state` |
| Outgoing-message exclusion | `test_tracker.py::test_open_conversation_pane_resolves_direction`, `test_pipeline.py::test_new_conversation_without_badge_goes_to_review_not_email`, `test_service.py::test_automatic_keyword_reply_end_to_end...` |
| Persisted dedup & restart recovery | `test_pipeline.py::test_new_customer_notified_once_across_messages_frames_and_restart`, `test_jobs.py::test_recovery_*`, `test_service.py::test_restart_*` |
| Bounded queues / backpressure | `test_bounded_queue.py` |
| MySQL outbox behavior | `test_jobs.py` (both DBs), `test_mysql.py` (parallel claims, concurrent same-key enqueue, utf8mb4) |
| SMTP uncertainty | `test_email.py` |
| Reply targeting / verification | `test_reply_executor.py`, `test_service.py` |
| Layout change, covered window, challenge, blank screen | `test_analyzer.py`, `test_service.py::test_challenge_*` |
| Admin UI auth, CSRF, i18n, guarded switches | `test_web.py`, `test_i18n.py` |
| Migration ↔ models | `test_migrations.py` |
| Live X11 capture/input (CI ARM64 job only) | `test_x11_live.py` |
| Real OCR on synthetic text (CI ARM64 job only) | `test_real_ocr.py` |

## Manual acceptance on the board

Mocked tests passing does not mean a live deployment works. The controlled acceptance cases
(new controlled account, repeated messages, phone reads, two alternating customers, a manual
seller reply, the platform assistant, covering/scrolling the list, resizing, network loss,
worker restart, SMTP failure, natural login expiry, failed-send indicator) must be run with the
owner's second account during an agreed window. Record each result and compute precision and
recall for new-customer alerts plus detection latency (Overview shows p50/p95).
