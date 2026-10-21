"""Live smoke test against a real X server (Xvfb in the ARM64 CI job). Skipped elsewhere.

Verifies that the production capture backend (scrot) and input adapter (xdotool/xclip)
work against a 1280x720, 16-bit display like the board's Xvnc. It does not touch any browser.
"""

import asyncio
import os

import pytest

from xianyu_monitor.capture.backends import ScrotCapture
from xianyu_monitor.capture.x11 import discover, x_env
from xianyu_monitor.replies.input import XdotoolInput

DISPLAY = os.environ.get("XVM_TEST_X_DISPLAY", "")
pytestmark = pytest.mark.skipif(not DISPLAY, reason="XVM_TEST_X_DISPLAY not set")


async def test_scrot_capture_of_existing_display(tmp_path):
    capture = ScrotCapture(display=DISPLAY, xauthority="auto", tmp_dir=tmp_path)
    frame = await capture.capture()
    assert frame.image.size == (1280, 720) and frame.image.mode == "RGB"
    assert capture.describe()["display"] == DISPLAY


async def test_xdotool_input_commands_run():
    adapter = XdotoolInput(x_env(discover(DISPLAY)), text_method="clipboard")
    await adapter.click(10, 10)
    # Pointer already here: --sync would wait forever for a motion event.
    await asyncio.wait_for(adapter.click(10, 10), timeout=2)
    await adapter.type_text("REF-TEST 测试")
    await adapter.press("Escape")
    typing = XdotoolInput(x_env(discover(DISPLAY)), text_method="xdotool_type")
    await typing.type_text("abc")
