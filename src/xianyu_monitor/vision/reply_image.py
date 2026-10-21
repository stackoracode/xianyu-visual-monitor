"""Private local image validation and screenshot-only outgoing thumbnail matching."""
from __future__ import annotations

import hashlib
import io
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from xianyu_monitor.vision import imageops


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


MIN_WIDTH, MAX_WIDTH = 80, 360
COARSE_STEP = 2
# A width survives the half-resolution screen when its coarse score is within this margin of
# the acceptance threshold. Downsampling both images averages away sub-pixel misalignment, so
# a full-resolution score above the threshold does not fall this far at half resolution
# (tested against the exhaustive search in tests/test_image_matcher.py).
COARSE_MARGIN = 0.2
# Widths far below the best coarse score cannot hold the best full-resolution match.
RELATIVE_MARGIN = 0.1
_THUMBS: OrderedDict[tuple, object] = OrderedDict()
_THUMB_CACHE_SIZE = 96


def _reference_key(reference: Image.Image) -> str:
    key = getattr(reference, "_xvm_digest", None)
    if key is None:
        key = imageops.digest(reference)
        try:
            reference._xvm_digest = key  # type: ignore[attr-defined]
        except AttributeError:  # pragma: no cover
            pass
    return key


def _cached(key: tuple, build):
    value = _THUMBS.get(key)
    if value is None:
        value = build()
        _THUMBS[key] = value
        while len(_THUMBS) > _THUMB_CACHE_SIZE:
            _THUMBS.popitem(last=False)
    else:
        _THUMBS.move_to_end(key)
    return value


def _template_geometry(width: int, ratio: float, pane_height: int) -> tuple[int, int, int] | None:
    """(height, crop_offset, visible_height) exactly as the exhaustive search derives them."""
    height = max(12, round(width * ratio))
    if height > pane_height - 2:
        visible_height = int(pane_height * .75)
        if visible_height < height * .6:
            return None
        return height, (height - visible_height) // 2, visible_height
    return height, 0, height


def _score_width(np, cv2, search, gray, reference, width, ratio, pane, threshold, thumb_of):
    """The exhaustive search's per-width step, unchanged: LANCZOS thumbnail, normalized
    correlation on gray, right-half candidate mask, mean color difference check."""
    left, top, right, bottom = pane
    geometry = _template_geometry(width, ratio, bottom - top)
    if geometry is None:
        return None
    height, offset, visible = geometry
    thumb = thumb_of(width, height)
    if offset or visible != height:
        thumb = thumb[offset:offset + visible]
        height = visible
    template = cv2.cvtColor(thumb, cv2.COLOR_RGB2GRAY)
    if float(template.std()) < 8:
        return None  # flat colors cannot identify a particular image
    scores = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
    # Images wider than half the pane can still be right aligned. Mask candidate
    # centers instead of cropping away their left side; reject incoming thumbnails.
    minimum_x = max(0, int((right - left) / 2 - width / 2))
    scores[:, :minimum_x] = -1
    _, score, _, pos = cv2.minMaxLoc(scores)
    if score < threshold:
        return None
    x, y = pos
    actual = search[y:y + height, x:x + width].astype(float)
    # Also require color agreement: correlation alone accepts wrong, dark thumbnails.
    if float(np.abs(actual - thumb.astype(float)).mean()) > 20:
        return None
    return ImageMatch((left + x, top + y, left + x + width, top + y + height), float(score))


def find_outgoing_image_exhaustive(frame: Image.Image, reference: Image.Image, pane,
                                   threshold=0.92) -> ImageMatch | None:
    """Reference implementation: every width from 80 to 360 px at full resolution."""
    import cv2
    import numpy as np

    left, top, right, bottom = pane
    search = np.asarray(_rgb(frame.crop((left, top, right, bottom))))
    gray = cv2.cvtColor(search, cv2.COLOR_RGB2GRAY)
    ratio = reference.height / reference.width

    def thumb_of(width, height):
        return np.asarray(reference.resize((width, height), Image.Resampling.LANCZOS))

    best = None
    for width in range(MIN_WIDTH, min(MAX_WIDTH, right - left - 2) + 1):
        match = _score_width(np, cv2, search, gray, reference, width, ratio, pane, threshold, thumb_of)
        if match is not None and (best is None or match.score > best.score):
            best = match
    return best


