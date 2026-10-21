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


def _cv():
    """OpenCV/numpy when installed (always on the worker); ``None`` selects the pure-Python
    reference implementation, which returns identical components."""
    try:
        import cv2
        import numpy as np
    except ImportError:  # pragma: no cover - minimal web-only installs
        return None
    return cv2, np


def _components_cv(mask) -> list[tuple[int, int, int, int, int]]:
    """4-connected components of a boolean mask as (left, top, width, height, pixel_count)."""
    cv2, np = _CV
    if mask.size == 0:
        return []
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=4)
    return [(int(x), int(y), int(w), int(h), int(a)) for x, y, w, h, a in stats[1:count]]


def _components_py(points: set[tuple[int, int]]) -> list[tuple[int, int, int, int, int]]:
    out = []
    while points:
        seed = points.pop()
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
                if neighbor in points:
                    points.remove(neighbor)
                    pending.append(neighbor)
        out.append((left, top, right - left + 1, bottom - top + 1, hits))
    return out


def _color_components(image: Image.Image, target: tuple[int, int, int], tol: int) -> list[tuple[int, int, int, int, int]]:
    rgb = image.convert("RGB")
    if _CV is not None:
        np = _CV[1]
        arr = np.asarray(rgb).astype(np.int16)
        mask = (np.abs(arr - np.array(target, dtype=np.int16)) <= tol).all(axis=2)
        return _components_cv(mask)
    w, h = rgb.size
    px = rgb.load()
    return _components_py({(x, y) for y in range(h) for x in range(w) if _matches(px[x, y], target, tol)})


def _red_components(image: Image.Image, colors: Colors) -> list[tuple[int, int, int, int, int]]:
    rgb = image.convert("RGB")
    if _CV is not None:
        np = _CV[1]
        arr = np.asarray(rgb)
        mask = ((arr[:, :, 0] >= colors.badge_red_min) & (arr[:, :, 1] <= colors.badge_green_max)
                & (arr[:, :, 2] <= colors.badge_blue_max))
        return _components_cv(mask)
    return _components_py({(x, y) for y in range(rgb.height) for x in range(rgb.width)
                           if (p := rgb.getpixel((x, y)))[0] >= colors.badge_red_min
                           and p[1] <= colors.badge_green_max and p[2] <= colors.badge_blue_max})


_CV = _cv()


def _segments(image: Image.Image, target: tuple[int, int, int], tol: int, min_w: int, min_h: int) -> list[tuple[Box, float]]:
    # Keep disconnected same-color pixels (e.g. avatar highlights) separate from
    # a short bubble. Row extrema used to stretch a 48px bubble across an avatar
    # and fail the fill test even though the message was plainly visible.
    boxes: list[tuple[Box, float]] = []
    for left, top, width, height, hits in _color_components(image, target, tol):
        if height >= min_h and width >= min_w:
            fill = hits / float(width * height)
            if fill >= 0.45:
                boxes.append(((left, top, left + width, top + height), fill))
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
    bubbles.sort(key=lambda b: (b.box[3], b.box[0], b.box[1], b.box[2]))
    return bubbles


def has_send_failure(frame: Image.Image, bubble: Box, pane: Box, colors: Colors) -> bool:
    """Conservatively detect a red error indicator immediately left of an outgoing bubble.

    Never inspect row unread badges or the right-hand avatar. A red icon here is
    sufficient to withhold success; real-page calibration is still required.
    """
    left, top, _, bottom = bubble
    region = frame.crop((max(pane[0], left - 40), top, left, bottom))
    return any(hits >= 8 and 2 <= width <= 32 and 6 <= height <= 32
               for _, _, width, height, hits in _red_components(region, colors))
