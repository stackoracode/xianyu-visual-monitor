"""Command line entry point: ``xianyu-monitor <command>``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import logging
import os
import shutil
import signal
import sys
from dataclasses import asdict
from pathlib import Path

from xianyu_monitor import __version__
from xianyu_monitor.logging_setup import configure_logging
from xianyu_monitor.settings import Settings, load_settings

log = logging.getLogger("xianyu_monitor.cli")


def _settings() -> Settings:
    return load_settings()


def cmd_web(args: argparse.Namespace) -> int:
    import uvicorn

    settings = _settings()
    configure_logging(settings.log_level, settings.log_json)
    uvicorn.run("xianyu_monitor.web.asgi:app", host=args.host or settings.web_host, port=args.port or settings.web_port,
                workers=args.workers, proxy_headers=False, log_config=None, access_log=False)
    return 0


def ocr_pool_from_settings(settings: Settings, use_processes: bool | None = None):
    from xianyu_monitor.cpu import resolve_affinity
    from xianyu_monitor.ocr.pool import OcrPool

    processes = settings.ocr_use_processes if use_processes is None else use_processes
    # Affinity is applied inside OCR child processes only, never to the worker's event loop.
    cpus = resolve_affinity(settings.ocr_cpu_affinity) if processes else []
    return OcrPool(settings.ocr_backend, settings.ocr_workers, processes, settings.tesseract_lang,
                   cache_size=settings.ocr_cache_size, cpus=cpus, threads=settings.ocr_threads)


def build_worker(settings: Settings):
    from xianyu_monitor.capture.backends import create_capture
    from xianyu_monitor.capture.x11 import discover, x_env
    from xianyu_monitor.db import Database
    from xianyu_monitor.replies.input import RecordingInput, XdotoolInput
    from xianyu_monitor.worker.service import MonitorService

    db = Database(settings.database_url)
    capture = create_capture(settings)
    ocr = ocr_pool_from_settings(settings)

    def input_factory():
        if settings.input_backend == "fake":
            return RecordingInput()
        session = discover(settings.capture_display, settings.capture_xauthority)
        return XdotoolInput(x_env(session), settings.input_text_method)

    return MonitorService(settings, db, capture, ocr, input_factory=input_factory)


def cmd_worker(args: argparse.Namespace) -> int:
    settings = _settings()
    configure_logging(settings.log_level, settings.log_json, settings.logs_dir / "worker.log")
    settings.run_dir.mkdir(parents=True, exist_ok=True)
    settings.frames_dir.mkdir(parents=True, exist_ok=True)

    async def main() -> None:
        service = build_worker(settings)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, service.request_stop)
        try:
            await service.run()
        finally:
            await service.db.dispose()

    from xianyu_monitor.worker.service import WorkerAlreadyRunning

    try:
        asyncio.run(main())
    except WorkerAlreadyRunning as exc:
        log.error("%s", exc)
        return 3
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from xianyu_monitor.db import run_migrations

    settings = _settings()
    run_migrations(settings.database_url, args.revision)
    print("migrations applied")
    return 0


def cmd_create_admin(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from xianyu_monitor.db import Database
    from xianyu_monitor.models import AdminUser
    from xianyu_monitor.web.security import hash_password

    settings = _settings()
    if args.password_env:
        password = os.environ.get(args.password_env, "")
    else:
        password = getpass.getpass("New admin password: ")
        if password != getpass.getpass("Repeat password: "):
            print("passwords do not match", file=sys.stderr)
            return 2
    try:
        encoded = hash_password(password)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    async def main() -> None:
        db = Database(settings.database_url)
        try:
            async with db.transaction() as session:
                user = (await session.execute(select(AdminUser).where(AdminUser.username == args.username))).scalar_one_or_none()
                if user is None:
                    session.add(AdminUser(username=args.username, password_hash=encoded, language=args.language))
                else:
                    user.password_hash = encoded
                    user.failed_logins = 0
                    user.locked_until = None
        finally:
            await db.dispose()

    asyncio.run(main())
    print(f"admin user {args.username!r} saved")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    settings = _settings()
    report: dict = {"version": __version__, "settings": settings.redacted(), "checks": {}}
    checks = report["checks"]
    for binary in ("scrot", "xdotool", "xclip", "tesseract"):
        checks[f"bin:{binary}"] = bool(shutil.which(binary))
    try:
        from xianyu_monitor.capture.x11 import discover

        session = discover(settings.capture_display, settings.capture_xauthority)
        checks["display"] = {"display": session.display, "xauthority_readable":
                             bool(session.xauthority and os.access(session.xauthority, os.R_OK))}
    except Exception as exc:  # noqa: BLE001
        checks["display"] = f"error: {exc}"
    try:
        from xianyu_monitor.ocr.base import create_engine

        create_engine(settings.ocr_backend, settings.tesseract_lang)
        checks["ocr"] = f"{settings.ocr_backend}: ok"
    except Exception as exc:  # noqa: BLE001
        checks["ocr"] = f"{settings.ocr_backend}: {exc}"
    if settings.calibration_file:
        try:
            from xianyu_monitor.vision.profile import load_profile

            load_profile(settings.calibration_file)
            checks["calibration_file"] = "valid"
        except Exception as exc:  # noqa: BLE001
            checks["calibration_file"] = f"invalid: {exc}"

    async def db_check() -> str:
        from sqlalchemy import text

        from xianyu_monitor.db import Database

        db = Database(settings.database_url)
        try:
            async with db.session() as session:
                await session.execute(text("SELECT 1"))
            return "ok"
        finally:
            await db.dispose()

    try:
        checks["database"] = asyncio.run(db_check())
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {type(exc).__name__}"
    checks["secret_key"] = len(settings.secret_key) >= 32
    checks["smtp_configured"] = settings.smtp_configured
    checks["recipients"] = len(settings.notify_to)
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    from xianyu_monitor.capture.x11 import list_sessions

    for session in list_sessions():
        print(json.dumps(asdict(session)))
    return 0


def cmd_capture_once(args: argparse.Namespace) -> int:
    from xianyu_monitor.capture.backends import create_capture

    settings = _settings()

    async def main() -> None:
        frame = await create_capture(settings).capture()
        frame.image.save(args.out, format="PNG")
        print(json.dumps({"out": args.out, "size": frame.image.size, "capture_ms": round(frame.capture_ms, 1)}))

    asyncio.run(main())
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """Offline analysis of a saved screenshot (calibration aid). Prints OCR results; no DB."""
    from PIL import Image

    from xianyu_monitor.ocr.pool import OcrPool
    from xianyu_monitor.runtime_settings import RuntimeSettings
    from xianyu_monitor.timeutil import utcnow
    from xianyu_monitor.vision.analyzer import FrameAnalyzer
    from xianyu_monitor.vision.profile import load_profile

    profile = load_profile(Path(args.profile))
    pool = OcrPool(args.ocr, 1, use_processes=False)

    async def main() -> dict:
        analyzer = FrameAnalyzer(profile, pool)
        with Image.open(args.image) as image:
            obs = await analyzer.analyze(image.convert("RGB"), utcnow(), RuntimeSettings(), force_full=True)
            out = {"status": obs.status, "issues": obs.layout_issues, "overall_unread": obs.overall_unread,
                   "challenge": obs.challenge_hits, "ocr_ms": round(obs.ocr_ms, 1),
                   "rows": [asdict(r) for r in obs.rows]}
            if args.pane:
                pane = await analyzer.analyze_pane(image.convert("RGB"))
                out["pane"] = asdict(pane)
        return out

    try:
        print(json.dumps(asyncio.run(main()), indent=2, ensure_ascii=False, default=str))
    finally:
        pool.close()
    return 0


def _benchmark_image_matches(frame, reference, profile) -> dict:
    """Measure both cache states and check each result against the exhaustive oracle."""
    import time

    from xianyu_monitor.vision.reply_image import (
        clear_thumbnail_cache,
        find_outgoing_image,
        find_outgoing_image_exhaustive,
    )

    def measure(matcher):
        started = time.perf_counter()
        result = matcher(frame, reference, profile.message_pane, profile.image_match_threshold)
        return result, round((time.perf_counter() - started) * 1000.0, 1)

    def identical(expected, actual):
        return (expected is None and actual is None) or (
            expected is not None and actual is not None and expected.box == actual.box
            and abs(expected.score - actual.score) < 1e-6)

    clear_thumbnail_cache()
    slow, exhaustive_ms = measure(find_outgoing_image_exhaustive)
    cold, cold_ms = measure(find_outgoing_image)
    warm, warm_ms = measure(find_outgoing_image)
    cold_ok, warm_ok = identical(slow, cold), identical(slow, warm)
    return {"image_match_exhaustive_ms": exhaustive_ms,
            "image_match_fast_cold_ms": cold_ms, "image_match_fast_warm_ms": warm_ms,
            "image_match_found": slow is not None,
            "image_match_cold_identical": cold_ok, "image_match_warm_identical": warm_ok,
            "image_match_identical": cold_ok and warm_ok}


def cmd_benchmark(args: argparse.Namespace) -> int:
    """Time analysis stages on private screenshots and check that the optimizations agree
    with fresh/exhaustive results. Prints timings and booleans only, never text or pixels."""
    import time

    from PIL import Image

    from xianyu_monitor.cpu import resolve_affinity
    from xianyu_monitor.ocr.pool import OcrPool
    from xianyu_monitor.runtime_settings import RuntimeSettings
    from xianyu_monitor.timeutil import utcnow
    from xianyu_monitor.vision.analyzer import FrameAnalyzer
    from xianyu_monitor.vision.profile import load_profile

    settings = _settings()
    profile = load_profile(Path(args.profile))
    rt = RuntimeSettings()
    affinity = args.cpus if args.cpus is not None else settings.ocr_cpu_affinity
    cpus = resolve_affinity(affinity) if args.processes else []
    threads = args.threads if args.threads is not None else settings.ocr_threads

    def pool(cache: int) -> OcrPool:
        return OcrPool(args.ocr, 1, use_processes=args.processes, lang=settings.tesseract_lang,
                       cache_size=cache, cpus=cpus, threads=threads)

    def ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000.0, 1)

    def texts(obs, pane) -> list:
        rows = [(r.index, r.name, r.preview, r.time_text, r.badge_count) for r in obs.rows]
        return [rows, obs.challenge_hits, pane and (pane.title, pane.composer_text,
                                                    [(b.direction, b.text) for b in pane.bubbles])]

    frames = []
    for path in args.image:
        with Image.open(path) as image:
            frames.append(image.convert("RGB"))

    async def main() -> dict:
        plain, cached = pool(0), pool(max(1, settings.ocr_cache_size))
        report: dict = {"ocr": args.ocr, "processes": args.processes, "cpus": cpus, "threads": threads,
                        "frames": []}
        try:
            warm = FrameAnalyzer(profile, plain)
            await warm.analyze(frames[0], utcnow(), rt, force_full=True)  # load models once
            previous = None
            for frame in frames:
                entry: dict = {}
                fresh = []
                for _ in range(max(1, args.repeat)):
                    analyzer = FrameAnalyzer(profile, plain)
                    started = time.perf_counter()
                    obs = await analyzer.analyze(frame, utcnow(), rt, force_full=True)
                    entry.setdefault("analyze_uncached_ms", []).append(ms(started))
                    pane = None
                    if profile.message_pane is not None:
                        started = time.perf_counter()
                        pane = await analyzer.analyze_pane(frame)
                        entry.setdefault("pane_uncached_ms", []).append(ms(started))
                    fresh.append(texts(obs, pane))
                # The exact-pixel cache is only valid if re-reading identical pixels is stable.
                entry["fresh_reads_identical"] = all(item == fresh[0] for item in fresh)
                analyzer = FrameAnalyzer(profile, cached)
                await analyzer.analyze(frame, utcnow(), rt, force_full=True)
                started = time.perf_counter()
                obs = await analyzer.analyze(frame, utcnow(), rt, force_full=True)
                entry["analyze_cached_ms"] = ms(started)
                pane = None
                if profile.message_pane is not None:
                    await analyzer.analyze_pane(frame)
                    started = time.perf_counter()
                    pane = await analyzer.analyze_pane(frame)
                    entry["pane_cached_ms"] = ms(started)
                entry["cached_matches_fresh"] = texts(obs, pane) == fresh[0]
                if profile.challenge_probe is not None:
                    full = FrameAnalyzer(profile, plain)
                    started = time.perf_counter()
                    full_hits = await full._probe_challenge(frame, rt)
                    entry["probe_full_ms"] = ms(started)
                    if previous is not None:
                        incremental = FrameAnalyzer(profile, plain)
                        await incremental._probe_challenge(previous, rt)
                        started = time.perf_counter()
                        hits = await incremental._probe_challenge(frame, rt)
                        entry["probe_incremental_ms"] = ms(started)
                        entry["probe_incremental_mode"] = ("incremental" if incremental.probe_stats["incremental"]
                                                           else "full")
                        entry["probe_incremental_matches_full"] = sorted(hits) == sorted(full_hits)
                if args.reference and profile.message_pane is not None:
                    from xianyu_monitor.vision.reply_image import load_asset

                    reference = load_asset(str(Path(args.reference).resolve())).image
                    entry.update(_benchmark_image_matches(frame, reference, profile))
                report["frames"].append(entry)
                previous = frame
            report["ocr_cache"] = cached.stats()
        finally:
            plain.close()
            cached.close()
        return report

    print(json.dumps(asyncio.run(main()), indent=2))
    return 0


def _run_db(coro_factory) -> object:
    from xianyu_monitor.db import Database

    settings = _settings()

    async def main():
        db = Database(settings.database_url)
        try:
            async with db.transaction() as session:
                return await coro_factory(session)
        finally:
            await db.dispose()

    return asyncio.run(main())


def cmd_import_baseline(args: argparse.Namespace) -> int:
    from xianyu_monitor.web.services import import_baseline

    names = Path(args.file).read_text(encoding="utf-8").splitlines()
    print(_run_db(lambda s: import_baseline(s, names, "cli")))
    return 0


def cmd_import_rules(args: argparse.Namespace) -> int:
    from xianyu_monitor.models import KeywordRule
    from xianyu_monitor.replies.rules import Rule, validate_rule
    from xianyu_monitor.timeutil import utcnow

    items = json.loads(Path(args.file).read_text(encoding="utf-8"))

    async def run(session) -> int:
        count = 0
        for item in items:
            rule = Rule(id=0, name=item["name"], priority=int(item.get("priority", 100)),
                        match_type=item.get("match_type", "contains"), match_mode=item.get("match_mode", "any"),
                        patterns=list(item.get("patterns", [])), exclude_patterns=list(item.get("exclude_patterns", [])),
                        template=item["template"], cooldown_seconds=int(item.get("cooldown_seconds", 3600)),
                        scope=item.get("scope", "all"), allow_sensitive=bool(item.get("allow_sensitive", False)),
                        requires_review=bool(item.get("requires_review", False)),
                        enabled=bool(item.get("enabled", False)) and args.keep_enabled,
                        trigger_type=str(item.get("trigger_type", "keyword")), send_image=bool(item.get("send_image", False)))
            validate_rule(rule)
            session.add(KeywordRule(name=rule.name, enabled=rule.enabled, priority=rule.priority,
                                    match_type=rule.match_type, match_mode=rule.match_mode, patterns=rule.patterns,
                                    exclude_patterns=rule.exclude_patterns, template=rule.template,
                                    cooldown_seconds=rule.cooldown_seconds, scope=rule.scope,
                                    allow_sensitive=rule.allow_sensitive, requires_review=rule.requires_review,
                                    trigger_type=rule.trigger_type, send_image=rule.send_image,
                                    created_at=utcnow(), updated_at=utcnow()))
            count += 1
        return count

    print(f"imported {_run_db(run)} rule(s); rules are disabled unless --keep-enabled is given")
    return 0


def cmd_load_calibration(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from xianyu_monitor.models import CalibrationProfile
    from xianyu_monitor.timeutil import utcnow
    from xianyu_monitor.vision.profile import load_profile

    profile = load_profile(Path(args.file))

    async def run(session) -> int:
        for row in (await session.execute(select(CalibrationProfile).where(CalibrationProfile.active.is_(True)))).scalars():
            row.active = False
        row = CalibrationProfile(name=profile.name, data=profile.model_dump(mode="json"), active=True,
                                 created_at=utcnow(), created_by="cli")
        session.add(row)
        await session.flush()
        return row.id

    print(f"calibration profile stored and activated (id={_run_db(run)})")
    return 0


def cmd_smtp_test(args: argparse.Namespace) -> int:
    from xianyu_monitor.notify.emailer import SmtpConfig, SmtpSender, build_message
    from xianyu_monitor.timeutil import utcnow

    settings = _settings()
    cfg = SmtpConfig.from_settings(settings)
    sender = SmtpSender(cfg)
    if not args.send:
        print(sender.test_connection())
        return 0
    if not settings.notify_to:
        print("no recipient configured (XVM_NOTIFY_TO)", file=sys.stderr)
        return 2
    conn = sender.connect()
    try:
        sender.submit(conn, build_message(cfg, settings.notify_to, "xianyu-visual-monitor SMTP test",
                                          "This is a test message from xianyu-visual-monitor.\n",
                                          f"smtp-test:{utcnow().isoformat()}"))
    finally:
        sender.close(conn)
    print("accepted by SMTP server (acceptance is not proof of delivery)")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from xianyu_monitor.db import Database
    from xianyu_monitor.diagnostics import build_bundle

    settings = _settings()

    async def main() -> bytes:
        db = Database(settings.database_url)
        try:
            return await build_bundle(db, settings, include_frames=args.frames)
        finally:
            await db.dispose()

    Path(args.out).write_bytes(asyncio.run(main()))
    print(args.out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xianyu-monitor", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("web", help="run the admin web UI (uvicorn)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--workers", type=int, default=1)
    p.set_defaults(func=cmd_web)

    sub.add_parser("worker", help="run the single capture worker for this desktop").set_defaults(func=cmd_worker)

    p = sub.add_parser("migrate", help="apply database migrations")
    p.add_argument("--revision", default="head")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("create-admin", help="create or reset an admin user")
    p.add_argument("--username", required=True)
    p.add_argument("--password-env", help="read the password from this environment variable")
    p.add_argument("--language", default="en", choices=["en", "zh"])
    p.set_defaults(func=cmd_create_admin)

    sub.add_parser("check-config", help="print a redacted configuration and environment report").set_defaults(func=cmd_check)
    sub.add_parser("discover-display", help="list X server sessions").set_defaults(func=cmd_discover)

    p = sub.add_parser("capture-once", help="save one screenshot (no analysis)")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_capture_once)

    p = sub.add_parser("analyze-image", help="run calibration + OCR on a saved screenshot")
    p.add_argument("image")
    p.add_argument("--profile", required=True)
    p.add_argument("--ocr", default="rapidocr", choices=["rapidocr", "tesseract"])
    p.add_argument("--pane", action="store_true", help="also analyze the conversation pane")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("benchmark", help="time analysis on saved screenshots; prints timings and agreement only")
    p.add_argument("image", nargs="+", help="one or more screenshots, e.g. before/after typing")
    p.add_argument("--profile", required=True)
    p.add_argument("--ocr", default="rapidocr", choices=["rapidocr", "tesseract"])
    p.add_argument("--repeat", type=int, default=2)
    p.add_argument("--processes", action="store_true", help="use an OCR process like the worker (enables pinning)")
    p.add_argument("--cpus", default=None, help="OCR CPU affinity: auto, none or a list such as 4-7")
    p.add_argument("--threads", type=int, default=None, help="ONNX Runtime intra-op threads (0 = auto)")
    p.add_argument("--reference", help="reply image file to time the outgoing-image matcher")
    p.set_defaults(func=cmd_benchmark)

    p = sub.add_parser("import-baseline", help="import historical customer names (one per line)")
    p.add_argument("file")
    p.set_defaults(func=cmd_import_baseline)

    p = sub.add_parser("import-rules", help="import keyword rules from JSON (imported disabled)")
    p.add_argument("file")
    p.add_argument("--keep-enabled", action="store_true")
    p.set_defaults(func=cmd_import_rules)

    p = sub.add_parser("load-calibration", help="store a calibration JSON as the active profile")
    p.add_argument("file")
    p.set_defaults(func=cmd_load_calibration)

    p = sub.add_parser("smtp-test", help="test SMTP connectivity (optionally send a test email)")
    p.add_argument("--send", action="store_true")
    p.set_defaults(func=cmd_smtp_test)

    p = sub.add_parser("export-diagnostics", help="write a redacted diagnostics bundle")
    p.add_argument("--out", required=True)
    p.add_argument("--frames", action="store_true", help="include masked frames")
    p.set_defaults(func=cmd_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    with contextlib.suppress(BrokenPipeError):
        return int(args.func(args) or 0)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
