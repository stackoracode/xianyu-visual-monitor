"""OCR engine interface and implementations.

Engines receive already-cropped single-line images. Heavy engines are created lazily inside
the OCR worker process (see ``pool.py``) so the web process never imports them.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Protocol

from PIL import Image

from xianyu_monitor.vision import imageops

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OcrResult:
    text: str
    confidence: float

    @property
    def empty(self) -> bool:
        return not self.text.strip()


EMPTY = OcrResult("", 0.0)


class OcrEngine(Protocol):
    name: str

    def recognize_line(self, image: Image.Image) -> OcrResult: ...

    def recognize_block(self, image: Image.Image) -> OcrResult: ...


class OcrUnavailable(RuntimeError):
    pass


class RapidOcrEngine:
    """RapidOCR (PaddleOCR PP-OCR models exported to ONNX, CPU onnxruntime)."""

    name = "rapidocr"

    def __init__(self, scale: int = 2):
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - depends on optional install
            raise OcrUnavailable("rapidocr_onnxruntime is not installed (pip install -r requirements/ocr.txt)") from exc
        self._engine = RapidOCR()
        self._scale = scale

    def recognize_line(self, image: Image.Image) -> OcrResult:  # pragma: no cover - real OCR
        import numpy as np  # type: ignore[import-not-found]

        prepared = imageops.prepare_for_ocr(image, self._scale).convert("RGB")
        array = np.asarray(prepared)
        # Recognition-only on a pre-cropped single line is faster and avoids detector misses
        # on tiny crops. Fall back to the full pipeline if the recognizer returns nothing.
        result, _ = self._engine(array, use_det=False, use_cls=False, use_rec=True)
        text, score = _parse_rapid(result)
        if not text:
            result, _ = self._engine(array)
            text, score = _parse_rapid(result)
        return OcrResult(text.strip(), float(score))

    def recognize_block(self, image: Image.Image) -> OcrResult:  # pragma: no cover - real OCR
        import numpy as np  # type: ignore[import-not-found]

        result, _ = self._engine(np.asarray(image.convert("RGB")))
        if not result:
            return OcrResult("", 1.0)
        texts = [str(item[1]) for item in result]
        scores = [float(item[2]) for item in result]
        return OcrResult("\n".join(texts), min(scores))


def _parse_rapid(result: object) -> tuple[str, float]:
    if not result:
        return "", 0.0
    texts: list[str] = []
    scores: list[float] = []
    for item in result:  # type: ignore[union-attr]
        if isinstance(item, (list, tuple)):
            if len(item) == 3:  # [box, text, score]
                texts.append(str(item[1]))
                scores.append(float(item[2]))
            elif len(item) == 2:  # [text, score]
                texts.append(str(item[0]))
                scores.append(float(item[1]))
    if not texts:
        return "", 0.0
    return "".join(texts), min(scores)


class TesseractEngine:
    """Tesseract CLI (``apt install tesseract-ocr tesseract-ocr-chi-sim``)."""

    name = "tesseract"

    def __init__(self, lang: str = "chi_sim+eng", scale: int = 3, binary: str = "tesseract"):
        path = shutil.which(binary)
        if path is None:
            raise OcrUnavailable("tesseract binary not found")
        self._binary = path
        self._lang = lang
        self._scale = scale

    def recognize_line(self, image: Image.Image) -> OcrResult:  # pragma: no cover - real OCR
        return self._run(imageops.prepare_for_ocr(image, self._scale), "7")

    def recognize_block(self, image: Image.Image) -> OcrResult:  # pragma: no cover - real OCR
        result = self._run(imageops.prepare_for_ocr(image, 1), "11")
        return result if not result.empty else OcrResult("", 1.0)

    def _run(self, prepared: Image.Image, psm: str) -> OcrResult:  # pragma: no cover - real OCR
        proc = subprocess.run(
            [self._binary, "stdin", "stdout", "-l", self._lang, "--psm", psm, "tsv"],
            input=imageops.to_png_bytes(prepared), capture_output=True, timeout=30, check=False,
        )
        if proc.returncode != 0:
            log.warning("tesseract failed: %s", proc.stderr.decode("utf-8", "replace")[:200])
            return EMPTY
        return parse_tesseract_tsv(proc.stdout.decode("utf-8", "replace"))


def parse_tesseract_tsv(tsv: str) -> OcrResult:
    words: list[str] = []
    confs: list[float] = []
    lines = tsv.splitlines()
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) < 12:
            continue
        text = parts[11].strip()
        try:
            conf = float(parts[10])
        except ValueError:
            continue
        if text and conf >= 0:
            words.append(text)
            confs.append(conf / 100.0)
    if not words:
        return EMPTY
    joined = "".join(words) if any("一" <= ch <= "鿿" for ch in "".join(words)) else " ".join(words)
    return OcrResult(joined, min(confs))


class FakeOcrEngine:
    """Content-addressed fake for tests: returns registered text for an exact crop."""

    name = "fake"

    def __init__(self) -> None:
        self._table: dict[str, OcrResult] = {}
        self.calls = 0

    def register(self, image: Image.Image, text: str, confidence: float = 0.99) -> None:
        self._table[imageops.digest(image.convert("RGB"))] = OcrResult(text, confidence)

    def recognize_line(self, image: Image.Image) -> OcrResult:
        self.calls += 1
        return self._table.get(imageops.digest(image.convert("RGB")), EMPTY)

    def recognize_block(self, image: Image.Image) -> OcrResult:
        # Unregistered block regions contain no text (confidence 1.0 means "confidently empty").
        self.calls += 1
        return self._table.get(imageops.digest(image.convert("RGB")), OcrResult("", 1.0))


def create_engine(backend: str, tesseract_lang: str = "chi_sim+eng") -> OcrEngine:
    if backend == "rapidocr":
        return RapidOcrEngine()
    if backend == "tesseract":
        return TesseractEngine(lang=tesseract_lang)
    if backend == "fake":
        return FakeOcrEngine()
    raise OcrUnavailable(f"unknown OCR backend {backend!r}")
