# Limitations and blind spots

This monitor observes pixels of the visible conversation list. That has inherent limits, and
the software reports them instead of hiding them.

## What cannot be guaranteed

* **Complete message history.** Only the latest preview of each visible row can be read.
  Several messages between two captures collapse into one observation, and an intermediate text
  is never seen. In the reference captures, a burst of messages showed only the final preview,
  and in one case the preview did not show the most recent marker the sender reported.
* **Detection of messages read elsewhere first.** If the seller phone reads a conversation
  before the next capture, the badge never appears. Preview/time changes are still reported as
  `possible` events of unknown direction, but no incoming badge evidence exists, so no
  automatic email or reply is triggered.
* **Off-screen conversations.** Only rows inside the calibrated visible area are examined
  (`coverage` on the Overview page). A conversation below the fold or scrolled away is invisible.
  The monitor does not scroll by itself.
* **First-ever contact.** "New customer" means *absent from the stored baseline when first
  observed by this monitor*. A returning customer who is outside the baseline (for example
  because they were never visible during seeding) looks new. Old time labels push such cases
  into review, but this is a heuristic.
* **Stable identity from nicknames.** Names can be duplicated or changed, and avatars can be
  defaults or change. Ambiguous cases go to review, and only an owner merges identities.
* **OCR accuracy.** OCR quality on the real UI has not been measured on the board (see
  [COMPATIBILITY.md](COMPATIBILITY.md)). Low confidence is surfaced as uncertainty, never guessed.
* **Exactly-once email.** SMTP acceptance is not delivery. Interrupted submissions are
  `uncertain` and are only resent by the owner.
* **Exactly-once replies.** A crash between pressing Enter and verification leaves the reply
  `sent_unverified`. It is never retried automatically.
* **Invisibility to the platform.** Nothing here hides automation. Clicking a conversation marks
  it read; automatic replies are visible to customers as messages from the seller. Review the
  platform's terms before enabling replies.

## Known behavior from the reference captures

* The overall unread number can stay the same while one conversation gets a new message
  (another badge disappeared at the same time). It is never used as a trigger.
* Seller-phone reads clear the web badge while the preview and row order stay. This is reported
  as `read_elsewhere`.
* A right-aligned yellow bubble with a "read/unread" label is an *outgoing* message state, not an
  incoming badge.
* The desktop panel clock on the board differed from real time; all timestamps use the
  collector's UTC clock.

## Untested states

No samples were available for double-digit badges, verified error or exclamation indicators,
a genuinely unknown customer's first contact, or a read before the first observation.
Thresholds for these cases are calibrated guesses and need validation during a controlled test
window (the owner's controlled test plan, cases T05–T14).
