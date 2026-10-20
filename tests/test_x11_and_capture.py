import os

import pytest
from PIL import Image

from xianyu_monitor.capture.backends import CallableCapture, CaptureError, ReplayCapture
from xianyu_monitor.capture.x11 import DisplayDiscoveryError, discover, list_sessions, x_env


def fake_proc(root, pid, args, cwd=None):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in args) + b"\0")
    if cwd:
        os.symlink(cwd, d / "cwd")


def test_discovers_single_xvnc_with_relative_authority(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".Xauthority").write_text("x")
    proc = tmp_path / "proc"
    fake_proc(proc, 100, ["Xvnc", ":10", "-auth", ".Xauthority", "-geometry", "1280x720", "-depth", "16"], cwd=home)
    fake_proc(proc, 200, ["bash"])
    session = discover(proc_root=proc)
    assert session.display == ":10" and session.xauthority == str(home / ".Xauthority")
    env = x_env(session, {"PATH": "/usr/bin", "SECRET": "x"})
    assert env["DISPLAY"] == ":10" and "SECRET" not in env


def test_multiple_displays_require_configuration(tmp_path):
    proc = tmp_path / "proc"
    fake_proc(proc, 100, ["Xvnc", ":10", "-auth", "/a"])
    fake_proc(proc, 101, ["Xvnc", ":11", "-auth", "/b"])
    assert len(list_sessions(proc)) == 2
    with pytest.raises(DisplayDiscoveryError):
        discover(proc_root=proc)
    assert discover(":11", proc_root=proc).xauthority == "/b"
    with pytest.raises(DisplayDiscoveryError):
        discover("10; rm -rf /", proc_root=proc)


def test_no_x_server(tmp_path):
    (tmp_path / "proc").mkdir()
    with pytest.raises(DisplayDiscoveryError):
        discover(proc_root=tmp_path / "proc")


async def test_replay_and_callable_capture(tmp_path):
    for i in range(2):
        Image.new("RGB", (10, 10), (i, i, i)).save(tmp_path / f"{i:03d}.png")
    replay = ReplayCapture(tmp_path)
    assert (await replay.capture()).image.getpixel((0, 0)) == (0, 0, 0)
    assert (await replay.capture()).image.getpixel((0, 0)) == (1, 1, 1)
    with pytest.raises(CaptureError):
        await replay.capture()
    failing = CallableCapture(lambda: RuntimeError("display gone"))
    with pytest.raises(CaptureError):
        await failing.capture()
