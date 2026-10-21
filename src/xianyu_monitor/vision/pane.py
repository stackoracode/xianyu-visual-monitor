"""Conversation-pane bubble segmentation by color and alignment.

Observed reference layout: incoming bubbles are light gray and left-aligned, outgoing
bubbles are yellow and right-aligned. Both color and alignment must agree, otherwise the
bubble direction is ``unknown``. Colors are calibration values, not constants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from PIL import Image

from xianyu_monitor.detection.textnorm import normalize_name
from xianyu_monitor.vision.profile import Box, Colors


def canonical_title(text: str) -> str:
    return re.sub(r"\([^()]*\*{2,}[^()]*\)$", "", normalize_name(text))


@dataclass
class BubbleBox:
    direction: str  # incoming | outgoing | unknown
    box: Box  # absolute frame coordinates
    fill_ratio: float


def _matches(pixel: tuple[int, int, int], target: tuple[int, int, int], tol: int) -> bool:
    return abs(pixel[0] - target[0]) <= tol and abs(pixel[1] - target[1]) <= tol and abs(pixel[2] - target[2]) <= tol


def _segments(image: Image.Image, target: tuple[int, int, int], tol: int, min_w: int, min_h: int) -> list[tuple[Box, float]]:
    rgb = image.convert("RGB")
    w, h = rgb.size
    px = rgb.load()
    # Keep disconnected same-color pixels (e.g. avatar highlights) separate from
    # a short bubble. Row extrema used to stretch a 48px bubble across an avatar
    # and fail the fill test even though the message was plainly visible.
    matching: set[tuple[int, int]] = set()
    for y in range(h):
        matching.update((x, y) for x in range(w) if _matches(px[x, y], target, tol))
    boxes: list[tuple[Box, float]] = []
    while matching:
        seed = matching.pop()
        pending = [seed]
        left = right = seed[0]
        top = bottom = seed[1]
        hits = 0
        while pending:
            x, y = pending.pop()
            left, right = min(left, x), max(right, x)
            top, bottom = min(top, y), max(bottom, y)
            hits += 1
            for neighbor in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if neighbor in matching:
                    matching.remove(neighbor)
                    pending.append(neighbor)
        height = bottom - top + 1
        width = right - left + 1
        if height >= min_h and width >= min_w:
            fill = hits / float(width * height)
            if fill >= 0.45:
                boxes.append(((left, top, right + 1, bottom + 1), fill))
    return boxes


def find_bubbles(frame: Image.Image, pane: Box, colors: Colors, min_w: int = 24, min_h: int = 18) -> list[BubbleBox]:
    left, top, right, bottom = pane
    region = frame.crop(pane)
    mid = (right - left) / 2.0
    bubbles: list[BubbleBox] = []
    for direction, color, tol in (
        ("outgoing", colors.outgoing_bubble, colors.outgoing_tolerance),
        ("incoming", colors.incoming_bubble, colors.incoming_tolerance),
    ):
        for (bl, bt, br, bb), fill in _segments(region, color, tol, min_w, min_h):
            center = (bl + br) / 2.0
            aligned = center >= mid if direction == "outgoing" else center < mid
            bubbles.append(BubbleBox(
                direction=direction if aligned else "unknown",
                box=(left + bl, top + bt, left + br, top + bb),
                fill_ratio=fill,
            ))
    bubbles.sort(key=lambda b: (b.box[3], b.box[0]))
    return bubbles


def has_send_failure(frame: Image.Image, bubble: Box, pane: Box, colors: Colors) -> bool:
    """Conservatively detect a red error indicator immediately left of an outgoing bubble.

    Never inspect row unread badges or the right-hand avatar. A red icon here is
    sufficient to withhold success; real-page calibration is still required.
    """
    left, top, _, bottom = bubble
    region = frame.convert("RGB").crop((max(pane[0], left - 40), top, left, bottom))
    red = {(x, y) for y in range(region.height) for x in range(region.width)
           if (p := region.getpixel((x, y)))[0] >= colors.badge_red_min
           and p[1] <= colors.badge_green_max and p[2] <= colors.badge_blue_max}
    while red:
        pending = [red.pop()]
        component = []
        while pending:
            x, y = pending.pop()
            component.append((x, y))
            for point in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if point in red:
                    red.remove(point)
                    pending.append(point)
        width = max(x for x, _ in component) - min(x for x, _ in component) + 1
        height = max(y for _, y in component) - min(y for _, y in component) + 1
        if len(component) >= 8 and 2 <= width <= 32 and 6 <= height <= 32:
            return True
    return False
