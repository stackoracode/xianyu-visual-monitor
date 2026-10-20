"""Keyword-reply execution with conversation targeting and send-result verification.

Sequence (each step re-captures the screen and re-checks; any doubt aborts):

1. ``precheck``          - healthy layout, exactly one visible row for the target identity,
                           row preview still matches the triggering text (not stale).
2. ``targeting``         - click that row. Note: opening a conversation marks it read.
3. ``verifying_target``  - the conversation title matches the customer, the latest bubble is
                           incoming and matches the trigger, and the composer is empty (the
                           owner is not typing).
4. ``typing``            - persisted *before* any keystroke; from here on a crash leaves the
                           job ``uncertain`` and it is never retried automatically.
5. ``verifying_draft``   - recheck layout, target, trigger and the full pasted draft.
6. ``verifying_send``    - watch for failed-send indicators throughout the timeout.

Unverifiable sends are ``sent_unverified`` and pause automatic replies for the owner.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from PIL import Image

from xianyu_monitor.detection.textnorm import normalize, normalize_name, similarity, strip_ellipsis
from xianyu_monitor.replies.input import InputAdapter, InputError
from xianyu_monitor.runtime_settings import RuntimeSettings
from xianyu_monitor.timeutil import utcnow
from xianyu_monitor.vision.analyzer import FrameAnalyzer, PaneObservation
from xianyu_monitor.vision.imageops import hamming

log = logging.getLogger(__name__)

CaptureFn = Callable[[], Awaitable[Image.Image]]
PhaseFn = Callable[[str], Awaitable[bool]]


@dataclass
class ReplyTarget:
    customer_id: str
    aliases: set[str]
    avatars: set[str]
    trigger_text: str
    reply_text: str


@dataclass
class ReplyOutcome:
    status: str  # sent_verified | sent_unverified | aborted | dry_run
    reason: str
    pause_replies: bool = False
    clicked: bool = False
    typed: bool = False
    details: dict = field(default_factory=dict)


def _text_matches(observed: str, expected: str, threshold: float) -> bool:
    a, b = normalize(observed).replace(" ", ""), normalize(strip_ellipsis(expected)).replace(" ", "")
    if not a or not b:
        return False
    if similarity(a, b) >= threshold:
        return True
    # A list preview may be a truncated prefix of the full bubble text.
    return len(b) >= 4 and a.startswith(b)


class ReplyExecutor:
    def __init__(self, analyzer: FrameAnalyzer, capture: CaptureFn, input_adapter: InputAdapter,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.analyzer = analyzer
        self.capture = capture
        self.input = input_adapter
        self.sleep = sleep

    async def _pane(self, rt: RuntimeSettings, now) -> tuple[PaneObservation, str]:
        frame = await self.capture()
        obs = await self.analyzer.analyze(frame, now(), rt.with_overrides(challenge_probe_every_n_frames=1),
                                          force_full=True)
        return await self.analyzer.analyze_pane(frame), obs.status

    def _title_matches(self, pane: PaneObservation, target: ReplyTarget, rt: RuntimeSettings) -> bool:
        if pane.title_conf < float(rt.ocr_min_confidence):
            return False
        title = normalize_name(pane.title)
        # The page appends a masked account ID in parentheses. Remove only that
        # explicit decoration, never accept an arbitrary display-name prefix.
        title = re.sub(r"\([^()]*\*{2,}[^()]*\)$", "", title)
        return bool(title) and title in target.aliases

    def _composer_empty(self, pane: PaneObservation) -> bool:
        text = normalize(pane.composer_text).replace(" ", "")
        if not text:
            return True
        placeholder = normalize(self.analyzer.profile.composer_placeholder).replace(" ", "")
        return bool(placeholder) and similarity(text, placeholder) >= 0.6

    async def execute(self, target: ReplyTarget, rt: RuntimeSettings, set_phase: PhaseFn,
                      now: Callable[[], dt.datetime] = utcnow, dry_run: bool = False) -> ReplyOutcome:
        """Run the reply sequence. ``dry_run`` performs the read-only precheck and stops
        before any click, so it never opens the conversation or marks it read."""
        profile = self.analyzer.profile
        threshold = float(rt.reply_text_match_similarity)
        min_conf = float(rt.ocr_min_confidence)
        if (profile.composer_click is None or profile.composer is None or profile.message_pane is None
                or profile.conversation_title is None):
            return ReplyOutcome("aborted", "calibration_incomplete_for_replies", pause_replies=True)

        # 1. precheck
        if not await set_phase("precheck"):
            return ReplyOutcome("aborted", "lease_lost")
        frame = await self.capture()
        obs = await self.analyzer.analyze(frame, now(), rt, force_full=True)
        if obs.status == "challenge":
            return ReplyOutcome("aborted", "challenge_detected", pause_replies=True)
        if obs.status != "ok":
            return ReplyOutcome("aborted", f"screen_not_ready:{obs.status}")
        rows = [r for r in obs.rows if r.present and r.name_conf >= min_conf
                and normalize_name(r.name) in target.aliases]
        if len(rows) != 1:
            return ReplyOutcome("aborted", "target_row_not_unique" if rows else "target_row_not_visible",
                                details={"matches": len(rows)})
        row = rows[0]
        if row.avatar_hash and target.avatars:
            distance = min(hamming(row.avatar_hash, a) for a in target.avatars)
            if distance > int(rt.avatar_conflict_distance):
                return ReplyOutcome("aborted", "target_avatar_conflict", details={"distance": distance})
        if row.preview_conf < min_conf or not _text_matches(row.preview, target.trigger_text, threshold):
            return ReplyOutcome("aborted", "trigger_stale_or_unreadable", details={"preview": row.preview})

        if dry_run:
            return ReplyOutcome("dry_run", "precheck_passed_no_input_sent", details={"row": row.index})

        # 2. targeting
        if not await set_phase("targeting"):
            return ReplyOutcome("aborted", "lease_lost")
        x, y = profile.row_center(row.index)
        try:
            await self.input.click(x, y)
        except InputError as exc:
            return ReplyOutcome("aborted", f"input_error:{exc}", pause_replies=True)
        await self.sleep(float(rt.reply_step_delay_seconds))

        # 3. verify target conversation
        if not await set_phase("verifying_target"):
            return ReplyOutcome("aborted", "lease_lost", clicked=True)
        pane, status = await self._pane(rt, now)
        if status != "ok" or pane.send_failure:
            return ReplyOutcome("aborted", "unsafe_target_screen", pause_replies=True, clicked=True)
        if not self._title_matches(pane, target, rt):
            return ReplyOutcome("aborted", "conversation_title_mismatch", pause_replies=True, clicked=True,
                                details={"title": pane.title})
        last = pane.last_bubble
        if last is None or last.confidence < min_conf:
            return ReplyOutcome("aborted", "cannot_read_latest_bubble", clicked=True)
        if last.direction != "incoming":
            return ReplyOutcome("aborted", "latest_bubble_not_incoming", clicked=True,
                                details={"direction": last.direction})
        if not _text_matches(last.text, target.trigger_text, threshold):
            return ReplyOutcome("aborted", "latest_bubble_differs_from_trigger", clicked=True,
                                details={"bubble": last.text})
        if not self._composer_empty(pane):
            return ReplyOutcome("aborted", "composer_not_empty_owner_may_be_typing", clicked=True)
        before_count = len(pane.bubbles)

        # 4. type and send (point of no return)
        if not await set_phase("typing"):
            return ReplyOutcome("aborted", "lease_lost", clicked=True)
        try:
            cx, cy = profile.composer_click
            await self.input.click(cx, cy)
            await self.input.type_text(target.reply_text)
            await self.sleep(float(rt.reply_step_delay_seconds))
            if not await set_phase("verifying_draft"):
                return ReplyOutcome("aborted", "lease_lost_after_typing", pause_replies=True, clicked=True, typed=True)
            pane, status = await self._pane(rt, now)
            latest = pane.last_bubble
            draft_ok = (pane.composer_conf >= min_conf and
                        normalize(pane.composer_text).replace(" ", "") == normalize(target.reply_text).replace(" ", ""))
            trigger_ok = (latest is not None and latest.confidence >= min_conf and latest.direction == "incoming"
                          and normalize(latest.text) == normalize(last.text)
                          and _text_matches(latest.text, target.trigger_text, threshold))
            if (status != "ok" or pane.send_failure or not self._title_matches(pane, target, rt)
                    or not draft_ok or not trigger_ok):
                return ReplyOutcome("aborted", "draft_or_target_recheck_failed", pause_replies=True,
                                    clicked=True, typed=True)
            if not await set_phase("sending"):
                return ReplyOutcome("aborted", "lease_lost_after_typing", pause_replies=True, clicked=True, typed=True)
            await self.input.press("Return")
        except InputError as exc:
            return ReplyOutcome("sent_unverified", f"input_error_during_typing:{exc}", pause_replies=True,
                                clicked=True, typed=True)

        # 5. verify the send result
        await set_phase("verifying_send")
        step = max(0.1, float(rt.reply_step_delay_seconds))
        polls = max(1, int(float(rt.reply_verify_timeout_seconds) / step + 0.999))
        last_seen: dict = {}
        verified = False
        for _ in range(polls):
            await self.sleep(step)
            pane, status = await self._pane(rt, now)
            if status != "ok" or not self._title_matches(pane, target, rt) or pane.send_failure:
                return ReplyOutcome("sent_unverified", "send_failure_or_target_changed", pause_replies=True,
                                    clicked=True, typed=True)
            newest = pane.last_bubble
            verified = False
            if newest is not None:
                last_seen = {"direction": newest.direction, "text": newest.text, "bubbles": len(pane.bubbles)}
                if (newest.direction == "outgoing" and newest.confidence >= min_conf
                        and normalize(newest.text).replace(" ", "") == normalize(target.reply_text).replace(" ", "")
                        and self._composer_empty(pane)):
                    verified = True
        if verified:
            return ReplyOutcome("sent_verified", "outgoing_bubble_matches_no_failure_observed", clicked=True, typed=True,
                                details={**last_seen, "bubbles_before": before_count})
        return ReplyOutcome("sent_unverified", "send_not_confirmed_within_timeout", pause_replies=True,
                            clicked=True, typed=True, details=last_seen)
