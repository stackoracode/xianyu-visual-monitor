"""Conversation-pane bubble segmentation by color and alignment.

Observed reference layout: incoming bubbles are light gray and left-aligned, outgoing
bubbles are yellow and right-aligned. Both color and alignment must agree, otherwise the
bubble direction is ``unknown``. Colors are calibration values, not constants.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

from xianyu_monitor.vision.profile import Box, Colors


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
    row_ranges: list[tuple[int, int, int] | None] = []
    for y in range(h):
        xs = [x for x in range(w) if _matches(px[x, y], target, tol)]
        if len(xs) >= min_w:
            row_ranges.append((min(xs), max(xs), len(xs)))
        else:
            row_ranges.append(None)
    boxes: list[tuple[Box, float]] = []
    y = 0
    while y < h:
        if row_ranges[y] is None:
            y += 1
            continue
        start = y
        left, right, hits = w, 0, 0
        while y < h and row_ranges[y] is not None:
            lo, hi, n = row_ranges[y]  # type: ignore[misc]
            left, right, hits = min(left, lo), max(right, hi), hits + n
            y += 1
        height = y - start
        width = right - left + 1
        if height >= min_h and width >= min_w:
            fill = hits / float(width * height)
            if fill >= 0.45:
                boxes.append(((left, start, right + 1, y), fill))
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
