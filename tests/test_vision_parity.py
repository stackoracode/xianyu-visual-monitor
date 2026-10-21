"""Vectorized vision paths must return exactly what the reference implementations return."""

import random

import pytest
from PIL import Image, ImageDraw, ImageFilter

from xianyu_monitor.vision import imageops, pane
from xianyu_monitor.vision.profile import Colors

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")

COLORS = Colors()


def scene(seed: int) -> Image.Image:
    """Random bubbles, avatar-like specks, red icons and noise on a pane background."""
    rng = random.Random(seed)
    image = Image.new("RGB", (420, 260), (248, 252, 248))
    draw = ImageDraw.Draw(image)
    for _ in range(rng.randrange(3, 9)):
        color = rng.choice([COLORS.incoming_bubble, COLORS.outgoing_bubble, (200, 40, 40), (90, 90, 90)])
        x, y = rng.randrange(0, 380), rng.randrange(0, 230)
        w, h = rng.randrange(3, 160), rng.randrange(3, 60)
        if rng.random() < 0.3:
            draw.ellipse((x, y, x + w, y + h), fill=color)
        else:
            draw.rectangle((x, y, x + w, y + h), fill=color)
        if rng.random() < 0.5:
            draw.text((x + 3, y + 3), "ab", fill=(20, 20, 20))
    pixels = image.load()
    for _ in range(400):  # isolated same-color pixels must stay separate components
        pixels[rng.randrange(420), rng.randrange(260)] = rng.choice(
            [COLORS.incoming_bubble, COLORS.outgoing_bubble, (250, 30, 30)])
    return image


def reference(fn, *args, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(pane, "_CV", None)
        return fn(*args)


@pytest.mark.parametrize("seed", range(12))
def test_bubble_segmentation_matches_reference(seed, monkeypatch):
    image = scene(seed)
    for color, tol in ((COLORS.incoming_bubble, COLORS.incoming_tolerance),
                       (COLORS.outgoing_bubble, COLORS.outgoing_tolerance)):
        fast = pane._segments(image, color, tol, 24, 18)
        slow = reference(pane._segments, image, color, tol, 24, 18, monkeypatch=monkeypatch)
        assert sorted(fast) == sorted(slow)
    box = (0, 0, image.width, image.height)
    fast_bubbles = pane.find_bubbles(image, box, COLORS)
    slow_bubbles = reference(pane.find_bubbles, image, box, COLORS, monkeypatch=monkeypatch)
    assert fast_bubbles == slow_bubbles


@pytest.mark.parametrize("seed", range(12))
def test_send_failure_detection_matches_reference(seed, monkeypatch):
    image = scene(seed)
    rng = random.Random(seed)
    for _ in range(10):
        left = rng.randrange(0, 380)
        top = rng.randrange(0, 200)
        bubble = (left, top, left + 30, top + rng.randrange(5, 60))
        box = (0, 0, image.width, image.height)
        fast = pane.has_send_failure(image, bubble, box, COLORS)
        assert fast == reference(pane.has_send_failure, image, bubble, box, COLORS, monkeypatch=monkeypatch)


def test_send_failure_icon_is_found():
    image = Image.new("RGB", (200, 60), (248, 252, 248))
    ImageDraw.Draw(image).ellipse((100, 20, 114, 34), fill=(245, 60, 60))
    assert pane.has_send_failure(image, (120, 10, 190, 50), (0, 0, 200, 60), COLORS)
    assert not pane.has_send_failure(image, (40, 10, 90, 50), (0, 0, 200, 60), COLORS)
    assert not pane.has_send_failure(image, (0, 10, 50, 50), (0, 0, 200, 60), COLORS)  # empty strip


@pytest.mark.parametrize("seed", range(8))
def test_bubble_line_split_matches_reference(seed, monkeypatch):
    rng = random.Random(seed)
    image = Image.new("RGB", (rng.randrange(60, 300), rng.randrange(20, 90)), (240, 240, 240))
    draw = ImageDraw.Draw(image)
    for line in range(rng.randrange(1, 4)):
        draw.text((rng.randrange(0, 20), 4 + line * 18), "text 文字" * rng.randrange(1, 3), fill=(20, 20, 20))
    fast = [imageops.digest(c) for c in imageops.bubble_text_lines(image)]
    import builtins

    real_import = builtins.__import__

    def no_numpy(name, *args, **kwargs):
        if name == "numpy":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_numpy)
    slow = [imageops.digest(c) for c in imageops.bubble_text_lines(image)]
    monkeypatch.setattr(builtins, "__import__", real_import)
    assert fast == slow


