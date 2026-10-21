"""Image part of a reply; text has already been durably verified before entry."""
from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image

from xianyu_monitor.detection.textnorm import normalize_name
from xianyu_monitor.ocr.pool import BLOCK_PREFIX
from xianyu_monitor.replies.input import InputError
from xianyu_monitor.timeutil import utcnow
from xianyu_monitor.vision.pane import canonical_title, has_send_failure
from xianyu_monitor.vision.reply_image import find_outgoing_image, load_asset


@dataclass
class ImageOutcome:
    status: str
    reason: str
    submitted: bool = False
    pause_replies: bool = False
    details: dict = field(default_factory=dict)


class ImageReplyExecutor:
    def __init__(self, text_executor, matcher=find_outgoing_image):
        self.text = text_executor
        self.matcher = matcher

    async def _dialog(self, frame, rt, path=None):
        profile = self.text.analyzer.profile
        crops = [(BLOCK_PREFIX + "image_dialog", frame.crop(profile.image_dialog_title))]
        if path is not None:
            crops.append((BLOCK_PREFIX + "image_path", frame.crop(profile.image_dialog_path)))
            crops.append((BLOCK_PREFIX + "image_confirm", frame.crop(profile.image_dialog_confirm)))
        result = await self.text.analyzer.ocr.recognize(crops)
        title = result[BLOCK_PREFIX + "image_dialog"]
        good = title.confidence >= float(rt.ocr_min_confidence) and any(
            normalize_name(word) in normalize_name(title.text) for word in profile.image_dialog_keywords
        )
        if path is not None:
            entry = result[BLOCK_PREFIX + "image_path"]
            # GTK's insertion caret is sometimes OCR'd as a trailing vertical bar.
            good = good and entry.confidence >= float(rt.ocr_min_confidence) and entry.text.strip() in {
                str(path), str(path) + "|"
            }
            button = result[BLOCK_PREFIX + "image_confirm"]
            good = good and button.confidence >= float(rt.ocr_min_confidence) and normalize_name(button.text) in {
                "open", "打开"
            }
        return good

    async def execute(self, target, path, rt, set_phase, remember_asset, expected_sha256=None, on_observation=None):
        text, profile = self.text, self.text.analyzer.profile
        if not all([profile.image_attach_click, profile.image_dialog_title, profile.image_dialog_path,
                    profile.image_dialog_confirm, profile.image_dialog_confirm_click, profile.message_pane]):
            return ImageOutcome("failed", "image_controls_not_calibrated")
        try:
            asset = load_asset(path or "", expected_sha256)
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            return ImageOutcome("failed", "invalid_or_unavailable_image", details={"error_type": type(exc).__name__})
        if not await set_phase("image_preparing"):
            return ImageOutcome("uncertain", "image_lease_lost", pause_replies=True)
        await remember_asset(asset.sha256)
        before = await text.capture()
        status = await text.analyzer.check_screen(before, rt)
        pane = await text.analyzer.analyze_pane(before)
        newest = pane.last_bubble
        if (status != "ok" or not text._title_matches(pane, target, rt) or not text._composer_empty(pane)
                or pane.send_failure or newest is None or newest.direction != "outgoing"
                or newest.confidence < float(rt.ocr_min_confidence)
                or normalize_name(newest.text) != normalize_name(target.reply_text)):
            return ImageOutcome("cancelled", "image_target_or_context_changed")
        baseline = self.matcher(before, asset.image, profile.message_pane, profile.image_match_threshold)
        dialog_open, submitted = False, False
        try:
            if not await set_phase("image_dialog"):
                return ImageOutcome("uncertain", "image_lease_lost", pause_replies=True)
            await text.input.click(*profile.image_attach_click)
            await text.sleep(float(rt.reply_step_delay_seconds))
            frame = await text.capture()
            if not await self._dialog(frame, rt):
                return ImageOutcome("uncertain", "image_file_dialog_not_verified", pause_replies=True)
            dialog_open = True
            if not await set_phase("image_path"):
                return ImageOutcome("uncertain", "image_lease_lost", pause_replies=True)
            await text.input.press("ctrl+l")
            await text.sleep(float(rt.reply_step_delay_seconds))
            await text.input.type_text(str(asset.path))
            # GTK consumes Tab for filename completion; Ctrl+Tab moves focus away
            # so its insertion caret cannot be OCR'd as part of the exact path.
            await text.input.press("ctrl+Tab")
            await text.sleep(float(rt.reply_step_delay_seconds))
            frame = await text.capture()
            if not await self._dialog(frame, rt, asset.path):
                return ImageOutcome("failed", "image_path_not_verified")
            load_asset(str(asset.path), asset.sha256)  # no file replacement before irreversible selection
            if not await set_phase("image_submitting"):
                return ImageOutcome("uncertain", "image_lease_lost", pause_replies=True)
            # On the board GTK Return can close the chooser with a cancel event.
            # Only click the screenshot-verified Open button, once.
            await text.input.click(*profile.image_dialog_confirm_click)
            submitted = True
            dialog_open = False
            started = text.monotonic()
            deadline = started + float(rt.reply_image_timeout_seconds)
            verified_since = None
            checks, last = 0, {}
            def evidence():
                return {"selection_method": "verified_open_button", "verification_checks": checks,
                        "elapsed_seconds": round(text.monotonic() - started, 2), "last_observation": last}
            while True:
                if not await set_phase("image_verifying"):
                    return ImageOutcome("uncertain", "image_lease_lost", submitted=True, pause_replies=True)
                await text.sleep(max(.2, float(rt.reply_step_delay_seconds)))
                frame = await text.capture()
                obs = await text.analyzer.analyze(frame, utcnow(), rt)
                checks += 1
                last = {"screen_status": obs.status}
                if obs.status not in {"ok", "unchanged"}:
                    if text.monotonic() >= deadline:
                        break
                    continue
                title = (await text.analyzer.ocr.recognize([("title", text.analyzer.title_crop(frame))]))["title"]
                if title.confidence < float(rt.ocr_min_confidence) or canonical_title(title.text) not in target.aliases:
                    return ImageOutcome("uncertain", "image_target_changed", submitted=True, pause_replies=True)
                rows = [r for r in obs.rows if r.selected and normalize_name(r.name) in target.aliases]
                preview_ok = (len(rows) == 1 and rows[0].preview_conf >= float(rt.ocr_min_confidence)
                              and normalize_name(rows[0].preview) in {"[图片]", "图片", "[image]", "image"})
                if not preview_ok and on_observation is not None:
                    await on_observation(frame, obs)
                match = self.matcher(frame, asset.image, profile.message_pane, profile.image_match_threshold)
                if match and has_send_failure(frame, match.box, profile.message_pane, profile.colors):
                    return ImageOutcome("uncertain", "image_send_failure_indicator", submitted=True, pause_replies=True)
                novel = match and (baseline is None or match.box != baseline.box)
                last.update({"title_verified": True, "preview_is_image": preview_ok,
                             "image_matches": bool(match), "novel_image": bool(novel),
                             "match_score": round(match.score, 4) if match else None})
                if preview_ok and novel:
                    verified_since = verified_since if verified_since is not None else text.monotonic()
                elif not novel:
                    verified_since = None
                # A later incoming preview must not erase previously observed image evidence.
                if verified_since is not None and novel and text.monotonic() - verified_since >= float(rt.reply_image_verify_seconds):
                    return ImageOutcome("sent_verified", "outgoing_image_matches_no_failure_observed", submitted=True,
                                        details={"match_score": round(match.score, 4), "sha256": asset.sha256,
                                                 **evidence()})
                if text.monotonic() >= deadline:
                    break
            return ImageOutcome("uncertain", "image_not_confirmed_within_timeout", submitted=True,
                                pause_replies=True, details=evidence())
        except (InputError, OSError, ValueError) as exc:
            return ImageOutcome("uncertain" if submitted else "failed", "image_input_or_file_error",
                                submitted=submitted, pause_replies=submitted, details={"error_type": type(exc).__name__})
        finally:
            if dialog_open:
                # First Escape exits GTK location entry; second cancels our verified chooser.
                await text.input.press("Escape")
                await text.input.press("Escape")
