"""Desktop input adapters used only by the reply executor.

Input is sent to the *existing* X display (no new browser or desktop session). Nothing in
this module bypasses verification prompts or alters browser fingerprints; it moves the
pointer, clicks and types like a person at the keyboard would.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Protocol

log = logging.getLogger(__name__)


class InputError(RuntimeError):
    pass


class InputAdapter(Protocol):
    async def click(self, x: int, y: int) -> None: ...

    async def type_text(self, text: str) -> None: ...

    async def press(self, key: str) -> None: ...


class XdotoolInput:
    """``xdotool`` (+ ``xclip`` for the clipboard method) against DISPLAY/XAUTHORITY."""

    def __init__(self, env: dict[str, str], text_method: str = "clipboard", timeout: float = 10.0):
        self.env = env
        self.text_method = text_method
        self.timeout = timeout
        if shutil.which("xdotool") is None:
            raise InputError("xdotool not found")
        if text_method == "clipboard" and shutil.which("xclip") is None:
            raise InputError("xclip not found (required for input_text_method=clipboard)")

    async def _run(self, *args: str, stdin: bytes | None = None, detaches: bool = False) -> None:
        # A detaching helper (xclip keeps a forked child serving the selection) must not
        # inherit our pipes, otherwise reading stderr would wait for that child to exit.
        proc = await asyncio.create_subprocess_exec(
            *args, env=self.env, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL if detaches else asyncio.subprocess.PIPE)
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(stdin), timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            proc.kill()
            raise InputError(f"{args[0]} timed out") from exc
        if proc.returncode != 0:
            detail = (stderr or b"").decode("utf-8", "replace")[:200]
            raise InputError(f"{args[0]} failed (exit {proc.returncode}): {detail}")

    async def click(self, x: int, y: int) -> None:
        # --sync waits for a motion event, which never arrives if the pointer is
        # already at this position. X requests stay ordered without that wait.
        await self._run("xdotool", "mousemove", str(int(x)), str(int(y)), "click", "1")

    async def is_browser_active(self) -> bool:
        """Read X11 window ownership before maintenance input; never focus another window."""
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec("xdotool", "getactivewindow", env=self.env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), self.timeout)
            if proc.returncode or not out.strip():
                return False
            proc = await asyncio.create_subprocess_exec("xprop", "-id", out.decode().strip(), "WM_CLASS",
                env=self.env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), self.timeout)
            return proc.returncode == 0 and any(name in out.lower() for name in (b"google-chrome", b"chromium"))
        except (OSError, asyncio.TimeoutError):
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            return False

    async def type_text(self, text: str) -> None:
        if self.text_method == "clipboard":
            # xclip forks a selection owner that serves the paste; it exits on next selection.
            await self._run("xclip", "-selection", "clipboard", "-in", stdin=text.encode("utf-8"), detaches=True)
            await self._run("xdotool", "key", "--clearmodifiers", "ctrl+v")
        else:
            await self._run("xdotool", "type", "--clearmodifiers", "--delay", "40", "--", text)

    async def press(self, key: str) -> None:
        await self._run("xdotool", "key", "--clearmodifiers", key)


class RecordingInput:
    """Fake adapter that records actions; used by tests and dry-run diagnostics."""

    def __init__(self) -> None:
        self.actions: list[tuple] = []

    async def click(self, x: int, y: int) -> None:
        self.actions.append(("click", x, y))

    async def type_text(self, text: str) -> None:
        self.actions.append(("type", text))

    async def press(self, key: str) -> None:
        self.actions.append(("press", key))