def photo(seed: int, size=(240, 180), grain: int = 6, detail_weight: float = 0.35) -> Image.Image:
    """A smooth random 'photo' with some detail so normalized correlation is meaningful."""
    rng = np.random.default_rng(seed)
    base = Image.fromarray(rng.integers(0, 256, (6, 8, 3), dtype=np.uint8)).resize(size, Image.Resampling.BICUBIC)
    detail = Image.fromarray(rng.integers(0, 256, (size[1] // grain, size[0] // grain, 3), dtype=np.uint8)).resize(
        size, Image.Resampling.BILINEAR)
    image = Image.blend(base, detail, detail_weight).filter(ImageFilter.SMOOTH)
    draw = ImageDraw.Draw(image)
    draw.rectangle((size[0] // 5, size[1] // 4, size[0] // 3, size[1] // 2), outline=(255, 255, 255), width=3)
    return image


def chat(seed: int, reference: Image.Image, width: int | None, resample, extra: Image.Image | None = None,
         pane_size=(650, 272)):
    rng = random.Random(seed)
    frame = Image.new("RGB", (pane_size[0] + 40, pane_size[1] + 40), (248, 252, 248))
    left, top = 20, 20
    if extra is not None:  # an unrelated incoming image on the left
        thumb = extra.resize((150, round(150 * extra.height / extra.width)), resample)
        frame.paste(thumb.crop((0, 0, thumb.width, min(thumb.height, pane_size[1] - 10))), (left + 10, top + 5))
    if width is not None:
        height = round(width * reference.height / reference.width)
        thumb = reference.resize((width, height), resample)
        x = left + pane_size[0] - width - rng.randrange(5, 40)
        # A thumbnail taller than the pane is matched by its centered 75% band, which must be visible.
        y = top + rng.randrange(0, max(1, pane_size[1] - height)) if height < pane_size[1] else top - 60
        frame.paste(thumb, (x, y))
    return frame, (left, top, left + pane_size[0], top + pane_size[1])


CASES = [
    # (reference seed, displayed width, browser resampler, distractor)
    (1, 120, Image.Resampling.LANCZOS, None),
    (2, 201, Image.Resampling.BILINEAR, 9),
    (3, 333, Image.Resampling.BICUBIC, None),
    (4, 90, Image.Resampling.BOX, 10),
    (5, 260, Image.Resampling.LANCZOS, 11),
    (6, 175, Image.Resampling.HAMMING, None),
    (7, None, Image.Resampling.LANCZOS, 12),   # not sent: both must find nothing
    (8, 300, Image.Resampling.BILINEAR, None),  # taller than the pane: partially visible
]


@pytest.mark.parametrize("seed,width,resample,distractor", CASES)
def test_fast_image_matcher_equals_exhaustive_search(seed, width, resample, distractor):
    from xianyu_monitor.vision import reply_image

    size = (240, 320) if seed == 8 else (240, 180)
    ref = photo(seed, size)
    extra = photo(distractor) if distractor is not None else None
    frame, box = chat(seed, ref, width, resample, extra)
    slow = reply_image.find_outgoing_image_exhaustive(frame, ref, box, 0.92)
    fast = reply_image.find_outgoing_image(frame, ref, box, 0.92)
    again = reply_image.find_outgoing_image(frame, ref, box, 0.92)  # cached thumbnails
    if width is None:
        assert slow is None and fast is None
        return
    assert slow is not None, "fixture should be matchable by the reference search"
    assert fast is not None and again is not None
    assert fast.box == slow.box == again.box
    assert fast.score == pytest.approx(slow.score, abs=1e-9)


def test_fast_matcher_evaluates_far_fewer_widths(monkeypatch):
    from xianyu_monitor.vision import reply_image

    ref = photo(1, grain=3, detail_weight=0.6)  # product-photo-like detail gives a sharp scale peak
    frame, box = chat(1, ref, 150, Image.Resampling.LANCZOS)
    calls = []
    real = reply_image._score_width

    def counting(*args):
        calls.append(args[5])
        return real(*args)

    monkeypatch.setattr(reply_image, "_score_width", counting)
    assert reply_image.find_outgoing_image(frame, ref, box, 0.92) is not None
    assert len(calls) < 100  # the exhaustive search evaluates all 281 widths


@pytest.mark.parametrize("seed,width", [(21, 96), (22, 158), (23, 211), (24, 287), (25, 349)])
def test_fast_matcher_equals_exhaustive_on_detailed_photos(seed, width):
    from xianyu_monitor.vision import reply_image

    ref = photo(seed, grain=3, detail_weight=0.6)
    frame, box = chat(seed, ref, width, Image.Resampling.BILINEAR, photo(seed + 100, grain=3, detail_weight=0.6))
    slow = reply_image.find_outgoing_image_exhaustive(frame, ref, box, 0.92)
    fast = reply_image.find_outgoing_image(frame, ref, box, 0.92)
    assert slow is not None and fast is not None
    assert fast.box == slow.box and fast.score == pytest.approx(slow.score, abs=1e-9)
