"""Frame analysis: geometry/anchor validation, change detection, row OCR.

The analyzer turns one screenshot into a ``FrameObservation``. It never decides whether a
message is new; that is the tracker's job. OCR runs only for rows whose pixels changed,
except every ``full_validation_every_n_frames`` frames, when all rows are re-read.
"""

from __future__ import annotations

import datetime as dt
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace

from PIL import Image, ImageChops

from xianyu_monitor.detection.textnorm import normalize, parse_badge
from xianyu_monitor.ocr.base import EMPTY, OcrResult
from xianyu_monitor.ocr.pool import BLOCK_PREFIX, BUBBLE_PREFIX, OcrPool
from xianyu_monitor.runtime_settings import RuntimeSettings
from xianyu_monitor.vision import imageops
from xianyu_monitor.vision.pane import find_bubbles, has_send_failure
from xianyu_monitor.vision.profile import Profile


@dataclass
class RowObservation:
    index: int
    present: bool
    name: str = ""
    name_conf: float = 0.0
    preview: str = ""
    preview_conf: float = 0.0
    time_text: str = ""
    time_conf: float = 0.0
    badge_present: bool = False
    badge_count: int | None = None
    badge_conf: float = 0.0
    badge_digest: str = ""
    avatar_hash: str | None = None
    row_digest: str = ""
    selected: bool = False

    @property
    def min_text_conf(self) -> float:
        confs = [self.name_conf, self.preview_conf]
        return min(confs)


@dataclass
class FrameObservation:
    captured_at: dt.datetime
    width: int
    height: int
    status: str  # ok | unchanged | layout_mismatch | blank | challenge
    layout_issues: list[str] = field(default_factory=list)
    changed: bool = True
    ocr_performed: bool = False
    ocr_ms: float = 0.0
    analysis_ms: float = 0.0
    overall_badge_present: bool = False
    overall_unread: int | None = None
    rows: list[RowObservation] = field(default_factory=list)
    challenge_hits: list[str] = field(default_factory=list)
    pane: PaneObservation | None = None

    @property
    def usable(self) -> bool:
        return self.status in {"ok", "unchanged"}


@dataclass
class Bubble:
    direction: str
    text: str
    confidence: float
    box: tuple[int, int, int, int]


@dataclass
class PaneObservation:
    title: str
    title_conf: float
    bubbles: list[Bubble]
    composer_text: str
    composer_conf: float
    send_failure: bool = False

    @property
    def last_bubble(self) -> Bubble | None:
        return self.bubbles[-1] if self.bubbles else None


@dataclass
class _ProbeState:
    keywords: tuple[str, ...]
    digest: str
    image: Image.Image  # the last probe region whose every text line has been read
    hits: tuple[str, ...]
    incremental_reads: int = 0


def _rgb(image: Image.Image) -> Image.Image:
    return image if image.mode == "RGB" else image.convert("RGB")


