"""Redacted diagnostics / review bundle export.

The bundle is suitable for sharing with a developer or an optional offline reviewer. By
default it contains no screenshots, no customer names and no message text: names are replaced
with stable pseudonyms and previews are reduced to length/flags. Frames can be included only
with name, preview, avatar and conversation-pane regions masked.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections import deque
from pathlib import Path

from PIL import Image, ImageDraw
from sqlalchemy import func, select

from xianyu_monitor import __version__
from xianyu_monitor.db import Database, all_states
from xianyu_monitor.models import AuditLog, CalibrationProfile, Frame, Job, MessageEvent, ReviewItem
from xianyu_monitor.redact import pseudonym, redact_mapping, redact_text
from xianyu_monitor.runtime_settings import load_runtime
from xianyu_monitor.settings import Settings
from xianyu_monitor.timeutil import iso, utcnow
from xianyu_monitor.vision.profile import Profile


def mask_frame(image: Image.Image, profile: Profile) -> Image.Image:
    """Allow-list masking: only unread badges and layout anchors survive, plus row outlines.
    Browser tabs, URL bar, account header, avatars, names, previews, item thumbnails and the
    conversation pane are not included."""
    source = image.convert("RGB")
    masked = Image.new("RGB", source.size, (40, 40, 40))
    keep = [a.region for a in profile.anchors]
    if profile.overall_unread_badge is not None:
        keep.append(profile.overall_unread_badge)
    keep += [profile.field_box(i, "badge") for i in range(profile.row.count)]
    for box in keep:
        masked.paste(source.crop(box), box[:2])
    draw = ImageDraw.Draw(masked)
    for index in range(profile.row.count):
        draw.rectangle(profile.row_box(index), outline=(90, 90, 90))
    return masked


def _tail(path: Path, lines: int = 400) -> list[str]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        return [redact_text(line.rstrip("\n")) for line in deque(fh, maxlen=lines)]


async def build_bundle(db: Database, settings: Settings, include_frames: bool = False, max_frames: int = 10) -> bytes:
    buf = io.BytesIO()
    async with db.session() as session:
        rt = await load_runtime(session)
        states = await all_states(session)
        profile_row = (await session.execute(select(CalibrationProfile).where(CalibrationProfile.active.is_(True))
                                             .order_by(CalibrationProfile.id.desc()).limit(1))).scalar_one_or_none()
        frames = (await session.execute(select(Frame).order_by(Frame.id.desc()).limit(200))).scalars().all()
        events = (await session.execute(select(MessageEvent).order_by(MessageEvent.id.desc()).limit(200))).scalars().all()
        reviews = (await session.execute(select(ReviewItem).order_by(ReviewItem.id.desc()).limit(200))).scalars().all()
        job_counts = (await session.execute(select(Job.kind, Job.status, func.count()).group_by(Job.kind, Job.status))).all()
        audit_rows = (await session.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(200))).scalars().all()

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps({
            "generated_at": iso(utcnow()), "version": __version__, "frames_included": include_frames,
            "note": "Redacted diagnostics. Names are pseudonyms; message text is omitted.",
        }, indent=2))
        zf.writestr("settings/static.json", json.dumps(settings.redacted(), indent=2, ensure_ascii=False))
        runtime = rt.as_dict()
        runtime["ignored_customer_names"] = [pseudonym(n) for n in runtime.get("ignored_customer_names", [])]
        zf.writestr("settings/runtime.json", json.dumps(runtime, indent=2, ensure_ascii=False))
        zf.writestr("state.json", json.dumps(redact_mapping(states), indent=2, ensure_ascii=False, default=str))
        if profile_row is not None:
            zf.writestr("calibration.json", json.dumps(profile_row.data, indent=2, ensure_ascii=False))
        zf.writestr("frames.json", json.dumps([{
            "id": f.id, "captured_at": iso(f.captured_at), "status": f.status, "changed": f.changed,
            "ocr": f.ocr_performed, "capture_ms": round(f.capture_ms, 1), "ocr_ms": round(f.ocr_ms, 1),
            "total_ms": round(f.total_ms, 1), "rows_seen": f.rows_seen, "overall_unread": f.overall_unread,
            "summary": redact_mapping(f.summary or {}),
        } for f in frames], indent=2))
        zf.writestr("events.json", json.dumps([{
            "id": e.id, "customer": pseudonym(e.customer_id, "cust"), "kind": e.kind, "direction": e.direction,
            "preview_len": len(e.preview_raw or ""), "truncated": e.preview_truncated,
            "badge_before": e.badge_before, "badge_after": e.badge_after, "signals": e.signals,
            "confidence": e.confidence, "captured_at": iso(e.captured_at),
            "detect_latency_ms": round((e.created_at - e.captured_at).total_seconds() * 1000.0, 1),
        } for e in events], indent=2))
        zf.writestr("reviews.json", json.dumps([{
            "id": r.id, "kind": r.kind, "status": r.status, "name": pseudonym(r.observed_name),
            "reasons": r.reasons, "seen_count": r.seen_count, "created_at": iso(r.created_at),
        } for r in reviews], indent=2))
        zf.writestr("jobs.json", json.dumps([{"kind": k, "status": s, "count": int(c)} for k, s, c in job_counts], indent=2))
        zf.writestr("audit.json", json.dumps([{
            "at": iso(a.at), "actor": pseudonym(a.actor, "user"), "action": a.action,
        } for a in audit_rows], indent=2))
        zf.writestr("logs/worker.log", "\n".join(_tail(settings.logs_dir / "worker.log")))
        if include_frames and profile_row is not None:
            profile = Profile.model_validate(profile_row.data)
            paths = [Path(f.image_path) for f in frames if f.image_path][:max_frames]
            for path in paths:
                if not path.is_file():
                    continue
                with Image.open(path) as image:
                    if image.size != (profile.frame_width, profile.frame_height):
                        continue  # cannot mask reliably with a mismatched profile
                    out = io.BytesIO()
                    mask_frame(image, profile).save(out, format="PNG")
                zf.writestr(f"frames/{path.name}", out.getvalue())
    return buf.getvalue()
