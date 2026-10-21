"""Per-conversation visible-state transitions.

Rules derived from the controlled reference captures:

* The overall unread total is never used as a trigger; it can stay constant while one row
  gains a message and another row is read.
* A row badge disappearing with an unchanged preview is a read on another device (or the
  web), not a message.
* A badge increase with an unchanged preview is a new message with repeated text.
* A preview/time change without a badge increase has unknown direction (an outgoing reply
  or an incoming message read elsewhere) unless the open conversation pane shows the
  direction of the latest bubble.
* Relative time labels aging ("刚刚" -> "1分钟前") are not events.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field, replace

from xianyu_monitor.detection.textnorm import is_truncated, normalize, similarity
from xianyu_monitor.detection.timeparse import RelTime, is_recency_refresh, parse_relative
from xianyu_monitor.runtime_settings import RuntimeSettings
from xianyu_monitor.vision.analyzer import RowObservation


@dataclass
class RowState:
    preview_raw: str = ""
    preview_norm: str = ""
    preview_conf: float = 0.0
    badge_present: bool = False
    badge_count: int | None = None
    badge_digest: str = ""
    time_text: str = ""
    time_age_min: float | None = None
    position: int = -1
    observed_at: dt.datetime | None = None
    revision: int = 0
    digest: str = ""

    def compute_digest(self) -> str:
        raw = "\x1f".join([self.preview_norm, str(self.badge_present), str(self.badge_count),
                           self.badge_digest if self.badge_count is None else "", normalize(self.time_text)])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass
class Transition:
    kind: str | None  # incoming | possible | outgoing | read_elsewhere | None
    direction: str = "unknown"
    signals: list[str] = field(default_factory=list)
    confidence: float = 0.0
    badge_before: int | None = None
    badge_after: int | None = None
    new_state: RowState = field(default_factory=RowState)
    state_changed: bool = False
    dedup_key: str = ""
    preview_truncated: bool = False


def _badge_value(present: bool, count: int | None) -> int | None:
    if not present:
        return 0
    return count


def observe(prev: RowState | None, row: RowObservation, now: dt.datetime, rt: RuntimeSettings,
            customer_id: str, pane_direction: str | None = None, pane_changed: bool = False,
            pane_verified: bool = False) -> Transition:
    """Compute the transition from the persisted state ``prev`` to the observed ``row``."""
    min_conf = float(rt.ocr_min_confidence)
    rel_new = parse_relative(row.time_text, now) if row.time_conf >= min_conf else RelTime("unknown", None)
    preview_ok = row.preview_conf >= min_conf and bool(row.preview)
    new = RowState(
        preview_raw=row.preview if preview_ok else (prev.preview_raw if prev else ""),
        preview_norm=normalize(row.preview) if preview_ok else (prev.preview_norm if prev else ""),
        preview_conf=row.preview_conf if preview_ok else (prev.preview_conf if prev else 0.0),
        badge_present=row.badge_present,
        badge_count=row.badge_count,
        badge_digest=row.badge_digest,
        time_text=row.time_text if row.time_conf >= min_conf else (prev.time_text if prev else ""),
        time_age_min=rel_new.age_minutes if rel_new.age_minutes is not None else None,
        position=row.index,
        observed_at=now,
        revision=(prev.revision if prev else 0),
    )
    new.digest = new.compute_digest()
    signals: list[str] = []
    if not preview_ok:
        signals.append("preview_unreadable")
    truncated = is_truncated(row.preview)

    after = _badge_value(row.badge_present, row.badge_count)
    if prev is None:
        signals.append("first_seen")
        t = Transition(None, signals=signals, new_state=new, badge_before=None, badge_after=after,
                       preview_truncated=truncated)
        if row.badge_present:
            t.kind, t.direction = "incoming", "incoming"
            t.signals.append("badge_present")
            t.confidence = 0.9 if preview_ok else 0.7
        elif rel_new.age_minutes is not None and rel_new.age_minutes <= 5:
            t.kind, t.signals = "possible", t.signals + ["recent_time_label"]
            t.confidence = 0.4
        return _finish(t, prev, customer_id)

    before = _badge_value(prev.badge_present, prev.badge_count)
    badge_up = badge_down = badge_cleared = badge_unreadable_change = False
    if not prev.badge_present and row.badge_present:
        badge_up = True
    elif prev.badge_present and row.badge_present:
        if before is not None and after is not None:
            badge_up, badge_down = after > before, after < before
        elif prev.badge_digest != row.badge_digest:
            badge_unreadable_change = True
    elif prev.badge_present and not row.badge_present:
        badge_cleared = True

    preview_changed = False
    if preview_ok and prev.preview_conf >= min_conf:
        preview_changed = similarity(prev.preview_norm, new.preview_norm) < float(rt.preview_change_similarity)
    elif preview_ok and not prev.preview_norm:
        preview_changed = True

    rel_prev = RelTime("stored", prev.time_age_min, 1.0) if prev.time_age_min is not None else RelTime("unknown", None)
    recency = (prev.observed_at is not None and row.time_conf >= min_conf
               and is_recency_refresh(rel_prev, prev.observed_at, rel_new, now))
    moved_to_top = prev.position > 0 and row.index == 0

    for flag, name in ((badge_up, "badge_up"), (badge_down, "badge_down"), (badge_cleared, "badge_cleared"),
                       (badge_unreadable_change, "badge_changed_unreadable"), (preview_changed, "preview_changed"),
                       (recency, "time_refreshed"), (moved_to_top, "moved_to_top")):
        if flag:
            signals.append(name)

    t = Transition(None, signals=signals, new_state=new, badge_before=before, badge_after=after,
                   preview_truncated=truncated)
    if badge_up:
        t.kind, t.direction = "incoming", "incoming"
        t.confidence = 0.95 if preview_ok else 0.75
        if preview_ok and not preview_changed:
            t.signals.append("repeated_or_unchanged_preview")
    elif badge_unreadable_change:
        t.kind, t.confidence = "possible", 0.5
    elif preview_changed or recency or pane_changed:
        if pane_direction == "outgoing":
            t.kind, t.direction, t.confidence = "outgoing", "outgoing", 0.85
            t.signals.append("pane_last_bubble_outgoing")
        elif pane_direction == "incoming":
            t.kind, t.direction, t.confidence = "incoming", "incoming", 0.95 if pane_verified else 0.8
            t.signals.append("pane_last_bubble_incoming")
        else:
            t.kind, t.confidence = "possible", 0.6 if preview_changed else 0.35
            if badge_cleared:
                t.signals.append("read_elsewhere_with_change")
    elif badge_cleared or badge_down:
        t.kind, t.direction, t.confidence = "read_elsewhere", "none", 0.9
    if pane_changed and t.kind in {"incoming", "outgoing"}:
        t.state_changed = True
        t.signals.append("verified_pane_changed")
    return _finish(t, prev, customer_id)


def _finish(t: Transition, prev: RowState | None, customer_id: str) -> Transition:
    prev_digest = prev.digest if prev else "-"
    prev_revision = prev.revision if prev else 0
    t.state_changed = t.state_changed or prev is None or prev.digest != t.new_state.digest or prev.position != t.new_state.position
    if t.state_changed:
        t.new_state = replace(t.new_state, revision=prev_revision + 1)
    if t.kind is not None:
        raw = f"{customer_id}:{prev_revision}:{prev_digest}:{t.new_state.digest}"
        t.dedup_key = "ev:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]
    return t
