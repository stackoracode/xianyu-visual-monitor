"""CPU topology helpers for heterogeneous (big.LITTLE) boards such as the RK3588.

The RK3588 has four Cortex-A55 and four Cortex-A76 cores. ONNX Runtime's default thread
pool uses one thread per core and splits each operator evenly, so the slower A55 cores
gate every step. Pinning the OCR process to the fastest cluster avoids that straggler
effect. Pinning changes only scheduling, never the pixels or models used for OCR.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

SYS_CPU = Path("/sys/devices/system/cpu")


def parse_cpu_list(text: str) -> list[int]:
    """Parse ``"4-7"`` / ``"0,2,4-5"`` (the kernel cpulist format)."""
    cpus: set[int] = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            lo, hi = int(start), int(end)
            if lo > hi:
                raise ValueError(f"invalid cpu range {part!r}")
            cpus.update(range(lo, hi + 1))
        else:
            cpus.add(int(part))
    if any(cpu < 0 for cpu in cpus):
        raise ValueError("cpu numbers must be non-negative")
    return sorted(cpus)


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _allowed_cpus() -> list[int]:
    try:
        return sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):  # pragma: no cover - non-Linux
        return list(range(os.cpu_count() or 1))


def performance_cpus(sys_root: Path = SYS_CPU, allowed: list[int] | None = None) -> list[int]:
    """Return the fastest cluster among allowed CPUs, or ``[]`` when the CPUs are uniform
    or the topology cannot be read (callers then leave scheduling alone)."""
    allowed = _allowed_cpus() if allowed is None else allowed
    for metric in ("cpu_capacity", "cpufreq/cpuinfo_max_freq"):
        values = {cpu: _read_int(sys_root / f"cpu{cpu}" / metric) for cpu in allowed}
        if not values or any(v is None for v in values.values()):
            continue
        best = max(v for v in values.values() if v is not None)
        fast = sorted(cpu for cpu, v in values.items() if v == best)
        return fast if len(fast) < len(allowed) else []
    return []


def resolve_affinity(spec: str, sys_root: Path = SYS_CPU, allowed: list[int] | None = None) -> list[int]:
    """``auto`` -> fastest cluster on heterogeneous CPUs; ``none``/empty -> no pinning;
    otherwise an explicit cpulist intersected with the CPUs this process may use."""
    spec = (spec or "").strip().lower()
    if spec in {"", "none", "off"}:
        return []
    allowed = _allowed_cpus() if allowed is None else allowed
    if spec == "auto":
        return performance_cpus(sys_root, allowed)
    wanted = parse_cpu_list(spec)
    usable = [cpu for cpu in wanted if cpu in set(allowed)]
    if not usable:
        log.warning("ocr_cpu_affinity %s matches no usable CPU; not pinning", spec)
    return usable


def apply_affinity(cpus: list[int]) -> bool:
    if not cpus:
        return False
    try:
        os.sched_setaffinity(0, set(cpus))
    except (AttributeError, OSError) as exc:  # pragma: no cover - platform dependent
        log.warning("could not pin OCR process to CPUs %s: %s", cpus, exc)
        return False
    return True
