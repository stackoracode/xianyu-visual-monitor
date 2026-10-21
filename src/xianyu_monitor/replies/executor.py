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
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from PIL import Image

from xianyu_monitor.detection.textnorm import normalize, normalize_name, similarity, strip_ellipsis
from xianyu_monitor.replies.input import InputAdapter, InputError
from xianyu_monitor.runtime_settings import RuntimeSettings
from xianyu_monitor.timeutil import utcnow
from xianyu_monitor.vision.analyzer import FrameAnalyzer, PaneObservation
from xianyu_monitor.vision.imageops import hamming
from xianyu_monitor.vision.pane import canonical_title

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
    status: str  # sent_verified | sent_unverified | superseded | aborted | dry_run
    reason: str
    pause_replies: bool = False
    clicked: bool = False
    typed: bool = False
    submitted: bool = False
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
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 monotonic: Callable[[], float] = time.monotonic):
        self.analyzer = analyzer
        self.capture = capture
        self.input = input_adapter
        self.sleep = sleep
        self.monotonic = monotonic

    async def _pane(self, rt: RuntimeSettings, now) -> tuple[PaneObservation, str]:
        frame = await self.capture()
        status = await self.analyzer.check_screen(frame, rt)
        return await self.analyzer.analyze_pane(frame), status

    def _title_matches(self, pane: PaneObservation, target: ReplyTarget, rt: RuntimeSettings) -> bool:
        if pane.title_conf < float(rt.ocr_min_confidence):
            return False
        title = canonical_title(pane.title)
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
        before any click, so it never opens the conversation or marks it read.

        The outcome records how long each phase took (``phase_seconds``) so latency can be
        measured on the board without screenshots or message text."""
        marks: list[tuple[str, float]] = []

        async def timed_phase(phase: str) -> bool:
            marks.append((phase, self.monotonic()))
            return await set_phase(phase)

        started = self.monotonic()
        outcome = await self._execute(target, rt, timed_phase, now, dry_run)
        finished = self.monotonic()
        seconds: dict[str, float] = {}
        for i, (phase, at) in enumerate(marks):
            end = marks[i + 1][1] if i + 1 < len(marks) else finished
            seconds[phase] = round(seconds.get(phase, 0.0) + end - at, 3)
        outcome.details.setdefault("phase_seconds", seconds)
        outcome.details.setdefault("elapsed_seconds", round(finished - started, 3))
        return outcome

    async def _execute(self, target: ReplyTarget, rt: RuntimeSettings, set_phase: PhaseFn,
                       now: Callable[[], dt.datetime], dry_run: bool) -> ReplyOutcome:
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
            status = "superseded" if row.preview_conf >= min_conf else "aborted"
            return ReplyOutcome(status, "trigger_stale_or_unreadable", details={"preview": row.preview})

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
            return ReplyOutcome("superseded", "latest_bubble_differs_from_trigger", clicked=True,
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
        step = max(0.1, float(rt.reply_step_delay_seconds))
        deadline = self.monotonic() + float(rt.reply_verify_timeout_seconds)
        last_seen: dict = {}
        verified = False
        while True:
            if not await set_phase("verifying_send"):
                return ReplyOutcome("sent_unverified", "lease_lost_during_verification", pause_replies=True,
                                    clicked=True, typed=True, submitted=True)
            await self.sleep(min(step, max(0.0, deadline - self.monotonic())))
            sampled_at = self.monotonic()
            pane, status = await self._pane(rt, now)
            if status != "ok" or not self._title_matches(pane, target, rt) or pane.send_failure:
                return ReplyOutcome("sent_unverified", "send_failure_or_target_changed", pause_replies=True,
                                    clicked=True, typed=True, submitted=True)
            newest = pane.last_bubble
            # Keep previously observed send evidence when a new incoming bubble
            # arrives during verification. Still require that outgoing bubble
            # to remain visible and check failure indicators on every sample.
            matching_visible = any(
                b.direction == "outgoing" and b.confidence >= min_conf
                and normalize(b.text).replace(" ", "") == normalize(target.reply_text).replace(" ", "")
                for b in pane.bubbles
            )
            verified = verified and matching_visible and self._composer_empty(pane)
            if newest is not None:
                last_seen = {"direction": newest.direction, "text": newest.text, "bubbles": len(pane.bubbles)}
                if (newest.direction == "outgoing" and newest.confidence >= min_conf
                        and normalize(newest.text).replace(" ", "") == normalize(target.reply_text).replace(" ", "")
                        and self._composer_empty(pane)):
                    verified = True
            # OCR itself consumes time. Still inspect a frame captured at/after
            # the deadline, rather than accepting an early frame analyzed slowly.
            if sampled_at >= deadline:
                break
        if verified:
            return ReplyOutcome("sent_verified", "outgoing_bubble_matches_no_failure_observed", clicked=True, typed=True,
                                submitted=True,
                                details={**last_seen, "bubbles_before": before_count})
        return ReplyOutcome("sent_unverified", "send_not_confirmed_within_timeout", pause_replies=True,
                            clicked=True, typed=True, submitted=True, details=last_seen)

    async def return_to_list(self, target: ReplyTarget, rt: RuntimeSettings, now=utcnow, before_leave=None) -> dict:
        """Leave a finished/cancelled action or an idle chat; preserve drafts and target changes."""
        frame = await self.capture()
        obs = await self.analyzer.analyze(frame, now(), rt)
        if obs.status not in {"ok", "unchanged"}:
            return {"returned": False, "reason": "screen_not_ready"}
        pane = await self.analyzer.analyze_pane(frame)
        if not self._title_matches(pane, target, rt):
            return {"returned": False, "reason": "owner_changed_target_or_draft_present"}
        if before_leave is not None:
            obs.pane = pane
            await before_leave(frame, obs)
        if (not self._composer_empty(pane) or pane.composer_conf < float(rt.ocr_min_confidence)):
            return {"returned": False, "reason": "owner_changed_target_or_draft_present"}
        try:
            await self.input.press("F5")
        except InputError:
            return {"returned": False, "reason": "refresh_input_error", "pause_replies": True}
        self.analyzer.reset()
        deadline = self.monotonic() + float(rt.reply_refresh_timeout_seconds)
        while True:
            await self.sleep(max(0.1, float(rt.reply_step_delay_seconds)))
            frame = await self.capture()
            obs = await self.analyzer.analyze(frame, now(), rt, force_full=True)
            if obs.status == "challenge":
                return {"returned": False, "reason": "refresh_requires_login_or_verification", "pause_replies": True}
            if obs.status == "ok":
                pane = await self.analyzer.analyze_pane(frame)
                if (any(r.present and r.name_conf >= float(rt.ocr_min_confidence) for r in obs.rows)
                        and not any(r.selected for r in obs.rows) and not canonical_title(pane.title)
                        and self._composer_empty(pane)):
                    return {"returned": True, "reason": "refreshed_to_conversation_list"}
            if self.monotonic() >= deadline:
                return {"returned": False, "reason": "refresh_return_not_verified", "pause_replies": True}
