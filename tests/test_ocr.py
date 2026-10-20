"""Recognizer/detector agreement protects low-confidence field OCR."""

import sys
from types import SimpleNamespace

import pytest
from PIL import Image

from xianyu_monitor.ocr.base import RapidOcrEngine


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
