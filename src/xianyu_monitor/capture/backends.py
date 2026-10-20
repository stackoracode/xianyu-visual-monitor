"""Screenshot backends. All of them read the existing desktop; none starts a browser."""

from __future__ import annotations

import asyncio
import os
import shlex
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PIL import Image

from xianyu_monitor.capture.x11 import XSession, discover, x_env
from xianyu_monitor.timeutil import utcnow
from xianyu_monitor.vision import imageops


class CaptureError(RuntimeError):
    pass


@dataclass
class CapturedFrame:
    image: Image.Image
    captured_at: object  # datetime (naive UTC)
    capture_ms: float
    source: str


class CaptureBackend(Protocol):
    async def capture(self) -> CapturedFrame: ...

    def describe(self) -> dict: ...


class _SubprocessCapture:
    def __init__(self, display: str = "auto", xauthority: str = "auto", tmp_dir: Path | None = None,
                 timeout: float = 15.0):
        self.display = display
        self.xauthority = xauthority
        self.tmp_dir = tmp_dir
        self.timeout = timeout
        self.session: XSession | None = None
        self.failures = 0

    def _session(self) -> XSession:
        if self.session is None:
            self.session = discover(self.display, self.xauthority)
        return self.session

    def env(self) -> dict[str, str]:
        return x_env(self._session())

    def invalidate(self) -> None:
        self.session = None

    def describe(self) -> dict:
        s = self.session
        return {"display": s.display if s else None, "xauthority_set": bool(s and s.xauthority),
                "server": s.server if s else None}

    async def _run(self, args: list[str], stdout_png: bool) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            *args, env=self.env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            proc.kill()
            raise CaptureError(f"{args[0]} timed out") from exc
        if proc.returncode != 0:
            raise CaptureError(f"{args[0]} exit {proc.returncode}: {err.decode('utf-8', 'replace')[:200]}")
        return out if stdout_png else b""


class ScrotCapture(_SubprocessCapture):
    """``scrot`` writes the root window (works with the measured 16-bit Xvnc depth)."""

    async def capture(self) -> CapturedFrame:
        started = time.perf_counter()
        at = utcnow()
        tmp_dir = str(self.tmp_dir) if self.tmp_dir else None
        fd, path = tempfile.mkstemp(prefix="xvm-", suffix=".png", dir=tmp_dir)
        os.close(fd)
        try:
            try:
                await self._run(["scrot", "-o", path], stdout_png=False)
            except (CaptureError, OSError):
                self.invalidate()
                raise
            try:
                image = Image.open(path)
                image.load()
            except OSError as exc:
                raise CaptureError(f"unreadable screenshot: {exc}") from exc
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        self.failures = 0
        return CapturedFrame(image.convert("RGB"), at, (time.perf_counter() - started) * 1000.0, "scrot")


class CommandCapture(_SubprocessCapture):
    """Any command that prints a PNG of the screen to stdout (e.g. ``import -window root png:-``)."""

    def __init__(self, command: str, **kwargs):
        super().__init__(**kwargs)
        if not command.strip():
            raise CaptureError("XVM_CAPTURE_COMMAND is empty")
        self.args = shlex.split(command)

    async def capture(self) -> CapturedFrame:
        started = time.perf_counter()
        at = utcnow()
        try:
            data = await self._run(self.args, stdout_png=True)
        except (CaptureError, OSError):
            self.invalidate()
            raise
        try:
            image = imageops.from_png_bytes(data)
        except OSError as exc:
            raise CaptureError(f"command output is not an image: {exc}") from exc
        return CapturedFrame(image.convert("RGB"), at, (time.perf_counter() - started) * 1000.0, "command")


class ReplayCapture:
    """Replays PNG files from a directory in name order (offline calibration and tests)."""

    def __init__(self, directory: Path, loop: bool = False):
        self.files = sorted(p for p in directory.iterdir() if p.suffix.lower() == ".png")
        if not self.files:
            raise CaptureError(f"no PNG files in {directory}")
        self.loop = loop
        self.index = 0

    def describe(self) -> dict:
        return {"replay_files": len(self.files), "position": self.index}

    async def capture(self) -> CapturedFrame:
        if self.index >= len(self.files):
            if not self.loop:
                raise CaptureError("replay exhausted")
            self.index = 0
        path = self.files[self.index]
        self.index += 1
        image = Image.open(path)
        image.load()
        return CapturedFrame(image.convert("RGB"), utcnow(), 0.0, f"replay:{path.name}")


class CallableCapture:
    """Wraps a function returning images (fake desktops in tests)."""

    def __init__(self, source: Callable[[], Image.Image] | Iterable[Image.Image]):
        if callable(source):
            self._next = source
        else:
            iterator = iter(source)
            self._next = lambda: next(iterator)

    def describe(self) -> dict:
        return {"backend": "callable"}

    async def capture(self) -> CapturedFrame:
        try:
            image = self._next()
        except StopIteration as exc:
            raise CaptureError("source exhausted") from exc
        if isinstance(image, Exception):
            raise CaptureError(str(image))
        return CapturedFrame(image.convert("RGB"), utcnow(), 0.0, "callable")


def create_capture(settings) -> CaptureBackend:
    kwargs = {"display": settings.capture_display, "xauthority": settings.capture_xauthority,
              "tmp_dir": settings.run_dir}
    if settings.capture_backend == "scrot":
        return ScrotCapture(**kwargs)
    if settings.capture_backend == "command":
        return CommandCapture(settings.capture_command, **kwargs)
    if settings.capture_backend == "replay":
        if settings.replay_dir is None:
            raise CaptureError("XVM_REPLAY_DIR is required for the replay backend")
        return ReplayCapture(settings.replay_dir)
    raise CaptureError(f"unknown capture backend {settings.capture_backend}")
