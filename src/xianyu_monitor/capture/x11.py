"""Discover the existing Xvnc/X display and its X authority file.

Display numbers can change (e.g. after an RDP/xrdp reconnect), so discovery is repeated
after capture failures. With more than one candidate display the operator must set
``XVM_CAPTURE_DISPLAY``; the monitor never guesses between sessions.
"""

from __future__ import annotations

import os
import pwd
import re
from dataclasses import dataclass
from pathlib import Path

_X_SERVERS = ("Xvnc", "Xtigervnc", "Xorg", "Xvfb", "X")
_DISPLAY_RE = re.compile(r"^:\d+(\.\d+)?$")


@dataclass(frozen=True)
class XSession:
    display: str
    xauthority: str | None
    pid: int | None
    server: str


class DisplayDiscoveryError(RuntimeError):
    pass


def _parse_cmdline(args: list[str]) -> tuple[str | None, str | None]:
    display = next((a for a in args[1:] if _DISPLAY_RE.match(a)), None)
    auth = None
    for i, arg in enumerate(args):
        if arg == "-auth" and i + 1 < len(args):
            auth = args[i + 1]
    return display, auth


def _resolve_auth(auth: str | None, pid: int, proc_root: Path) -> str | None:
    if not auth:
        return None
    if auth.startswith("/"):
        return auth
    try:
        cwd = os.readlink(proc_root / str(pid) / "cwd")
        candidate = Path(cwd) / auth
        if candidate.exists():
            return str(candidate)
    except OSError:
        pass
    try:
        uid = (proc_root / str(pid)).stat().st_uid
        return str(Path(pwd.getpwuid(uid).pw_dir) / auth)
    except (OSError, KeyError):
        return None


def list_sessions(proc_root: Path = Path("/proc")) -> list[XSession]:
    sessions: list[XSession] = []
    for entry in proc_root.iterdir() if proc_root.is_dir() else []:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        args = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if not args:
            continue
        server = os.path.basename(args[0])
        if server not in _X_SERVERS:
            continue
        display, auth = _parse_cmdline(args)
        if display is None:
            continue
        sessions.append(XSession(display, _resolve_auth(auth, int(entry.name), proc_root), int(entry.name), server))
    sessions.sort(key=lambda s: s.display)
    return sessions


def discover(display: str = "auto", xauthority: str = "auto", proc_root: Path = Path("/proc")) -> XSession:
    if display != "auto":
        if not _DISPLAY_RE.match(display):
            raise DisplayDiscoveryError(f"invalid display {display!r}")
        auth = None if xauthority == "auto" else xauthority
        if auth is None:
            match = [s for s in list_sessions(proc_root) if s.display == display]
            auth = match[0].xauthority if match else None
        return XSession(display, auth, None, "configured")
    sessions = list_sessions(proc_root)
    vnc = [s for s in sessions if s.server.startswith("Xvnc") or s.server == "Xtigervnc"]
    pool = vnc or sessions
    if not pool:
        raise DisplayDiscoveryError("no X server process found; set XVM_CAPTURE_DISPLAY")
    if len(pool) > 1:
        names = ", ".join(s.display for s in pool)
        raise DisplayDiscoveryError(f"multiple X displays ({names}); set XVM_CAPTURE_DISPLAY")
    session = pool[0]
    if xauthority != "auto":
        session = XSession(session.display, xauthority, session.pid, session.server)
    return session


def x_env(session: XSession, base: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in (base if base is not None else os.environ).items()
           if k in {"PATH", "LANG", "LC_ALL", "HOME", "USER"}}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["DISPLAY"] = session.display
    if session.xauthority:
        env["XAUTHORITY"] = session.xauthority
    return env