class FrameAnalyzer:
    ROW_CACHE_SIZE = 512
    # Incremental safety OCR: changed rows are widened by this margin so that any text line
    # touching a changed pixel is read whole (dialog headings are well below 2x this).
    PROBE_BAND_MARGIN = 40
    PROBE_BAND_STRIP = 4
    PROBE_MAX_BAND_FRACTION = 0.6
    PROBE_FULL_EVERY_N_READS = 20

    def __init__(self, profile: Profile, ocr: OcrPool):
        self.profile = profile
        self.ocr = ocr
        self._prev_signature: str | None = None
        self._row_cache: OrderedDict[str, RowObservation] = OrderedDict()
        self._last: FrameObservation | None = None
        self.frame_counter = 0
        self._challenge_cache: _ProbeState | None = None
        self.probe_stats = {"full": 0, "incremental": 0, "reused": 0}

    def reset(self, profile: Profile | None = None) -> None:
        if profile is not None:
            self.profile = profile
        self._prev_signature = None
        self._row_cache.clear()
        self._last = None
        self._challenge_cache = None

    # ----- crops exactly as sent to OCR (also used by synthetic test fixtures) -----
    def field_crop(self, frame: Image.Image, index: int, field_name: str) -> Image.Image:
        return imageops.crop(frame, self.profile.field_box(index, field_name)).convert("RGB")

    def avatar_crop(self, frame: Image.Image, index: int) -> Image.Image:
        """Avatar pixels with the overlapping unread-badge area neutralized, so a badge
        appearing or changing does not alter the avatar fingerprint."""
        avatar_box = self.profile.field_box(index, "avatar")
        badge_box = self.profile.field_box(index, "badge")
        avatar = imageops.crop(frame, avatar_box).convert("RGB").copy()
        margin = 3  # badge rendering may spill a pixel or two beyond its calibrated box
        left = max(avatar_box[0], badge_box[0] - margin) - avatar_box[0]
        top = max(avatar_box[1], badge_box[1] - margin) - avatar_box[1]
        right = min(avatar_box[2], badge_box[2] + margin) - avatar_box[0]
        bottom = min(avatar_box[3], badge_box[3] + margin) - avatar_box[1]
        if right > left and bottom > top:
            avatar.paste((128, 128, 128), (left, top, right, bottom))
        return avatar

    def badge_ocr_image(self, crop: Image.Image) -> Image.Image:
        c = self.profile.colors
        return imageops.badge_glyph_image(crop, c.badge_red_min, c.badge_green_max, c.badge_blue_max).convert("RGB")

    def _badge_present(self, crop: Image.Image) -> bool:
        """Red area *and* light numeral strokes enclosed by red; a red patch inside an avatar
        picture alone is not a badge."""
        c = self.profile.colors
        if imageops.red_ratio(crop, c.badge_red_min, c.badge_green_max, c.badge_blue_max) < c.badge_min_ratio:
            return False
        glyph = imageops.badge_glyph_image(crop, c.badge_red_min, c.badge_green_max, c.badge_blue_max)
        ink = sum(1 for value in glyph.getdata() if value == 0)
        return ink >= 3

    def _row_selected(self, frame: Image.Image, index: int) -> bool:
        left, top, _, bottom = self.profile.row_box(index)
        strip = imageops.crop(frame, (left + 1, top + 2, left + 5, bottom - 2))
        c = self.profile.colors
        return imageops.color_close(imageops.mean_rgb(strip), c.selected_row, c.selected_tolerance)

    def check_layout(self, frame: Image.Image) -> tuple[str, list[str]]:
        p = self.profile
        if frame.size != (p.frame_width, p.frame_height):
            return "layout_mismatch", [f"frame_size:{frame.width}x{frame.height}!={p.frame_width}x{p.frame_height}"]
        small = frame.convert("L").resize((64, 36))
        if imageops.stddev(small) < 1.0:
            return "blank", ["blank_frame"]
        issues = []
        for anchor in p.anchors:
            if not anchor.dhash:
                continue
            region = imageops.crop(frame, anchor.region)
            distance = imageops.hamming(imageops.dhash(region), anchor.dhash)
            if distance > anchor.max_distance:
                issues.append(f"anchor:{anchor.name}:{distance}")
            elif anchor.mean is not None and abs(imageops.mean_gray(region) - anchor.mean) > anchor.max_mean_delta:
                issues.append(f"anchor:{anchor.name}:mean")
        return ("layout_mismatch" if issues else "ok"), issues

    async def analyze(self, frame: Image.Image, captured_at: dt.datetime, rt: RuntimeSettings,
                      force_full: bool = False) -> FrameObservation:
        started = time.perf_counter()
        self.frame_counter += 1
        frame = _rgb(frame)
        status, issues = self.check_layout(frame)
        probe_due = self.frame_counter % max(1, int(rt.challenge_probe_every_n_frames)) == 0
        challenge_hits: list[str] = []
        if self.profile.challenge_probe is not None and (status != "ok" or probe_due):
            challenge_hits = await self._probe_challenge(frame, rt)
        if challenge_hits:
            obs = FrameObservation(captured_at, frame.width, frame.height, "challenge", issues, True,
                                   True, self.ocr.last_batch_ms, 0.0, challenge_hits=challenge_hits)
            self._last = None
            obs.analysis_ms = (time.perf_counter() - started) * 1000.0
            return obs
        if status != "ok":
            self._last = None
            self._prev_signature = None
            return FrameObservation(captured_at, frame.width, frame.height, status, issues, True,
                                    analysis_ms=(time.perf_counter() - started) * 1000.0)

        # Exact per-row pixel digests are cheap and catch small changes (a 19px badge) that a
        # downsampled whole-list difference would miss.
        row_imgs = [imageops.crop(frame, self.profile.row_box(i)) for i in range(self.profile.row.count)]
        signature = "|".join(imageops.digest(img) for img in row_imgs)
        if self.profile.overall_unread_badge is not None:
            signature += "|" + imageops.digest(imageops.crop(frame, self.profile.overall_unread_badge))
        # An open conversation may receive messages without ever showing a row badge.
        for region in (self.profile.message_pane, self.profile.conversation_title):
            if region is not None:
                signature += "|" + imageops.digest(imageops.crop(frame, region))
        full_due = self.frame_counter % max(1, int(rt.full_validation_every_n_frames)) == 0
        if (self._last is not None and not force_full and not full_due and signature == self._prev_signature):
            obs = replace(self._last, captured_at=captured_at, status="unchanged", changed=False,
                          ocr_performed=False, ocr_ms=0.0)
            obs.analysis_ms = (time.perf_counter() - started) * 1000.0
            return obs
        self._prev_signature = signature
        if force_full or full_due:
            self._row_cache.clear()

        rows: list[RowObservation | None] = []
        requests: list[tuple[str, Image.Image]] = []
        pending: dict[int, RowObservation] = {}
        c = self.profile.colors
        for index, row_img in enumerate(row_imgs):
            selected = self._row_selected(frame, index)
            if imageops.stddev(row_img) <= c.blank_stddev_max:
                rows.append(RowObservation(index=index, present=False, selected=selected))
                continue
            row_digest = imageops.digest(row_img)
            cached = self._row_cache.get(row_digest)
            if cached is not None:
                self._row_cache.move_to_end(row_digest)
                rows.append(replace(cached, index=index, selected=selected))
                continue
            avatar = self.avatar_crop(frame, index)
            avatar_hash = imageops.dhash(avatar) if imageops.stddev(avatar) > 3.0 else None
            badge_crop = self.field_crop(frame, index, "badge")
            badge_present = self._badge_present(badge_crop)
            obs = RowObservation(index=index, present=True, badge_present=badge_present,
                                 badge_digest=imageops.digest(badge_crop)[:16], avatar_hash=avatar_hash,
                                 row_digest=row_digest, selected=selected)
            for field_name in ("name", "preview", "time"):
                requests.append((f"r{index}.{field_name}", self.field_crop(frame, index, field_name)))
            if badge_present:
                requests.append((f"r{index}.badge", self.badge_ocr_image(badge_crop)))
            pending[index] = obs
            rows.append(None)

        overall_present = False
        if self.profile.overall_unread_badge is not None:
            overall_crop = imageops.crop(frame, self.profile.overall_unread_badge)
            overall_present = self._badge_present(overall_crop)
            if overall_present:
                requests.append(("overall", self.badge_ocr_image(overall_crop)))

        results: dict[str, OcrResult] = await self.ocr.recognize(requests) if requests else {}
        for index, obs in pending.items():
            name = results.get(f"r{index}.name", EMPTY)
            preview = results.get(f"r{index}.preview", EMPTY)
            when = results.get(f"r{index}.time", EMPTY)
            obs.name, obs.name_conf = name.text.strip(), name.confidence
            obs.preview, obs.preview_conf = preview.text.strip(), preview.confidence
            obs.time_text, obs.time_conf = when.text.strip(), when.confidence
            if obs.badge_present:
                badge = results.get(f"r{index}.badge", EMPTY)
                obs.badge_count = parse_badge(badge.text)
                obs.badge_conf = badge.confidence if obs.badge_count is not None else 0.0
            if not obs.name and not obs.preview:
                # Pixels present but no readable text (e.g. loading skeleton): unknown, not empty.
                obs.name_conf = obs.preview_conf = 0.0
            rows[index] = obs
            self._row_cache[obs.row_digest] = replace(obs, selected=False)
            while len(self._row_cache) > self.ROW_CACHE_SIZE:
                self._row_cache.popitem(last=False)

        overall_value = parse_badge(results["overall"].text) if "overall" in results else None
        obs = FrameObservation(
            captured_at=captured_at, width=frame.width, height=frame.height, status="ok",
            changed=True, ocr_performed=bool(requests), ocr_ms=self.ocr.last_batch_ms if requests else 0.0,
            overall_badge_present=overall_present, overall_unread=overall_value,
            rows=[row for row in rows if row is not None],
        )
        obs.analysis_ms = (time.perf_counter() - started) * 1000.0
        self._last = obs
        return obs

    @staticmethod
    def _keyword_hits(text: str, keywords: list[str]) -> list[str]:
        text = normalize(text).replace(" ", "")
        hits = []
        for keyword in keywords:
            needle = normalize(keyword).replace(" ", "")
            if needle and needle in text:
                hits.append(keyword)
        return hits

    def _changed_bands(self, before: Image.Image, after: Image.Image) -> list[tuple[int, int]]:
        """Full-width horizontal bands covering every changed pixel plus a margin."""
        diff = ImageChops.difference(before, after)
        height, step, margin = after.height, self.PROBE_BAND_STRIP, self.PROBE_BAND_MARGIN
        bands: list[list[int]] = []
        for y in range(0, height, step):
            if diff.crop((0, y, after.width, min(height, y + step))).getbbox() is None:
                continue
            top, bottom = max(0, y - margin), min(height, y + step + margin)
            if bands and top <= bands[-1][1]:
                bands[-1][1] = max(bands[-1][1], bottom)
            else:
                bands.append([top, bottom])
        return [(top, bottom) for top, bottom in bands]

    async def _probe_challenge(self, frame: Image.Image, rt: RuntimeSettings) -> list[str]:
        """Safety OCR of the challenge probe region.

        Identical pixels and keywords reuse the previous answer. After a *negative* full read,
        later frames read only the bands that changed: every text line is then either
        unchanged since a read without hits or fully inside a band that is read now, so the
        answer equals a fresh full read without re-reading the whole (large) region. Positive
        states, keyword/size changes and every ``PROBE_FULL_EVERY_N_READS`` reads do a full read.
        """
        assert self.profile.challenge_probe is not None
        region = _rgb(imageops.crop(frame, self.profile.challenge_probe))
        keywords = tuple(rt.challenge_keywords)
        digest = imageops.digest(region)
        state = self._challenge_cache
        if state is not None and state.keywords == keywords and state.digest == digest:
            self.probe_stats["reused"] += 1
            return list(state.hits)
        if (state is not None and state.keywords == keywords and not state.hits
                and state.image.size == region.size and state.incremental_reads < self.PROBE_FULL_EVERY_N_READS):
            bands = self._changed_bands(state.image, region)
            if bands and sum(b - t for t, b in bands) <= self.PROBE_MAX_BAND_FRACTION * region.height:
                requests = [(BLOCK_PREFIX + f"probe_band{i}", region.crop((0, top, region.width, bottom)))
                            for i, (top, bottom) in enumerate(bands)]
                results = await self.ocr.recognize(requests)
                hits = []
                for key, _ in requests:
                    for hit in self._keyword_hits(results.get(key, EMPTY).text, list(keywords)):
                        if hit not in hits:
                            hits.append(hit)
                self.probe_stats["incremental"] += 1
                self._challenge_cache = _ProbeState(keywords, digest, region, tuple(hits),
                                                    state.incremental_reads + 1)
                return hits
        result = (await self.ocr.recognize([(BLOCK_PREFIX + "probe", region)])).get(BLOCK_PREFIX + "probe", EMPTY)
        hits = self._keyword_hits(result.text, list(keywords))
        self.probe_stats["full"] += 1
        self._challenge_cache = _ProbeState(keywords, digest, region, tuple(hits))
        return hits

    async def check_screen(self, frame: Image.Image, rt: RuntimeSettings) -> str:
        """Validate input safety without re-OCRing every unchanged list field."""
        status, _ = self.check_layout(frame)
        if self.profile.challenge_probe is not None and await self._probe_challenge(frame, rt):
            return "challenge"
        return status

    def probe_crop(self, frame: Image.Image) -> Image.Image | None:
        if self.profile.challenge_probe is None:
            return None
        return _rgb(imageops.crop(frame, self.profile.challenge_probe))

    # ----- conversation pane (reply targeting / verification) -----
    def bubble_text_crop(self, frame: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
        left, top, right, bottom = box
        inset = (left + 2, top + 2, max(left + 3, right - 2), max(top + 3, bottom - 2))
        return _rgb(imageops.crop(frame, inset))

    def title_crop(self, frame: Image.Image) -> Image.Image | None:
        if self.profile.conversation_title is None:
            return None
        return _rgb(imageops.crop(frame, self.profile.conversation_title))

    def composer_crop(self, frame: Image.Image) -> Image.Image | None:
        if self.profile.composer is None:
            return None
        return _rgb(imageops.crop(frame, self.profile.composer))

    async def analyze_pane(self, frame: Image.Image) -> PaneObservation:
        p = self.profile
        frame = _rgb(frame)
        requests: list[tuple[str, Image.Image]] = []
        title_crop = self.title_crop(frame)
        if title_crop is not None:
            requests.append(("title", title_crop))
        composer_crop = self.composer_crop(frame)
        if composer_crop is not None:
            # The textarea can wrap or contain explicit newlines. Read the whole
            # block so pre-send verification cannot silently accept a single line.
            requests.append((BLOCK_PREFIX + "composer", composer_crop))
        boxes = []
        if p.message_pane is not None:
            boxes = find_bubbles(frame, p.message_pane, p.colors, p.min_bubble_width, p.min_bubble_height)
            for i, bubble in enumerate(boxes):
                requests.append((BUBBLE_PREFIX + f"b{i}", self.bubble_text_crop(frame, bubble.box)))
        results = await self.ocr.recognize(requests)
        title = results.get("title", EMPTY)
        composer = results.get(BLOCK_PREFIX + "composer", EMPTY)
        bubbles = []
        for i, bubble in enumerate(boxes):
            res = results.get(BUBBLE_PREFIX + f"b{i}", EMPTY)
            bubbles.append(Bubble(bubble.direction, res.text.strip(), res.confidence, bubble.box))
        failed = any(b.direction == "outgoing" and has_send_failure(frame, b.box, p.message_pane, p.colors)
                     for b in boxes) if p.message_pane is not None else False
        return PaneObservation(title.text.strip(), title.confidence, bubbles, composer.text.strip(),
                               composer.confidence, failed)
