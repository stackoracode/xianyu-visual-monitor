"""Synthetic chat-desktop renderer and fake desktop for tests.

Frames are drawn from scratch with Pillow using the calibration geometry: colored panels,
patterned avatars, red badges, bubbles. Text is drawn as a short hash placeholder so every
distinct string produces distinct pixels; the ground-truth string is registered with the
content-addressed ``FakeOcrEngine`` through the analyzer's own crop functions. No real
screenshots, names or UI assets are used.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from xianyu_monitor.ocr.base import FakeOcrEngine
from xianyu_monitor.ocr.pool import OcrPool
from xianyu_monitor.vision.analyzer import FrameAnalyzer
from xianyu_monitor.vision.pane import find_bubbles
from xianyu_monitor.vision.profile import Profile

EXAMPLE = Path(__file__).resolve().parents[1] / "src" / "xianyu_monitor" / "examples" / "calibration.example.json"
FONT = ImageFont.load_default()
INK = (24, 28, 24)
LIST_BG = (248, 248, 248)
PANE_BG = (248, 252, 248)
DESKTOP = (40, 90, 160)


@dataclass
class SRow:
    name: str
    preview: str
    time: str = "刚刚"
    badge: int | None = None
    avatar: int = 1
    selected: bool = False
    name_conf: float = 0.98
    preview_conf: float = 0.97
    time_conf: float = 0.97


@dataclass
class SBubble:
    direction: str  # incoming | outgoing
    text: str


@dataclass
class Scene:
    rows: list[SRow] = field(default_factory=list)
    overall: int | None = None
    title: str = ""
    bubbles: list[SBubble] = field(default_factory=list)
    composer: str = ""
    challenge: str | None = None
    cover_header: bool = False
    size: tuple[int, int] | None = None

    def copy(self) -> Scene:
        return copy.deepcopy(self)


def base_profile_dict() -> dict:
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return data


def placeholder(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


REAL_FONT: ImageFont.FreeTypeFont | None = None  # set by real-OCR tests to draw actual glyphs


def _draw_text(draw: ImageDraw.ImageDraw, box, text: str) -> None:
    if not text:
        return
    if REAL_FONT is not None:
        draw.text((box[0] + 1, box[1] + 1), text, fill=INK, font=REAL_FONT)
    else:
        draw.text((box[0] + 2, box[1] + 2), placeholder(text), fill=INK, font=FONT)


def _avatar(draw: ImageDraw.ImageDraw, box, seed: int) -> None:
    rng = random.Random(seed)
    left, top, right, bottom = box
    cell_w, cell_h = (right - left) / 4, (bottom - top) / 4
    for gy in range(4):
        for gx in range(4):
            color = (rng.randint(30, 225), rng.randint(130, 225), rng.randint(130, 225))  # never badge-red
            draw.rectangle((left + gx * cell_w, top + gy * cell_h, left + (gx + 1) * cell_w - 1,
                            top + (gy + 1) * cell_h - 1), fill=color)


def _badge(draw: ImageDraw.ImageDraw, box, value: int) -> None:
    draw.ellipse(box, fill=(248, 76, 72))
    label = str(value) if value < 100 else "99+"
    font = REAL_FONT if REAL_FONT is not None else FONT
    draw.text((box[0] + 5, box[1] + 1 if REAL_FONT is not None else box[1] + 3), label, fill=(248, 248, 248), font=font)


def draw_scene(scene: Scene, profile: Profile) -> Image.Image:
    size = scene.size or (profile.frame_width, profile.frame_height)
    image = Image.new("RGB", size, DESKTOP)
    draw = ImageDraw.Draw(image)
    if scene.size is not None and scene.size != (profile.frame_width, profile.frame_height):
        draw.rectangle((10, 10, size[0] - 10, size[1] - 10), fill=LIST_BG)
        return image
    draw.rectangle((44, 120, 1085, 669), fill=PANE_BG)
    draw.rectangle((135, 135, 225, 182), fill=(248, 228, 8))
    draw.rectangle((150, 145, 210, 172), fill=INK)
    draw.rectangle((44, 231, 374, 669), fill=LIST_BG)
    draw.text((52, 252), "MSG", fill=INK, font=FONT)
    for x in range(60, 100, 8):  # high-contrast header glyph used as a layout anchor
        draw.rectangle((x, 262, x + 3, 275), fill=INK)
    for index, row in enumerate(scene.rows[:profile.row.count]):
        draw.rectangle(profile.row_box(index), fill=(232, 236, 232) if row.selected else LIST_BG)
        _avatar(draw, profile.field_box(index, "avatar"), row.avatar)
        for field_name, text in (("name", row.name), ("preview", row.preview), ("time", row.time)):
            _draw_text(draw, profile.field_box(index, field_name), text)
        if row.badge:
            _badge(draw, profile.field_box(index, "badge"), row.badge)
    if scene.overall and profile.overall_unread_badge:
        _badge(draw, profile.overall_unread_badge, scene.overall)
    if profile.conversation_title and scene.title:
        _draw_text(draw, profile.conversation_title, scene.title)
    if profile.message_pane is not None:
        pl, pt, pr, pb = profile.message_pane
        y = pb - 6
        for bubble in reversed(scene.bubbles):
            width = 90 + 6 * min(len(bubble.text), 20)
            top = y - 34
            if top < pt + 2:
                break
            if bubble.direction == "outgoing":
                box = (pr - 60 - width, top, pr - 60, y)
                color = profile.colors.outgoing_bubble
            else:
                box = (pl + 60, top, pl + 60 + width, y)
                color = profile.colors.incoming_bubble
            draw.rectangle(box, fill=tuple(color))
            draw.text((box[0] + 8, box[1] + 10), placeholder(bubble.text), fill=INK, font=FONT)
            y = top - 14
    if profile.composer is not None and scene.composer:
        _draw_text(draw, profile.composer, scene.composer)
    if scene.cover_header or scene.challenge:
        draw.rectangle((40, 200, 700, 470), fill=(255, 255, 255), outline=INK)
        if scene.challenge:
            draw.text((80, 300), placeholder(scene.challenge), fill=INK, font=FONT)
    return image


def register(scene: Scene, frame: Image.Image, analyzer: FrameAnalyzer, fake: FakeOcrEngine) -> None:
    profile = analyzer.profile
    if frame.size != (profile.frame_width, profile.frame_height):
        return
    for index, row in enumerate(scene.rows[:profile.row.count]):
        fake.register(analyzer.field_crop(frame, index, "name"), row.name, row.name_conf)
        fake.register(analyzer.field_crop(frame, index, "preview"), row.preview, row.preview_conf)
        fake.register(analyzer.field_crop(frame, index, "time"), row.time, row.time_conf)
        if row.badge:
            badge = analyzer.badge_ocr_image(analyzer.field_crop(frame, index, "badge"))
            fake.register(badge, str(row.badge), 0.95)
    if scene.overall and profile.overall_unread_badge:
        from xianyu_monitor.vision import imageops

        crop = imageops.crop(frame, profile.overall_unread_badge)
        fake.register(analyzer.badge_ocr_image(crop), str(scene.overall), 0.95)
    title = analyzer.title_crop(frame)
    if title is not None and scene.title:
        fake.register(title, scene.title, 0.97)
    composer = analyzer.composer_crop(frame)
    if composer is not None and scene.composer:
        fake.register(composer, scene.composer, 0.95)
    if profile.message_pane is not None:
        boxes = find_bubbles(frame, profile.message_pane, profile.colors, profile.min_bubble_width,
                             profile.min_bubble_height)
        visible = scene.bubbles[-len(boxes):] if boxes else []
        for bubble, box in zip(visible, boxes, strict=False):
            fake.register(analyzer.bubble_text_crop(frame, box.box), bubble.text, 0.96)
    if scene.challenge:
        probe = analyzer.probe_crop(frame)
        if probe is not None:
            fake.register(probe, scene.challenge, 0.9)
            # Incremental safety OCR reads changed bands; the prompt line is drawn at y=300.
            line_top = 300 - profile.challenge_probe[1]
            fake.register_lines(probe, [(line_top - 2, line_top + 14, scene.challenge)])


class Harness:
    """Profile + fake OCR + analyzer + renderer bundled for tests."""

    def __init__(self, profile_overrides: dict | None = None, with_anchors: bool = True):
        data = base_profile_dict()
        data.update(profile_overrides or {})
        self.fake = FakeOcrEngine()
        self.pool = OcrPool("fake", engine_factory=lambda: self.fake, use_processes=False)
        profile = Profile.model_validate(data)
        if with_anchors:
            from xianyu_monitor.vision import imageops

            reference = draw_scene(Scene(), profile)
            for anchor in data["anchors"]:
                region = imageops.crop(reference, tuple(anchor["region"]))
                anchor["dhash"] = imageops.dhash(region)
                anchor["mean"] = imageops.mean_gray(region)
            profile = Profile.model_validate(data)
        self.profile = profile
        self.profile_dict = profile.model_dump(mode="json")
        self.analyzer = FrameAnalyzer(profile, self.pool)

    def render(self, scene: Scene) -> Image.Image:
        frame = draw_scene(scene, self.profile)
        register(scene, frame, self.analyzer, self.fake)
        return frame


class FakeDesktop:
    """A tiny model of the chat page that reacts to clicks, typing and Enter."""

    def __init__(self, harness: Harness, rows: list[SRow], history: dict[str, list[SBubble]]):
        self.h = harness
        self.rows = rows
        self.history = history
        self.selected: int | None = next((i for i, r in enumerate(rows) if r.selected), None)
        self.composer = ""
        self.focused = False
        self.misroute = 0  # clicking row i opens row i+misroute
        self.drop_send = False
        self.refresh_stuck = False
        self.actions: list[tuple] = []

    def scene(self) -> Scene:
        title, bubbles = "", []
        if self.selected is not None:
            title = self.rows[self.selected].name
            bubbles = list(self.history.get(title, []))
        for i, row in enumerate(self.rows):
            row.selected = i == self.selected
        return Scene(rows=self.rows, title=title, bubbles=bubbles, composer=self.composer)

    def capture(self) -> Image.Image:
        return self.h.render(self.scene())

    async def capture_async(self) -> Image.Image:
        return self.capture()

    # InputAdapter protocol
    async def click(self, x: int, y: int) -> None:
        self.actions.append(("click", x, y))
        p = self.h.profile
        for i in range(min(len(self.rows), p.row.count)):
            left, top, right, bottom = p.row_box(i)
            if left <= x < right and top <= y < bottom:
                target = min(len(self.rows) - 1, i + self.misroute)
                self.selected = target
                self.rows[target].badge = None
                self.focused = False
                return
        if p.composer and p.composer[0] <= x < p.composer[2] and p.composer[1] <= y < p.composer[3]:
            self.focused = True

    async def type_text(self, text: str) -> None:
        self.actions.append(("type", text))
        if self.focused:
            self.composer += text

    async def press(self, key: str) -> None:
        self.actions.append(("press", key))
        if key == "F5" and not self.refresh_stuck:
            self.selected = None
            self.focused = False
        if key == "Return" and self.focused and self.composer and self.selected is not None and not self.drop_send:
            name = self.rows[self.selected].name
            self.history.setdefault(name, []).append(SBubble("outgoing", self.composer))
            self.rows[self.selected].preview = self.composer
            self.composer = ""
