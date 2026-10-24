# Network recovery and scheduled refresh

These actions are opt-in and use screenshots, local OCR and X11 input only. No HTML,
DOM or browser debugging connection is used. Keep the dedicated desktop and calibrated
Chrome layout unchanged.

## Settings

In **Settings → Page recovery and scheduled refresh**, enable only the actions you want:

| Key | Default | Meaning |
|---|---|---|
| `page_recovery_enabled` | false | Recover the calibrated “连接中断，请重连” dialog |
| `page_quick_enter_enabled` | false | Allow the calibrated saved account's “快速进入” |
| `page_refresh_enabled` | false | Refresh an idle, verified message list with F5 |
| `page_refresh_interval_minutes` | 30 | Interval, minimum 5 minutes |
| `page_recovery_wait_seconds` | 15 | Minimum wait between recovery actions |
| `email_network_wait_hours` | 0 | Notifications: pre-submission SMTP connection errors may wait this many hours (0 disables) |

Existing deployments remain unchanged until enabled. A suitable starting point is a
30-minute refresh interval and a 24-hour email connection window. Browser recovery
checks the site's TCP connectivity independently of SMTP. TCP success does not prove
that the application is logged in or an email was delivered.

## Required private calibration

The active profile needs a `page_recovery` object. Region values are pixel boxes
`[left, top, right, bottom]` within that profile's exact frame size:

- `network_title`: tight crop of “连接中断，请重连”.
- `reconnect_button`: tight crop of “重连”, whose center is clickable.
- `login_heading`: tight crop of “手机扫码安全登录”.
- `quick_button`: tight crop of “快速进入”, whose center is clickable.
- `quick_account`: saved account's masked label.
- `quick_avatar`: saved account's avatar.
- `seller`: logged-in seller name on the message list.
- `expected_account`, `expected_seller`: exact expected OCR strings.
- `expected_avatar_hash`: 16-character lowercase difference hash of the approved avatar,
  calculated with `vision.imageops.dhash`.

Store real identity values and reference screenshots privately on the board. Do not
commit them. Confirm the saved account and post-login seller yourself when calibrating.
Tight crops matter: extra blank space can produce incorrect OCR. Add the full heading
“手机扫码安全登录” to existing `challenge_keywords` settings; changing defaults does not
replace previously stored settings.

## Recovery sequence and limits

1. Stop customer detection/replies while a recognized recovery screen is present.
2. Wait for site connectivity; attempt “重连” once.
3. If the same dialog remains, press F5 once, provided the composer is verified empty.
4. If the saved-account panel appears, verify its heading, button, masked account and
   avatar, then click “快速进入” once only when explicitly enabled.
5. Require two valid message-list observations, no selected conversation or draft, and
   the expected seller before releasing this recovery's reply pause.

Each attempt marker is stored before input; a restart does not repeat that action.
Fresh screenshot/button checks and the active Chrome window class are required before
input. Recovery holds after an unrecognized account, other verification prompt, failed
quick entry or three-minute online timeout. Network waiting extends the online deadline.
The owner can resolve the page manually; two verified ready observations then clear only
this recovery's own pause. Other manual/challenge/uncertain-send pauses remain in force.
Queued or leased replies take precedence over scheduled maintenance. A pre-existing
challenge pause must be reviewed by the owner before enabling recovery.

Periodic F5 runs only on the verified idle message list, without pending/leased reply
jobs. It defers while a customer is selected or an owner draft is present. The interval
starts when first enabled and resets after successful recovery. Changing the display
size, zoom, fullscreen state or account requires recalibration.

Look at the settings page's maintenance state and worker logs:

```bash
journalctl -u xianyu-monitor-worker --since '1 hour ago' --no-pager
```

Transitions appear as `page maintenance: reconnect`, `refreshed`, `quick`, `ready` or a
specific blocking reason. No customer text, screenshot or account mask is logged by the
maintenance state machine.

## Mail semantics

The optional connection window applies only before SMTP message submission. DNS failure,
connection refusal and pre-submission disconnect wait 30–900 seconds between attempts
without consuming the delivery retry budget. Every connection attempt remains in the
attempt history; budget attempt numbers can repeat. The deadline is based on job creation
time and survives restarts. Authentication/TLS configuration errors still require repair.

A disconnect/timeout during DATA remains **uncertain** and is never automatically resent.
Previously failed/uncertain jobs are not revived by enabling this option. Review historical
jobs separately rather than replaying them in bulk.

## Controlled acceptance

Pause real replies before a deliberate network test. Keep SSH available if possible;
otherwise use a bounded local recorder and reconnect before inspecting private captures.
Verify the reconnect dialog, F5 fallback, saved-account identity and final idle list. Do not
click arbitrary “确定” buttons. QR codes, CAPTCHA, expired sessions without a verified
saved account and unknown dialogs still need the owner. Re-enable real replies only after
checking the final account and pending jobs. Synthetic state-machine tests do not replace
board-side visual calibration.
