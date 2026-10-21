"""Private local image validation and screenshot-only outgoing thumbnail matching."""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


@dataclass
class ImageAsset:
    path: Path
    sha256: str
    image: Image.Image


def load_asset(path: str, expected_sha256: str | None = None) -> ImageAsset:
    source = Path(path)
    if not source.is_absolute() or any(ord(c) < 32 for c in path):
        raise ValueError("image_path_must_be_absolute")
    if source.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("image_file_too_large")
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 and digest != expected_sha256:
        raise ValueError("image_changed_since_planning")
    with Image.open(io.BytesIO(data)) as image:
        if image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height > 32_000_000:
            raise ValueError("unsupported_image")
        image.load()
        rgb = image.convert("RGB")
    return ImageAsset(source.resolve(), digest, rgb)


@dataclass
class ImageMatch:
    box: tuple[int, int, int, int]
    score: float


def find_outgoing_image(frame: Image.Image, reference: Image.Image, pane, threshold=0.92) -> ImageMatch | None:
    # Imported only by the desktop worker. Minimal web installs do not need the OCR stack.
    import cv2
    import numpy as np

    left, top, right, bottom = pane
    search_left = left
    search = np.asarray(frame.convert("RGB").crop((search_left, top, right, bottom)))
    gray = cv2.cvtColor(search, cv2.COLOR_RGB2GRAY)
    ratio = reference.height / reference.width
    best = None
    maximum = min(360, right - search_left - 2)
    for width in range(80, maximum + 1):
        height = max(12, round(width * ratio))
        thumb = np.asarray(reference.resize((width, height), Image.Resampling.LANCZOS))
        if height > bottom - top - 2:
            visible_height = int((bottom - top) * .75)
            if visible_height < height * .6:
                continue
            offset = (height - visible_height) // 2
            thumb = thumb[offset:offset + visible_height]
            height = visible_height
        template = cv2.cvtColor(thumb, cv2.COLOR_RGB2GRAY)
        if float(template.std()) < 8:
            continue  # flat colors cannot identify a particular image
        scores = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
        # Images wider than half the pane can still be right aligned. Mask candidate
        # centers instead of cropping away their left side; reject incoming thumbnails.
        minimum_x = max(0, int((right - left) / 2 - width / 2))
        scores[:, :minimum_x] = -1
        _, score, _, pos = cv2.minMaxLoc(scores)
        if score < threshold or (best is not None and score <= best.score):
            continue
        x, y = pos
        actual = search[y:y + height, x:x + width].astype(float)
        # Also require color agreement: correlation alone accepts wrong, dark thumbnails.
        if float(np.abs(actual - thumb.astype(float)).mean()) > 20:
            continue
        best = ImageMatch((search_left + x, top + y, search_left + x + width, top + y + height), float(score))
    return best
