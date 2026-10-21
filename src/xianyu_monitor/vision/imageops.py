"""Small, dependency-light image operations (Pillow only)."""

from __future__ import annotations

import hashlib
import io

from PIL import Image, ImageOps, ImageStat

from xianyu_monitor.vision.profile import Box


def crop(image: Image.Image, box: Box) -> Image.Image:
    left, top, right, bottom = box
    width, height = image.size
    box = (max(0, left), max(0, top), min(width, right), min(height, bottom))
    return image.crop(box)


def digest(image: Image.Image) -> str:
    """Exact content digest (used for OCR caching and change detection)."""
    h = hashlib.blake2b(digest_size=16)
    h.update(f"{image.mode}{image.size}".encode())
    h.update(image.tobytes())
    return h.hexdigest()


def _ink_extents(gray: Image.Image, threshold: int) -> list[tuple[int, int] | None]:
    """Per scanline, the (min_x, max_x) of pixels darker than ``threshold``, else None."""
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - minimal web-only installs
        px = gray.load()
        out: list[tuple[int, int] | None] = []
        for y in range(gray.height):
            xs = [x for x in range(gray.width) if px[x, y] < threshold]
            out.append((xs[0], xs[-1]) if xs else None)
        return out
    ink = np.asarray(gray) < threshold
    present = ink.any(axis=1)
    first = ink.argmax(axis=1)
    last = gray.width - 1 - ink[:, ::-1].argmax(axis=1)
    return [(int(first[y]), int(last[y])) if present[y] else None for y in range(gray.height)]


def bubble_text_lines(image: Image.Image) -> list[Image.Image]:
    """Locate dark text rows on a light bubble; return no crops when ambiguous."""
    gray = image.convert("L")
    histogram = gray.histogram()
    background = max(range(256), key=histogram.__getitem__)
    if background < 170:
        return []
    extents = _ink_extents(gray, background - 40)
    rows = [y for y, extent in enumerate(extents) if extent is not None]
    groups: list[list[int]] = []
    for y in rows:
        if not groups or y - groups[-1][-1] > 3:
            groups.append([])
        groups[-1].append(y)
    if not groups or any(g[-1] - g[0] < 5 for g in groups):
        return []  # punctuation-only/fragmented rows need ordinary block OCR
    out = []
    for group in groups:
        spans = [extents[y] for y in group]
        min_x = min(span[0] for span in spans if span is not None)
        max_x = max(span[1] for span in spans if span is not None)
        out.append(image.crop((max(0, min_x - 3), max(0, group[0] - 3),
                               min(gray.width, max_x + 4), min(gray.height, group[-1] + 4))))
    return out


def dhash(image: Image.Image, size: int = 8) -> str:
    """Difference hash; robust to tiny rendering noise. Returns 16 hex chars for size=8."""
    gray = image.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    pixels = list(gray.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            left = pixels[row * (size + 1) + col]
            right = pixels[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:0{size * size // 4}x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def thumbnail_signature(image: Image.Image, width: int = 64) -> bytes:
    gray = image.convert("L")
    w, h = gray.size
    if w == 0 or h == 0:
        return b""
    height = max(1, round(h * width / w))
    return gray.resize((width, height), Image.Resampling.BILINEAR).tobytes()


def mean_abs_diff(a: bytes, b: bytes) -> float:
    if not a or not b or len(a) != len(b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / len(a)


def stddev(image: Image.Image) -> float:
    stat = ImageStat.Stat(image.convert("L"))
    return float(stat.stddev[0])


def mean_gray(image: Image.Image) -> float:
    return float(ImageStat.Stat(image.convert("L")).mean[0])


def mean_rgb(image: Image.Image) -> tuple[float, float, float]:
    stat = ImageStat.Stat(image.convert("RGB"))
    r, g, b = stat.mean[:3]
    return (r, g, b)


def color_close(color: tuple[float, float, float], target: tuple[int, int, int], tolerance: int) -> bool:
    return all(abs(c - t) <= tolerance for c, t in zip(color, target, strict=True))


def red_ratio(image: Image.Image, r_min: int, g_max: int, b_max: int) -> float:
    rgb = image.convert("RGB")
    data = rgb.getdata()
    total = len(data)
    if total == 0:
        return 0.0
    hits = sum(1 for r, g, b in data if r >= r_min and g <= g_max and b <= b_max)
    return hits / total


def badge_glyph_image(image: Image.Image, r_min: int, g_max: int, b_max: int) -> Image.Image:
    """Turn a red badge with a white numeral into dark-on-white text for OCR."""
    rgb = image.convert("RGB")
    out = Image.new("L", rgb.size, 255)
    src = rgb.load()
    dst = out.load()
    w, h = rgb.size
    for y in range(h):
        reds = [x for x in range(w) if _is_red(src[x, y], r_min, g_max, b_max)]
        if len(reds) < 2:
            continue
        # Only light pixels enclosed by badge red on this scanline are numeral strokes; the
        # light page background around the round badge must not be treated as ink.
        for x in range(reds[0] + 1, reds[-1]):
            r, g, b = src[x, y]
            if not _is_red(src[x, y], r_min, g_max, b_max) and g >= 150 and b >= 150:
                dst[x, y] = 0
    return out


def _is_red(pixel: tuple[int, int, int], r_min: int, g_max: int, b_max: int) -> bool:
    return pixel[0] >= r_min and pixel[1] <= g_max and pixel[2] <= b_max


def prepare_for_ocr(image: Image.Image, scale: int = 2) -> Image.Image:
    gray = image.convert("L")
    if scale > 1:
        gray = gray.resize((gray.width * scale, gray.height * scale), Image.Resampling.LANCZOS)
    return ImageOps.autocontrast(gray)


def to_png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def from_png_bytes(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image
