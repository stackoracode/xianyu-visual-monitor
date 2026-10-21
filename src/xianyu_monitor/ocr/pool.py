"""Run blocking/CPU-heavy OCR outside the asyncio event loop.

Production uses a small ``ProcessPoolExecutor`` (default one process) whose initializer
creates the engine once; crops travel as PNG bytes. Tests use the in-process mode with a
fake engine. A crashed OCR process (``BrokenProcessPool``) is recreated on the next call.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from collections.abc import Callable
from concurrent.futures.process import BrokenProcessPool

from PIL import Image

from xianyu_monitor.ocr.base import OcrEngine, OcrResult, create_engine
from xianyu_monitor.vision import imageops

log = logging.getLogger(__name__)

_ENGINE: OcrEngine | None = None
BLOCK_PREFIX = "block:"
BUBBLE_PREFIX = "bubble:"


def _init_worker(backend: str, lang: str) -> None:  # pragma: no cover - runs in child process
    global _ENGINE
    _ENGINE = create_engine(backend, lang)


def _recognize_batch(items: list[tuple[str, bytes]]) -> dict[str, tuple[str, float]]:  # pragma: no cover
    assert _ENGINE is not None, "OCR worker not initialized"
    out: dict[str, tuple[str, float]] = {}
    for key, png in items:
        result = _recognize_one(_ENGINE, key, imageops.from_png_bytes(png))
        out[key] = (result.text, result.confidence)
    return out


def _recognize_one(engine: OcrEngine, key: str, image: Image.Image) -> OcrResult:
    """Keys prefixed with ``block:`` request multi-line block OCR; others are single lines."""
    if key.startswith(BUBBLE_PREFIX):
        return getattr(engine, "recognize_bubble", engine.recognize_block)(image)
    if key.startswith(BLOCK_PREFIX):
        return engine.recognize_block(image)
    return engine.recognize_line(image)


class OcrPool:
    def __init__(self, backend: str = "rapidocr", workers: int = 1, use_processes: bool = True,
                 lang: str = "chi_sim+eng", engine_factory: Callable[[], OcrEngine] | None = None):
        self.backend = backend
        self.workers = max(1, workers)
        self.use_processes = use_processes and engine_factory is None and backend != "fake"
        self.lang = lang
        self._engine_factory = engine_factory
        self._engine: OcrEngine | None = None
        self._executor: concurrent.futures.Executor | None = None
        self.last_batch_ms = 0.0
        self.total_calls = 0

    def _ensure(self) -> concurrent.futures.Executor:
        if self._executor is None:
            if self.use_processes:
                self._executor = concurrent.futures.ProcessPoolExecutor(
                    max_workers=self.workers, initializer=_init_worker, initargs=(self.backend, self.lang))
            else:
                if self._engine is None:
                    self._engine = self._engine_factory() if self._engine_factory else create_engine(self.backend, self.lang)
                self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr")
        return self._executor

    @property
    def engine(self) -> OcrEngine | None:
        return self._engine

    def _run_inline(self, items: list[tuple[str, Image.Image]]) -> dict[str, OcrResult]:
        assert self._engine is not None
        engine = self._engine
        return {key: _recognize_one(engine, key, img) for key, img in items}

    async def recognize(self, items: list[tuple[str, Image.Image]]) -> dict[str, OcrResult]:
        if not items:
            return {}
        loop = asyncio.get_running_loop()
        started = time.perf_counter()
        executor = self._ensure()
        self.total_calls += len(items)
        try:
            if self.use_processes:
                payload = [(key, imageops.to_png_bytes(img)) for key, img in items]
                raw = await loop.run_in_executor(executor, _recognize_batch, payload)
                result = {key: OcrResult(text, conf) for key, (text, conf) in raw.items()}
            else:
                result = await loop.run_in_executor(executor, self._run_inline, items)
        except BrokenProcessPool:
            log.error("OCR process pool crashed; it will be recreated")
            self.close()
            raise
        finally:
            self.last_batch_ms = (time.perf_counter() - started) * 1000.0
        return result

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