def _coarse_widths(np, cv2, gray, reference, ref_key, widths, ratio, pane, threshold) -> list[int]:
    """Screen all widths on 2x-downsampled images; return widths that still need the exact
    full-resolution check (neighbors of every promising coarse width)."""
    left, top, right, bottom = pane
    half = cv2.resize(gray, (max(1, gray.shape[1] // 2), max(1, gray.shape[0] // 2)), interpolation=cv2.INTER_AREA)
    work_width = min(reference.width, 2 * MAX_WIDTH)
    small = _cached((ref_key, "coarse_gray", work_width), lambda: np.asarray(
        reference.convert("L").resize((work_width, max(1, round(work_width * ratio))), Image.Resampling.LANCZOS)))
    coarse: dict[int, float | None] = {}
    for width in widths[::COARSE_STEP]:
        geometry = _template_geometry(width, ratio, bottom - top)
        if geometry is None:
            continue
        height, offset, visible = geometry
        tw, th = max(1, round(width / 2)), max(1, round(height / 2))
        template = cv2.resize(small, (tw, th), interpolation=cv2.INTER_AREA)
        if visible != height:
            template = template[offset // 2:offset // 2 + max(1, visible // 2)]
        score = None  # too small/flat to screen at half resolution: the exact check decides
        if (template.shape[0] <= half.shape[0] and template.shape[1] <= half.shape[1]
                and float(template.std()) >= 2.0):
            scores = cv2.matchTemplate(half, template, cv2.TM_CCOEFF_NORMED)
            minimum_x = max(0, int((right - left) / 2 - width / 2) // 2 - 1)
            scores[:, :minimum_x] = -1
            _, value, _, _ = cv2.minMaxLoc(scores)
            score = float(value) if np.isfinite(value) else None
        coarse[width] = score
    screened = [v for v in coarse.values() if v is not None]
    floor = threshold - COARSE_MARGIN
    if screened:
        floor = max(floor, max(screened) - RELATIVE_MARGIN)
    keep: set[int] = set()
    lowest, highest = widths[0], widths[-1]
    for width, score in coarse.items():
        if score is None or score >= floor:
            keep.update(range(max(lowest, width - COARSE_STEP), min(highest, width + COARSE_STEP) + 1))
    return sorted(keep)


def find_outgoing_image(frame: Image.Image, reference: Image.Image, pane, threshold=0.92) -> ImageMatch | None:
    """Fast equivalent of :func:`find_outgoing_image_exhaustive`.

    A half-resolution screen discards widths that cannot reach the threshold or are far
    below the best coarse score; surviving widths get exactly the exhaustive per-width check
    (same LANCZOS thumbnails, scores, masks and color test, in the same order). Acceptance is
    therefore never looser than the exhaustive search. Equality of the selected box/score is
    checked by parity tests and by ``xianyu-monitor benchmark --reference`` on real frames.
    Thumbnails are cached per reference image across the repeated verification calls.
    """
    # Imported only by the desktop worker. Minimal web installs do not need the OCR stack.
    import cv2
    import numpy as np

    left, top, right, bottom = pane
    search = np.asarray(_rgb(frame.crop((left, top, right, bottom))))
    gray = cv2.cvtColor(search, cv2.COLOR_RGB2GRAY)
    ratio = reference.height / reference.width
    ref_key = _reference_key(reference)
    widths = list(range(MIN_WIDTH, min(MAX_WIDTH, right - left - 2) + 1))
    if not widths:
        return None

    def thumb_of(width, height):
        return _cached((ref_key, "thumb", width, height), lambda: np.asarray(
            reference.resize((width, height), Image.Resampling.LANCZOS)))

    best = None
    for width in _coarse_widths(np, cv2, gray, reference, ref_key, widths, ratio, pane, threshold):
        match = _score_width(np, cv2, search, gray, reference, width, ratio, pane, threshold, thumb_of)
        if match is not None and (best is None or match.score > best.score):
            best = match
    return best


def _rgb(image: Image.Image) -> Image.Image:
    return image if image.mode == "RGB" else image.convert("RGB")
