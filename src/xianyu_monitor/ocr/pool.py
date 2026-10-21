"""Run blocking/CPU-heavy OCR outside the asyncio event loop.

Production uses a small ``ProcessPoolExecutor`` (default one process) whose initializer
creates the engine once; crops travel as raw pixel buffers (no PNG encode/decode). Tests use
the in-process mode with a fake engine. A crashed OCR process (``BrokenProcessPool``) is
recreated on the next call.

Results are memoized by *exact* pixel content and OCR mode. The engines are deterministic
for identical input, so a cache hit returns what a fresh read of the same pixels would; any
changed pixel (caret blink, new bubble, dialog) is a different key and is read again.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures.process import BrokenProcessPool

from PIL import Image

from xianyu_monitor.cpu import apply_affinity
from xianyu_monitor.ocr.base import OcrEngine, OcrResult, create_engine
from xianyu_monitor.vision import imageops

log = logging.getLogger(__name__)

_ENGINE: OcrEngine | None = None
BLOCK_PREFIX = "block:"
BUBBLE_PREFIX = "bubble:"


def _init_worker(backend: str, lang: str, cpus: tuple[int, ...] = (), threads: int = 0) -> None:  # pragma: no cover
    global _ENGINE
    if cpus and apply_affinity(list(cpus)):
        log.info("OCR process pinned to CPUs %s", ",".join(map(str, cpus)))
    _ENGINE = create_engine(backend, lang, threads=threads)


def _recognize_batch(items: list[tuple[str, str, tuple[int, int], bytes]]) -> dict[str, tuple[str, float]]:  # pragma: no cover
    assert _ENGINE is not None, "OCR worker not initialized"
    out: dict[str, tuple[str, float]] = {}
    for key, mode, size, data in items:
        result = _recognize_one(_ENGINE, key, Image.frombytes(mode, size, data))
        out[key] = (result.text, result.confidence)
    return out


def ocr_mode(key: str) -> str:
    if key.startswith(BUBBLE_PREFIX):
        return "bubble"
    if key.startswith(BLOCK_PREFIX):
        return "block"
    return "line"


def _recognize_one(engine: OcrEngine, key: str, image: Image.Image) -> OcrResult:
    """Keys prefixed with ``block:`` request multi-line block OCR; others are single lines."""
    mode = ocr_mode(key)
    if mode == "bubble":
        return getattr(engine, "recognize_bubble", engine.recognize_block)(image)
    if mode == "block":
        return engine.recognize_block(image)
    return engine.recognize_line(image)


class OcrPool:
    def __init__(self, backend: str = "rapidocr", workers: int = 1, use_processes: bool = True,
                 lang: str = "chi_sim+eng", engine_factory: Callable[[], OcrEngine] | None = None,
                 cache_size: int = 0, cpus: list[int] | None = None, threads: int = 0):
        self.backend = backend
        self.workers = max(1, workers)
        self.use_processes = use_processes and engine_factory is None and backend != "fake"
        self.lang = lang
        self._engine_factory = engine_factory
        self._engine: OcrEngine | None = None
        self._executor: concurrent.futures.Executor | None = None
        self.cpus = tuple(cpus or ())
        # One ORT thread per pinned core; unpinned keeps the engine's own default.
        self.threads = threads if threads > 0 else len(self.cpus)
        self.cache_size = max(0, int(cache_size))
        self._cache: OrderedDict[tuple[str, str], OcrResult] = OrderedDict()
        self.cache_hits = 0
        self.cache_misses = 0
        self.last_batch_ms = 0.0
        self.total_calls = 0

    def _ensure(self) -> concurrent.futures.Executor:
        if self._executor is None:
            if self.use_processes:
                self._executor = concurrent.futures.ProcessPoolExecutor(
                    max_workers=self.workers, initializer=_init_worker,
                    initargs=(self.backend, self.lang, self.cpus, self.threads))
            else:
                if self._engine is None:
                    self._engine = (self._engine_factory() if self._engine_factory
                                    else create_engine(self.backend, self.lang, threads=self.threads))
                self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr")
        return self._executor

    @property
    def engine(self) -> OcrEngine | None:
        return self._engine

    def stats(self) -> dict:
        lookups = self.cache_hits + self.cache_misses
        return {"cache_hits": self.cache_hits, "cache_misses": self.cache_misses,
                "cache_hit_ratio": round(self.cache_hits / lookups, 3) if lookups else None,
                "cache_entries": len(self._cache), "engine_calls": self.total_calls,
                "cpus": list(self.cpus), "threads": self.threads}

    def clear_cache(self) -> None:
        self._cache.clear()

    def _run_inline(self, items: list[tuple[str, Image.Image]]) -> dict[str, OcrResult]:
        assert self._engine is not None
        engine = self._engine
        return {key: _recognize_one(engine, key, img) for key, img in items}

    async def _run(self, items: list[tuple[str, Image.Image]]) -> dict[str, OcrResult]:
        loop = asyncio.get_running_loop()
        executor = self._ensure()
        self.total_calls += len(items)
        try:
            if self.use_processes:
                payload = []
                for key, img in items:
                    if img.mode not in {"RGB", "L"}:
                        img = img.convert("RGB")
                    payload.append((key, img.mode, img.size, img.tobytes()))
                raw = await loop.run_in_executor(executor, _recognize_batch, payload)
                return {key: OcrResult(text, conf) for key, (text, conf) in raw.items()}
            return await loop.run_in_executor(executor, self._run_inline, items)
        except BrokenProcessPool:
            log.error("OCR process pool crashed; it will be recreated")
            self.close()
            raise

    async def recognize(self, items: list[tuple[str, Image.Image]]) -> dict[str, OcrResult]:
        if not items:
            return {}
        started = time.perf_counter()
        try:
            if not self.cache_size:
                return await self._run(items)
            result: dict[str, OcrResult] = {}
            misses: list[tuple[str, Image.Image]] = []
            miss_keys: dict[str, tuple[str, str]] = {}
            pending: dict[tuple[str, str], str] = {}  # identical crops within one batch run once
            aliases: list[tuple[str, str]] = []
            for key, img in items:
                cache_key = (ocr_mode(key), imageops.digest(img))
                cached = self._cache.get(cache_key)
                if cached is not None:
                    self._cache.move_to_end(cache_key)
                    self.cache_hits += 1
                    result[key] = cached
                elif cache_key in pending:
                    self.cache_hits += 1
                    aliases.append((key, pending[cache_key]))
                else:
                    self.cache_misses += 1
                    pending[cache_key] = key
                    miss_keys[key] = cache_key
                    misses.append((key, img))
            if misses:
                fresh = await self._run(misses)
                for key, value in fresh.items():
                    result[key] = value
                    self._cache[miss_keys[key]] = value
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
            for key, source in aliases:
                result[key] = result[source]
            return result
        finally:
            self.last_batch_ms = (time.perf_counter() - started) * 1000.0

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
