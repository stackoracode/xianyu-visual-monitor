"""Recognizer/detector agreement protects low-confidence field OCR."""

import sys
from types import SimpleNamespace

import pytest
from PIL import Image

from xianyu_monitor.ocr.base import RapidOcrEngine


def test_wrapped_bubble_uses_complete_visual_rows_and_conservative_confidence():
    from PIL import ImageDraw

    from xianyu_monitor.ocr.base import OcrResult
    from xianyu_monitor.vision.imageops import bubble_text_lines

    image = Image.new("RGB", (300, 60), (248, 228, 16))
    draw = ImageDraw.Draw(image)
    draw.rectangle((8, 10, 270, 23), fill=(32, 32, 32))
    draw.rectangle((8, 34, 25, 47), fill=(32, 32, 32))
    crops = bubble_text_lines(image)
    assert len(crops) == 2 and crops[0].width > crops[1].width * 5
    def ink(im):
        return sum(px == (32, 32, 32) for px in im.getdata())

    assert sum(ink(im) for im in crops) == ink(image)
    results = iter([OcrResult("long first line", .98), OcrResult("tail.", .91)])
    engine = object.__new__(RapidOcrEngine)
    engine.recognize_line = lambda im: next(results)
    result = engine.recognize_bubble(image)
    assert result.text == "long first line\ntail." and result.confidence == .91


def test_punctuation_fragments_keep_block_ocr_fallback():
    from PIL import ImageDraw

    from xianyu_monitor.vision.imageops import bubble_text_lines

    image = Image.new("RGB", (40, 40), "white")
    ImageDraw.Draw(image).rectangle((10, 20, 12, 22), fill="black")
    assert bubble_text_lines(image) == []


@pytest.mark.parametrize("first,detected,expected,calls", [
    (("buyer", 0.65), ("buyer", 0.99), ("buyer", 0.99), 2),
    (("buyer", 0.65), ("other", 0.99), ("buyer", 0.65), 2),
    (("buyer", 0.65), ("buyer", 0.55), ("buyer", 0.65), 2),
    (("buyer", 0.95), ("other", 0.99), ("buyer", 0.95), 1),
    (("", 0.0), ("buyer", 0.99), ("buyer", 0.99), 2),
])
def test_rapid_low_confidence_requires_agreement(monkeypatch, first, detected, expected, calls):
    # Exercise control flow on every CI platform without importing optional model weights.
    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace(asarray=lambda image: image))
    invocations = []

    def engine(image, **options):
        invocations.append(options)
        text, confidence = first if options else detected
        return ([[text, confidence]] if text else None), None

    ocr = object.__new__(RapidOcrEngine)
    ocr._engine = engine
    ocr._scale = 2
    result = ocr.recognize_line(Image.new("RGB", (180, 20), "white"))
    assert (result.text, result.confidence) == expected
    assert len(invocations) == calls
