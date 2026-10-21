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

    def __init__(self, scale: int = 2, threads: int = 0):
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - depends on optional install
            raise OcrUnavailable("rapidocr_onnxruntime is not installed (pip install -r requirements/ocr.txt)") from exc
        # Thread count only changes scheduling; RapidOCR ignores values outside 1..cpu_count.
        self._engine = RapidOCR(intra_op_num_threads=threads) if threads > 0 else RapidOCR()
        self._scale = scale

    def recognize_line(self, image: Image.Image) -> OcrResult:  # pragma: no cover - real OCR
        import numpy as np  # type: ignore[import-not-found]

        prepared = imageops.prepare_for_ocr(image, self._scale).convert("RGB")
        array = np.asarray(prepared)
        # Recognition-only on a pre-cropped single line is faster and avoids detector misses
        # on tiny crops. A field with lots of blank padding may distort recognition.
        # For low confidence, cross-check with detection without cutting off any text.
        result, _ = self._engine(array, use_det=False, use_cls=False, use_rec=True)
        text, score = _parse_rapid(result)
        if not text or score < 0.8:
            result, _ = self._engine(array)
            detected_text, detected_score = _parse_rapid(result)
            # Never promote conflicting text solely because its confidence is higher.
            if not text or (detected_text.strip() == text.strip() and detected_score > score):
                text, score = detected_text, detected_score
        return OcrResult(text.strip(), float(score))

    def recognize_bubble(self, image: Image.Image) -> OcrResult:  # pragma: no cover - real OCR
        lines = imageops.bubble_text_lines(image)
        if not lines:
            return self.recognize_block(image)
        results = [self.recognize_line(line) for line in lines]
        return OcrResult("\n".join(r.text for r in results), min(r.confidence for r in results))

    def recognize_block(self, image: Image.Image) -> OcrResult:  # pragma: no cover - real OCR
        import numpy as np  # type: ignore[import-not-found]

        prepared = imageops.prepare_for_ocr(image, self._scale).convert("RGB")
        result, _ = self._engine(np.asarray(prepared))
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
        self._regions: list[tuple[int, bytes, list[tuple[int, int, str]], float]] = []
        self.calls = 0

    def register(self, image: Image.Image, text: str, confidence: float = 0.99) -> None:
        self._table[imageops.digest(image.convert("RGB"))] = OcrResult(text, confidence)

    def register_lines(self, image: Image.Image, lines: list[tuple[int, int, str]], confidence: float = 0.9) -> None:
        """Let full-width horizontal bands of ``image`` read the text lines they fully contain."""
        rgb = image.convert("RGB")
        self._regions.append((rgb.width, rgb.tobytes(), lines, confidence))

    def _band(self, image: Image.Image) -> OcrResult | None:
        rgb = image.convert("RGB")
        data, stride = rgb.tobytes(), rgb.width * 3
        for width, source, lines, confidence in reversed(self._regions):
            if width != rgb.width or not data:
                continue
            offset = source.find(data)
            while offset >= 0 and offset % stride:
                offset = source.find(data, offset + 1)
            if offset < 0:
                continue
            top = offset // stride
            inside = [text for y0, y1, text in lines if y0 >= top and y1 <= top + rgb.height]
            return OcrResult("\n".join(inside), confidence if inside else 1.0)
        return None

    def recognize_line(self, image: Image.Image) -> OcrResult:
        self.calls += 1
        return self._table.get(imageops.digest(image.convert("RGB")), EMPTY)

    def recognize_block(self, image: Image.Image) -> OcrResult:
        # Unregistered block regions contain no text (confidence 1.0 means "confidently empty").
        self.calls += 1
        found = self._table.get(imageops.digest(image.convert("RGB")))
        if found is None and self._regions:
            found = self._band(image)
        return found if found is not None else OcrResult("", 1.0)


def create_engine(backend: str, tesseract_lang: str = "chi_sim+eng", threads: int = 0) -> OcrEngine:
    if backend == "rapidocr":
        return RapidOcrEngine(threads=threads)
    if backend == "tesseract":
        return TesseractEngine(lang=tesseract_lang)
    if backend == "fake":
        return FakeOcrEngine()
    raise OcrUnavailable(f"unknown OCR backend {backend!r}")
