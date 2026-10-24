"""Opt-in, calibrated recovery of known network/login screens, and idle-list refresh.

No DOM, credentials, QR interaction or arbitrary confirmation clicks. Every input is
persisted before execution, bounded to once per recovery episode, and rechecked fresh.
"""
from __future__ import annotations

import time

from xianyu_monitor.ocr.base import EMPTY
from xianyu_monitor.replies.executor import ReplyExecutor
from xianyu_monitor.vision.imageops import dhash, digest, hamming


def compact(text: str) -> str:
    return "".join(text.split()).casefold()


class PageMaintenance:
    def __init__(self, analyzer, capture, input_adapter, online, persist, clock=time.time, guard=None):
        self.analyzer, self.capture, self.input = analyzer, capture, input_adapter
        self.online, self.persist, self.clock = online, persist, clock
        self.guard = guard
        self.executor = ReplyExecutor(analyzer, capture, input_adapter)

    async def kind(self, frame, rt) -> str | None:
        p = self.analyzer.profile
        c = p.page_recovery
        if c is None or frame.size != (p.frame_width, p.frame_height):
            return None
        boxes = {key: getattr(c, key) for key in
                 ("network_title", "reconnect_button", "login_heading", "quick_button", "quick_account")}
        reads = await self.analyzer.ocr.recognize([(key, frame.crop(box)) for key, box in boxes.items()])

        def is_text(key, expected):
            value = reads.get(key, EMPTY)
            return value.confidence >= float(rt.ocr_min_confidence) and compact(value.text) == compact(expected)

        if is_text("network_title", "连接中断，请重连") and is_text("reconnect_button", "重连"):
            return "network"
        if is_text("login_heading", "手机扫码安全登录"):
            if (is_text("quick_button", "快速进入") and is_text("quick_account", c.expected_account)
                    and hamming(dhash(frame.crop(c.quick_avatar)), c.expected_avatar_hash) <= 4):
                return "quick"
            return "unsafe_login"
        return None

    async def ready(self, frame, rt) -> bool:
        from xianyu_monitor.timeutil import utcnow
        p = self.analyzer.profile
        if await self.analyzer.check_screen(frame, rt) != "ok":
            return False
        obs = await self.analyzer.analyze(frame, utcnow(), rt, force_full=True)
        if (obs.status not in {"ok", "unchanged"} or not any(row.present for row in obs.rows)
                or any(row.selected for row in obs.rows)):
            return False
        pane = await self.analyzer.analyze_pane(frame)
        if (pane.title.strip() or not self.executor._composer_empty(pane)
                or pane.composer_conf < float(rt.ocr_min_confidence)):
            return False
        seller = (await self.analyzer.ocr.recognize([("seller", frame.crop(p.page_recovery.seller))])).get("seller", EMPTY)
        return (seller.confidence >= float(rt.ocr_min_confidence)
                and compact(seller.text) == compact(p.page_recovery.expected_seller))

    async def tick(self, rt, state: dict, *, periodic=False) -> bool:
        """True consumes the frame: no detection/replies while recovery is unresolved."""
        if (rt.dry_run or rt.monitor_mode != "active" or self.analyzer.profile.page_recovery is None
                or not (rt.page_recovery_enabled or rt.page_refresh_enabled)):
            return False
        now = self.clock()
        frame = await self.capture()
        if frame.size != (self.analyzer.profile.frame_width, self.analyzer.profile.frame_height):
            return bool(state.get("active"))
        kind = await self.kind(frame, rt)
        if state.get("active") and kind is None and await self.online() and await self.ready(frame, rt):
            state["ready_samples"] = state.get("ready_samples", 0) + 1
            if state["ready_samples"] >= 2:
                state.update(active=False, blocked=False, phase="ready", last_refresh_at=now,
                             reconnect=False, refreshed=False, quick=False, next_action_at=0)
                self.analyzer.reset()
            await self.persist(state)
            return True
        if state.get("active") and state.get("ready_samples"):
            state["ready_samples"] = 0
            await self.persist(state)  # interrupted readiness must also survive a restart
        if state.get("blocked"):
            return True
        if now < state.get("next_action_at", 0):
            return bool(state.get("active"))
        if not state.get("active"):
            if kind is not None and rt.page_recovery_enabled:
                state.update(active=True, phase="detected", started_at=now, reconnect=False,
                             refreshed=False, quick=False, ready_samples=0, periodic=False, deadline=now + 180)
                await self.persist(state)
            elif periodic and rt.page_refresh_enabled:
                if "last_refresh_at" not in state:
                    state["last_refresh_at"] = now
                    await self.persist(state)
                    return False
                if now - state["last_refresh_at"] < rt.page_refresh_interval_minutes * 60:
                    return False
                if not await self.ready(frame, rt):
                    return False  # selected chat, draft, unknown account or layout: defer
                state.update(active=True, phase="periodic_refresh", started_at=now,
                             reconnect=True, refreshed=False, quick=False, ready_samples=0, periodic=True, deadline=now + 180)
                await self.persist(state)
            else:
                return False

        async def block(reason):
            state.update(blocked=True, phase=reason)
            await self.persist(state)
            return True

        if kind == "unsafe_login":
            return await block("cached_account_not_verified")
        if not await self.online():
            state.update(phase="waiting_for_network", next_action_at=now + 30, deadline=now + 180)
            await self.persist(state)
            return True
        if now > state.get("deadline", now + 180):
            return await block("recovery_timeout")
        # Other verification prompts must not be hidden by an allowed cached-login panel.
        hits = await self.analyzer._probe_challenge(frame, rt) if self.analyzer.profile.challenge_probe else []
        if any(hit != "手机扫码安全登录" or kind != "quick" for hit in hits):
            return await block("verification_requires_owner")
        if kind == "quick":
            if not rt.page_quick_enter_enabled:
                return await block("quick_enter_not_authorized")
            if state.get("quick"):
                state.update(phase="waiting_for_quick_enter", next_action_at=now + 5)
                await self.persist(state)
                return True
            action, box = "quick", self.analyzer.profile.page_recovery.quick_button
        elif kind == "network" and not state.get("reconnect"):
            action, box = "reconnect", self.analyzer.profile.page_recovery.reconnect_button
        elif (kind == "network" or state.get("periodic")) and not state.get("refreshed"):
            pane = await self.analyzer.analyze_pane(frame)
            if not self.executor._composer_empty(pane) or pane.composer_conf < float(rt.ocr_min_confidence):
                return await block("draft_prevents_refresh")
            action, box = "refreshed", None
        else:
            state["next_action_at"] = now + 5
            await self.persist(state)
            return True
        if not await self.input.is_browser_active():
            return await block("browser_not_active")
        fresh = await self.capture()
        if fresh.size != frame.size or await self.kind(fresh, rt) != kind:
            return await block("screen_changed_before_input")
        if box is not None and digest(fresh.crop(box)) != digest(frame.crop(box)):
            return await block("button_changed_before_input")
        # Recheck draft after the fresh capture, including for periodic refresh.
        if action == "refreshed":
            if state.get("periodic") and not await self.ready(fresh, rt):
                return await block("page_changed_before_refresh")
            pane = await self.analyzer.analyze_pane(fresh)
            if not self.executor._composer_empty(pane) or pane.composer_conf < float(rt.ocr_min_confidence):
                return await block("draft_prevents_refresh")
        fresh_hits = await self.analyzer._probe_challenge(fresh, rt) if self.analyzer.profile.challenge_probe else []
        if any(hit != "手机扫码安全登录" or kind != "quick" for hit in fresh_hits):
            return await block("verification_requires_owner")
        if not await self.input.is_browser_active():
            return await block("browser_not_active")
        if self.guard is not None and not await self.guard():
            return await block("owner_paused_before_input")
        state.update({action: True, "phase": action, "last_action_at": self.clock(),
                      "next_action_at": self.clock() + rt.page_recovery_wait_seconds})
        await self.persist(state)  # includes attempt marker; restart never repeats the input
        if box is None:
            await self.input.press("F5")
        else:
            await self.input.click((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
        self.analyzer.reset()
        return True
