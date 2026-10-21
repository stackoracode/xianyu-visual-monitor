"""FastAPI admin application (server-rendered, session-authenticated, CSRF-protected).

The web app never captures the screen, runs OCR or sends input; it reads/writes MySQL and the
latest diagnostic frame file. It can therefore run as an unprivileged user with several
uvicorn workers while exactly one capture worker runs per desktop.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import io
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageDraw
from sqlalchemy import desc, select
from starlette.middleware.sessions import SessionMiddleware

from xianyu_monitor import __version__, state_keys
from xianyu_monitor.db import Database, all_states, get_state
from xianyu_monitor.detection.textnorm import normalize_name
from xianyu_monitor.diagnostics import build_bundle
from xianyu_monitor.i18n import SUPPORTED, Translator, normalize_lang
from xianyu_monitor.models import (
    AdminUser,
    AuditLog,
    CalibrationProfile,
    ConversationState,
    Customer,
    CustomerAlias,
    CustomerAvatar,
    Frame,
    Job,
    KeywordRule,
    MessageEvent,
    ReplyIntent,
    ReviewItem,
)
from xianyu_monitor.notify.emailer import (
    SmtpConfig,
    SmtpConfigError,
    SmtpPermanentError,
    SmtpSender,
    SmtpTransientError,
    SmtpUncertainError,
    build_message,
)
from xianyu_monitor.queueing.jobs import admin_cancel, admin_resolve, admin_retry
from xianyu_monitor.replies.planner import rule_from_row
from xianyu_monitor.replies.rules import Rule, RuleError, evaluate, validate_rule
from xianyu_monitor.runtime_settings import GROUPS, SPECS, SettingError, load_runtime, save_runtime
from xianyu_monitor.settings import Settings
from xianyu_monitor.timeutil import iso, utcnow
from xianyu_monitor.vision import imageops
from xianyu_monitor.vision.profile import ROW_FIELDS, Profile
from xianyu_monitor.web import services
from xianyu_monitor.web.security import (
    MAX_FAILED_LOGINS,
    csrf_matches,
    hash_password,
    is_locked,
    lock_deadline,
    new_csrf_token,
    verify_password,
)

log = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
EXAMPLES_DIR = HERE.parent / "examples"
LANG_COOKIE = "xvm_lang"
# Settings that change safety posture are only changed through explicit, confirmed controls.
GUARDED_SETTINGS = {"reply_send_enabled", "dry_run", "monitor_mode", "notifications_enabled", "reply_mode"}
CONFIRM_WORD = "ENABLE"
PAGE_SIZE = 100


class LoginRequired(Exception):
    pass


class CsrfFailed(Exception):
    pass


def create_app(settings: Settings, db: Database | None = None) -> FastAPI:
    if not settings.secret_key or len(settings.secret_key) < 32:
        raise RuntimeError("XVM_SECRET_KEY must be set to a random value of at least 32 characters")
    database = db or Database(settings.database_url)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await database.dispose()

    app = FastAPI(title="xianyu-visual-monitor", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.db = database
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals["version"] = __version__
    templates.env.filters["iso"] = iso
    templates.env.filters["tojson_pretty"] = lambda v: json.dumps(v, indent=2, ensure_ascii=False, default=str)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="xvm_session",
                       max_age=settings.session_max_age_seconds, same_site="strict",
                       https_only=settings.session_cookie_secure)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy",
                                    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                                    "script-src 'self'; frame-ancestors 'none'; form-action 'self'")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "authentication required"}, status_code=401)
        return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)

    @app.exception_handler(CsrfFailed)
    async def _csrf_failed(request: Request, exc: CsrfFailed):
        return HTMLResponse("CSRF validation failed. Reload the page and try again.", status_code=403)

    # ----- helpers -----
    def lang_of(request: Request) -> str:
        return normalize_lang(request.cookies.get(LANG_COOKIE) or request.session.get("lang")
                              or settings.default_language)

    async def current_user(request: Request) -> AdminUser:
        uid = request.session.get("uid")
        if not uid:
            raise LoginRequired()
        async with database.session() as session:
            user = await session.get(AdminUser, uid)
        if user is None or request.session.get("pw") != user.password_hash[-16:]:
            request.session.clear()
            raise LoginRequired()
        return user

    def csrf_token(request: Request) -> str:
        token = request.session.get("csrf")
        if not token:
            token = new_csrf_token()
            request.session["csrf"] = token
        return token

    async def form_data(request: Request) -> dict[str, Any]:
        form = await request.form()
        if not csrf_matches(request.session.get("csrf"), form.get("csrf_token")):  # type: ignore[arg-type]
            raise CsrfFailed()
        data: dict[str, Any] = {}
        for key in form.keys():
            values = form.getlist(key)
            data[key] = values if len(values) > 1 else values[0]
        return data

    def flash(request: Request, message: str, level: str = "info") -> None:
        request.session.setdefault("flash", []).append({"message": message, "level": level})

    def back(request: Request, default: str = "/") -> RedirectResponse:
        target = request.headers.get("referer") or default
        if "://" in target:
            host = request.headers.get("host", "")
            if f"://{host}" not in target:
                target = default
        return RedirectResponse(target, status_code=303)

    async def banner_state() -> dict:
        async with database.session() as session:
            states = await all_states(session)
            rt = await load_runtime(session)
        heartbeat = states.get(state_keys.HEARTBEAT, {})
        fresh = False
        if heartbeat.get("at"):
            try:
                at = dt.datetime.fromisoformat(heartbeat["at"].rstrip("Z"))
                fresh = (utcnow() - at).total_seconds() < 60 and heartbeat.get("status") != "stopped"
            except ValueError:
                fresh = False
        return {"states": states, "rt": rt, "worker_fresh": fresh}

    async def render(request: Request, name: str, user: AdminUser | None, **context: Any) -> HTMLResponse:
        lang = lang_of(request)
        banner = await banner_state() if user is not None else {}
        flashes = request.session.pop("flash", [])
        return templates.TemplateResponse(request, name, {
            "t": Translator(lang), "lang": lang, "languages": SUPPORTED, "user": user,
            "csrf": csrf_token(request), "flashes": flashes, "banner": banner, **context})

    # ----- public -----
    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        try:
            banner = await banner_state()
            worker = "fresh" if banner["worker_fresh"] else "stale"
            return JSONResponse({"web": "ok", "worker": worker})
        except Exception:  # noqa: BLE001
            return JSONResponse({"web": "ok", "worker": "unknown", "database": "error"}, status_code=503)

    @app.get("/lang/{code}")
    async def set_lang(request: Request, code: str, next: str = "/"):
        lang = normalize_lang(code)
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(LANG_COOKIE, lang, max_age=365 * 86400, samesite="strict", httponly=True)
        request.session["lang"] = lang
        uid = request.session.get("uid")
        if uid:
            async with database.transaction() as session:
                user = await session.get(AdminUser, uid)
                if user is not None:
                    user.language = lang
        return response

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "/"):
        return await render(request, "login.html", None, next=next)

    @app.post("/login")
    async def login(request: Request):
        data = await form_data(request)
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        target = str(data.get("next") or "/")
        if not target.startswith("/") or target.startswith("//"):
            target = "/"
        async with database.transaction() as session:
            user = (await session.execute(select(AdminUser).where(AdminUser.username == username))).scalar_one_or_none()
            ok = False
            if user is not None and not is_locked(user.locked_until):
                ok = verify_password(password, user.password_hash)
                if ok:
                    user.failed_logins = 0
                    user.locked_until = None
                    user.last_login_at = utcnow()
                else:
                    user.failed_logins += 1
                    if user.failed_logins >= MAX_FAILED_LOGINS:
                        user.locked_until = lock_deadline()
                        user.failed_logins = 0
            elif user is None:
                await asyncio.sleep(0.2)
        if not ok:
            log.warning("failed admin login")
            flash(request, Translator(lang_of(request))("login.failed"), "error")
            return RedirectResponse("/login", status_code=303)
        request.session.clear()
        request.session["uid"] = user.id
        request.session["pw"] = user.password_hash[-16:]
        request.session["csrf"] = new_csrf_token()
        request.session["lang"] = user.language
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(LANG_COOKIE, normalize_lang(user.language), max_age=365 * 86400, samesite="strict",
                            httponly=True)
        return response

    @app.post("/logout")
    async def logout(request: Request):
        await form_data(request)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ----- overview & controls -----
    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request):
        user = await current_user(request)
        async with database.session() as session:
            counts = await services.dashboard_counts(session)
            frames = (await session.execute(select(Frame).order_by(desc(Frame.id)).limit(10))).scalars().all()
        return await render(request, "overview.html", user, counts=counts, frames=frames, confirm_word=CONFIRM_WORD)

    @app.post("/control")
    async def control(request: Request):
        user = await current_user(request)
        data = await form_data(request)
        action = str(data.get("action", ""))
        t = Translator(lang_of(request))
        confirmed = str(data.get("confirm", "")).strip() == CONFIRM_WORD
        needs_confirm = {"disable_dry_run", "enable_reply_send", "reply_mode_auto", "enable_notifications",
                         "mode_active"}
        if action in needs_confirm and not confirmed:
            flash(request, t("control.confirm_required", word=CONFIRM_WORD), "error")
            return back(request)
        changes: dict[str, Any] = {
            "mode_baseline": {"monitor_mode": "baseline"}, "mode_active": {"monitor_mode": "active"},
            "enable_notifications": {"notifications_enabled": True},
            "disable_notifications": {"notifications_enabled": False},
            "enable_dry_run": {"dry_run": True}, "disable_dry_run": {"dry_run": False},
            "reply_mode_off": {"reply_mode": "off"}, "reply_mode_review": {"reply_mode": "review"},
            "reply_mode_auto": {"reply_mode": "auto"},
            "enable_reply_send": {"reply_send_enabled": True}, "disable_reply_send": {"reply_send_enabled": False},
        }.get(action, {})
        async with database.transaction() as session:
            if action in {"pause_monitor", "resume_monitor"}:
                await services.set_pause(session, state_keys.MONITOR_PAUSE, action == "pause_monitor", user.username)
            elif action in {"pause_replies", "resume_replies"}:
                await services.set_pause(session, state_keys.REPLY_PAUSE, action == "pause_replies", user.username)
                if action == "resume_replies":
                    challenge = await get_state(session, state_keys.CHALLENGE)
                    if challenge.get("active"):
                        flash(request, t("control.challenge_still_active"), "error")
            elif changes:
                await save_runtime(session, changes, user.username)
                await services.audit(session, user.username, "control", {"action": action})
            else:
                flash(request, t("common.unknown_action"), "error")
                return back(request)
        flash(request, t("common.saved"))
        return back(request)

    # ----- calibration -----
    async def latest_frame() -> Image.Image | None:
        path = settings.frames_dir / "latest.png"
        if not path.is_file():
            return None
        with Image.open(path) as image:
            image.load()
            return image.convert("RGB")

    async def active_profile(session) -> CalibrationProfile | None:
        return (await session.execute(select(CalibrationProfile).where(CalibrationProfile.active.is_(True))
                                      .order_by(desc(CalibrationProfile.id)).limit(1))).scalar_one_or_none()

    @app.get("/calibration", response_class=HTMLResponse)
    async def calibration(request: Request):
        user = await current_user(request)
        async with database.session() as session:
            active = await active_profile(session)
            history = (await session.execute(select(CalibrationProfile).order_by(desc(CalibrationProfile.id))
                                             .limit(20))).scalars().all()
        text = json.dumps(active.data, indent=2, ensure_ascii=False) if active else ""
        if not text:
            example = EXAMPLES_DIR / "calibration.example.json"
            text = example.read_text(encoding="utf-8") if example.is_file() else "{}"
        frame_path = settings.frames_dir / "latest.png"
        return await render(request, "calibration.html", user, active=active, history=history, profile_text=text,
                            has_frame=frame_path.is_file())

    async def save_profile(session, data: dict, actor: str) -> CalibrationProfile:
        profile = Profile.model_validate(data)
        for row in (await session.execute(select(CalibrationProfile).where(CalibrationProfile.active.is_(True)))).scalars():
            row.active = False
        row = CalibrationProfile(name=profile.name, data=profile.model_dump(mode="json"), active=True,
                                 created_at=utcnow(), created_by=actor)
        session.add(row)
        await services.audit(session, actor, "calibration_save", {"name": profile.name})
        return row

    @app.post("/calibration")
    async def calibration_save(request: Request):
        user = await current_user(request)
        data = await form_data(request)
        t = Translator(lang_of(request))
        try:
            parsed = json.loads(str(data.get("profile", "")))
            async with database.transaction() as session:
                await save_profile(session, parsed, user.username)
            flash(request, t("calibration.saved"))
        except (ValueError, TypeError) as exc:
            flash(request, f"{t('calibration.invalid')}: {exc}"[:800], "error")
        return RedirectResponse("/calibration", status_code=303)

    @app.post("/calibration/anchors")
    async def calibration_anchors(request: Request):
        user = await current_user(request)
        await form_data(request)
        t = Translator(lang_of(request))
        frame = await latest_frame()
        async with database.transaction() as session:
            active = await active_profile(session)
            if active is None or frame is None:
                flash(request, t("calibration.need_profile_and_frame"), "error")
                return RedirectResponse("/calibration", status_code=303)
            profile = Profile.model_validate(active.data)
            if frame.size != (profile.frame_width, profile.frame_height):
                flash(request, t("calibration.size_mismatch"), "error")
                return RedirectResponse("/calibration", status_code=303)
            data = profile.model_dump(mode="json")
            for anchor in data["anchors"]:
                region = imageops.crop(frame, tuple(anchor["region"]))
                anchor["dhash"] = imageops.dhash(region)
                anchor["mean"] = round(imageops.mean_gray(region), 1)
            await save_profile(session, data, user.username)
        flash(request, t("calibration.anchors_updated"))
        return RedirectResponse("/calibration", status_code=303)

    @app.post("/calibration/{profile_id}/activate")
    async def calibration_activate(request: Request, profile_id: int):
        user = await current_user(request)
        await form_data(request)
        async with database.transaction() as session:
            target = await session.get(CalibrationProfile, profile_id)
            if target is not None:
                for row in (await session.execute(select(CalibrationProfile)
                                                  .where(CalibrationProfile.active.is_(True)))).scalars():
                    row.active = False
                target.active = True
                await services.audit(session, user.username, "calibration_activate", {"id": profile_id})
        return RedirectResponse("/calibration", status_code=303)

    @app.get("/calibration/overlay.png")
    async def calibration_overlay(request: Request, masked: int = 0):
        await current_user(request)
        frame = await latest_frame()
        if frame is None:
            return Response(status_code=404)
        async with database.session() as session:
            active = await active_profile(session)
        image = frame
        if active is not None:
            try:
                profile = Profile.model_validate(active.data)
            except ValueError:
                profile = None
            if profile is not None and frame.size == (profile.frame_width, profile.frame_height):
                if masked:
                    from xianyu_monitor.diagnostics import mask_frame
                    image = mask_frame(frame, profile)
                draw = ImageDraw.Draw(image)
                draw.rectangle(profile.conversation_list, outline=(0, 120, 255), width=2)
                colors = {"avatar": (160, 0, 200), "badge": (255, 0, 0), "name": (0, 160, 0),
                          "preview": (0, 120, 255), "time": (255, 140, 0)}
                for index in range(profile.row.count):
                    draw.rectangle(profile.row_box(index), outline=(120, 120, 120))
                    for field_name in ROW_FIELDS:
                        draw.rectangle(profile.field_box(index, field_name), outline=colors[field_name])
                for box in (profile.overall_unread_badge, profile.conversation_title, profile.message_pane,
                            profile.composer, profile.challenge_probe):
                    if box is not None:
                        draw.rectangle(box, outline=(255, 0, 255))
                for anchor in profile.anchors:
                    draw.rectangle(anchor.region, outline=(0, 200, 200), width=2)
        out = io.BytesIO()
        image.save(out, format="PNG")
        return Response(out.getvalue(), media_type="image/png")

    # ----- customers -----
    @app.get("/customers", response_class=HTMLResponse)
    async def customers(request: Request, status: str = "", q: str = "", offset: int = 0):
        user = await current_user(request)
        async with database.session() as session:
            query = select(Customer).order_by(desc(Customer.last_observed_at), Customer.display_name)
            if status:
                query = query.where(Customer.status == status)
            else:
                query = query.where(Customer.status != "merged")
            if q:
                norm = normalize_name(q)
                ids = select(CustomerAlias.customer_id).where(CustomerAlias.alias_norm.contains(norm))
                query = query.where(Customer.id.in_(ids))
            rows = (await session.execute(query.offset(max(0, offset)).limit(PAGE_SIZE))).scalars().all()
        return await render(request, "customers.html", user, customers=rows, status=status, q=q, offset=offset,
                            page_size=PAGE_SIZE, statuses=services.CUSTOMER_STATUSES + ("merged",))

    @app.get("/customers/{customer_id}", response_class=HTMLResponse)
    async def customer_detail(request: Request, customer_id: str):
        user = await current_user(request)
        async with database.session() as session:
            customer = await session.get(Customer, customer_id)
            if customer is None:
                return HTMLResponse("not found", status_code=404)
            aliases = (await session.execute(select(CustomerAlias).where(CustomerAlias.customer_id == customer_id))).scalars().all()
            avatars = (await session.execute(select(CustomerAvatar).where(CustomerAvatar.customer_id == customer_id))).scalars().all()
            state = await session.get(ConversationState, customer_id)
            events = (await session.execute(select(MessageEvent).where(MessageEvent.customer_id == customer_id)
                                            .order_by(desc(MessageEvent.id)).limit(50))).scalars().all()
            jobs = (await session.execute(select(Job).where(Job.dedup_key == f"new_customer:{customer_id}"))).scalars().all()
            intents = (await session.execute(select(ReplyIntent).where(ReplyIntent.customer_id == customer_id)
                                             .order_by(desc(ReplyIntent.id)).limit(50))).scalars().all()
        return await render(request, "customer_detail.html", user, customer=customer, aliases=aliases, avatars=avatars,
                            state=state, events=events, jobs=jobs, intents=intents,
                            statuses=services.CUSTOMER_STATUSES)

    @app.post("/customers/{customer_id}/action")
    async def customer_action(request: Request, customer_id: str):
        user = await current_user(request)
        data = await form_data(request)
        t = Translator(lang_of(request))
        action = data.get("action")
        try:
            async with database.transaction() as session:
                if action == "status":
                    await services.set_customer_status(session, customer_id, str(data.get("status")),
                                                       str(data.get("notes", "")), user.username)
                elif action == "add_alias":
                    await services.add_alias(session, customer_id, str(data.get("alias", "")), user.username)
                elif action == "remove_alias":
                    await services.remove_alias(session, int(data.get("alias_id", 0)), user.username)
                elif action == "merge":
                    await services.merge_customers(session, customer_id, str(data.get("target_id", "")).strip(),
                                                   user.username)
                elif action == "notify":
                    rt = await load_runtime(session)
                    _, created = await services.notify_customer(session, customer_id, rt, user.username)
                    if not created:
                        flash(request, t("customers.notify_exists"), "error")
                else:
                    raise services.AdminError("unknown action")
            flash(request, t("common.saved"))
        except (services.AdminError, ValueError) as exc:
            flash(request, str(exc), "error")
        return RedirectResponse(f"/customers/{customer_id}", status_code=303)

    @app.post("/customers/import")
    async def customers_import(request: Request):
        user = await current_user(request)
        data = await form_data(request)
        names = str(data.get("names", "")).replace("\r", "").split("\n")
        async with database.transaction() as session:
            result = await services.import_baseline(session, names, user.username)
        flash(request, Translator(lang_of(request))("customers.imported", **result))
        return RedirectResponse("/customers", status_code=303)

    # ----- review queue -----
    @app.get("/review", response_class=HTMLResponse)
    async def review(request: Request, status: str = "open"):
        user = await current_user(request)
        async with database.session() as session:
            items = (await session.execute(select(ReviewItem).where(ReviewItem.status == status)
                                           .order_by(desc(ReviewItem.last_seen_at)).limit(PAGE_SIZE))).scalars().all()
            ids = {cid for item in items for cid in (item.candidates or [])} | {i.customer_id for i in items if i.customer_id}
            names = {}
            if ids:
                rows = (await session.execute(select(Customer.id, Customer.display_name).where(Customer.id.in_(ids)))).all()
                names = {cid: name for cid, name in rows}
        return await render(request, "review.html", user, items=items, status=status, names=names)

    @app.post("/review/{item_id}/resolve")
    async def review_resolve(request: Request, item_id: int):
        user = await current_user(request)
        data = await form_data(request)
        try:
            async with database.transaction() as session:
                await services.resolve_review(session, item_id, str(data.get("action")), user.username,
                                              str(data.get("customer_id") or "").strip() or None)
            flash(request, Translator(lang_of(request))("common.saved"))
        except services.AdminError as exc:
            flash(request, str(exc), "error")
        return RedirectResponse("/review", status_code=303)

    # ----- observations -----
    @app.get("/observations", response_class=HTMLResponse)
    async def observations(request: Request, kind: str = ""):
        user = await current_user(request)
        async with database.session() as session:
            query = (select(MessageEvent, Customer.display_name)
                     .join(Customer, Customer.id == MessageEvent.customer_id).order_by(desc(MessageEvent.id)))
            if kind:
                query = query.where(MessageEvent.kind == kind)
            events = (await session.execute(query.limit(PAGE_SIZE * 2))).all()
            frames = (await session.execute(select(Frame).order_by(desc(Frame.id)).limit(50))).scalars().all()
        return await render(request, "observations.html", user, events=events, frames=frames, kind=kind)

    # ----- notifications -----
    @app.get("/notifications", response_class=HTMLResponse)
    async def notifications(request: Request, status: str = ""):
        user = await current_user(request)
        async with database.session() as session:
            query = select(Job).where(Job.kind == "email").order_by(desc(Job.id))
            if status:
                query = query.where(Job.status == status)
            jobs = (await session.execute(query.limit(PAGE_SIZE))).scalars().all()
        return await render(request, "notifications.html", user, jobs=jobs, status=status,
                            smtp_configured=settings.smtp_configured, recipients=len(settings.notify_to))

    @app.post("/jobs/{job_id}/action")
    async def job_action(request: Request, job_id: int):
        user = await current_user(request)
        data = await form_data(request)
        action = data.get("action")
        async with database.transaction() as session:
            if action == "retry":
                ok = await admin_retry(session, job_id, user.username)
            elif action == "cancel":
                ok = await admin_cancel(session, job_id)
            elif action in {"resolve_succeeded", "resolve_failed"}:
                ok = await admin_resolve(session, job_id, action.split("_", 1)[1])
            else:
                ok = False
            await services.audit(session, user.username, f"job_{action}", {"job": job_id, "ok": ok})
        t = Translator(lang_of(request))
        flash(request, t("common.saved") if ok else t("jobs.action_not_allowed"), "info" if ok else "error")
        return back(request, "/notifications")

    @app.post("/notifications/smtp-test")
    async def smtp_test(request: Request):
        user = await current_user(request)
        data = await form_data(request)
        t = Translator(lang_of(request))
        send = data.get("send") == "1"
        cfg = SmtpConfig.from_settings(settings)
        sender = SmtpSender(cfg)
        loop = asyncio.get_running_loop()

        def run() -> str:
            if not send:
                return sender.test_connection()
            if not settings.notify_to:
                raise SmtpConfigError("no recipient configured")
            conn = sender.connect()
            try:
                message = build_message(cfg, list(settings.notify_to), t("email.test.subject"),
                                        t("email.test.body"), f"smtp-test:{utcnow().isoformat()}")
                sender.submit(conn, message)
                return "accepted by SMTP server"
            finally:
                sender.close(conn)

        try:
            result = await loop.run_in_executor(None, run)
            flash(request, f"{t('notifications.smtp_ok')}: {result}")
            outcome = "ok"
        except (SmtpConfigError, SmtpTransientError, SmtpPermanentError, SmtpUncertainError) as exc:
            flash(request, f"{t('notifications.smtp_failed')}: {exc}", "error")
            outcome = type(exc).__name__
        async with database.transaction() as session:
            await services.audit(session, user.username, "smtp_test", {"send": send, "outcome": outcome})
        return RedirectResponse("/notifications", status_code=303)

    # ----- keyword rules -----
    def rule_from_form(data: dict, rule_id: int = 0) -> Rule:
        def lines(key: str) -> list[str]:
            return [s.strip() for s in str(data.get(key, "")).replace("\r", "").split("\n") if s.strip()]

        return Rule(id=rule_id, name=str(data.get("name", "")).strip()[:128] or "rule",
                    priority=int(data.get("priority", 100) or 100), match_type=str(data.get("match_type", "contains")),
                    match_mode=str(data.get("match_mode", "any")), patterns=lines("patterns"),
                    exclude_patterns=lines("exclude_patterns"), template=str(data.get("template", "")).strip(),
                    cooldown_seconds=max(0, int(data.get("cooldown_seconds", 3600) or 0)),
                    scope=str(data.get("scope", "all")), allow_sensitive=data.get("allow_sensitive") == "1",
                    requires_review=data.get("requires_review") == "1", enabled=data.get("enabled") == "1",
                    trigger_type=str(data.get("trigger_type", "keyword")), send_image=data.get("send_image") == "1")

    @app.get("/rules", response_class=HTMLResponse)
    async def rules(request: Request, edit: int = 0):
        user = await current_user(request)
        async with database.session() as session:
            rows = (await session.execute(select(KeywordRule).order_by(desc(KeywordRule.priority), KeywordRule.id))).scalars().all()
            editing = await session.get(KeywordRule, edit) if edit else None
        return await render(request, "rules.html", user, rules=rows, editing=editing, test=None)

    @app.post("/rules")
    async def rules_save(request: Request):
        user = await current_user(request)
        data = await form_data(request)
        t = Translator(lang_of(request))
        rule_id = int(data.get("id") or 0)
        try:
            rule = rule_from_form(data, rule_id)
            validate_rule(rule)
        except (RuleError, ValueError) as exc:
            flash(request, str(exc), "error")
            return RedirectResponse(f"/rules?edit={rule_id}" if rule_id else "/rules", status_code=303)
        async with database.transaction() as session:
            row = await session.get(KeywordRule, rule_id) if rule_id else None
            if row is None:
                row = KeywordRule(created_at=utcnow())
                session.add(row)
            for field_name in ("name", "priority", "match_type", "match_mode", "patterns", "exclude_patterns",
                               "template", "cooldown_seconds", "scope", "allow_sensitive", "requires_review", "enabled",
                               "trigger_type", "send_image"):
                setattr(row, field_name, getattr(rule, field_name))
            row.updated_at = utcnow()
            await services.audit(session, user.username, "rule_save", {"rule": rule_id or "new", "enabled": rule.enabled})
        flash(request, t("common.saved"))
        return RedirectResponse("/rules", status_code=303)

    @app.post("/rules/{rule_id}/delete")
    async def rules_delete(request: Request, rule_id: int):
        user = await current_user(request)
        await form_data(request)
        async with database.transaction() as session:
            row = await session.get(KeywordRule, rule_id)
            if row is not None:
                used = (await session.execute(select(ReplyIntent.id).where(ReplyIntent.rule_id == rule_id).limit(1))).first()
                if used:
                    row.enabled = False  # keep history; disable instead of deleting
                else:
                    await session.delete(row)
                await services.audit(session, user.username, "rule_delete", {"rule": rule_id, "disabled_only": bool(used)})
        return RedirectResponse("/rules", status_code=303)

    @app.post("/rules/test", response_class=HTMLResponse)
    async def rules_test(request: Request):
        user = await current_user(request)
        data = await form_data(request)
        text = str(data.get("text", ""))
        status = str(data.get("customer_status", "known"))
        async with database.session() as session:
            rows = (await session.execute(select(KeywordRule).order_by(desc(KeywordRule.priority)))).scalars().all()
            rt = await load_runtime(session)
        include_disabled = data.get("include_disabled") == "1"
        rules_list = [rule_from_row(r) for r in rows]
        if include_disabled:
            for r in rules_list:
                r.enabled = True
        decision = evaluate(text, rules_list, customer_status=status, customer_name="{customer_name}",
                            sensitive_terms=rt.sensitive_terms, attachment_markers=rt.attachment_markers,
                            negations=rt.negation_terms,
                            first_contact=status == "new" and data.get("first_contact") == "1")
        return await render(request, "rules.html", user, rules=rows, editing=None,
                            test={"text": text, "decision": decision, "status": status})

    # ----- replies -----
    @app.get("/replies", response_class=HTMLResponse)
    async def replies(request: Request, status: str = ""):
        user = await current_user(request)
        async with database.session() as session:
            query = (select(ReplyIntent, Customer.display_name).join(Customer, Customer.id == ReplyIntent.customer_id)
                     .order_by(desc(ReplyIntent.id)))
            if status:
                query = query.where(ReplyIntent.status == status)
            rows = (await session.execute(query.limit(PAGE_SIZE))).all()
        return await render(request, "replies.html", user, rows=rows, status=status)

    @app.post("/replies/{intent_id}/action")
    async def reply_action(request: Request, intent_id: int):
        user = await current_user(request)
        data = await form_data(request)
        try:
            async with database.transaction() as session:
                if data.get("action") == "approve":
                    status = await services.approve_intent(session, intent_id, user.username,
                                                           data.get("reply_text"))  # type: ignore[arg-type]
                    flash(request, Translator(lang_of(request))("replies.approved_status", status=status))
                elif data.get("action") == "reject":
                    await services.reject_intent(session, intent_id, user.username)
                    flash(request, Translator(lang_of(request))("common.saved"))
                else:
                    raise services.AdminError("unknown action")
        except services.AdminError as exc:
            flash(request, str(exc), "error")
        return RedirectResponse("/replies", status_code=303)

    # ----- settings -----
    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        user = await current_user(request)
        async with database.session() as session:
            rt = await load_runtime(session)
        specs = [s for s in SPECS if s.key not in GUARDED_SETTINGS]
        return await render(request, "settings.html", user, rt=rt.as_dict(), specs=specs, groups=GROUPS,
                            static=settings.redacted())

    @app.post("/settings")
    async def settings_save(request: Request):
        user = await current_user(request)
        data = await form_data(request)
        t = Translator(lang_of(request))
        changes = {}
        for spec in SPECS:
            if spec.key in GUARDED_SETTINGS:
                continue
            if spec.kind == "bool":
                changes[spec.key] = data.get(spec.key) == "1"
            elif spec.key in data:
                changes[spec.key] = data[spec.key]
        try:
            async with database.transaction() as session:
                applied = await save_runtime(session, changes, user.username)
                await services.audit(session, user.username, "settings_save", {"keys": sorted(applied)})
            flash(request, t("common.saved"))
        except SettingError as exc:
            flash(request, str(exc), "error")
        return RedirectResponse("/settings", status_code=303)

    @app.post("/account/password")
    async def change_password(request: Request):
        user = await current_user(request)
        data = await form_data(request)
        t = Translator(lang_of(request))
        if not verify_password(str(data.get("current", "")), user.password_hash):
            flash(request, t("account.wrong_password"), "error")
            return RedirectResponse("/settings", status_code=303)
        try:
            new_hash = hash_password(str(data.get("new", "")))
        except ValueError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse("/settings", status_code=303)
        async with database.transaction() as session:
            row = await session.get(AdminUser, user.id)
            assert row is not None
            row.password_hash = new_hash
            await services.audit(session, user.username, "password_change")
        request.session["pw"] = new_hash[-16:]
        flash(request, t("common.saved"))
        return RedirectResponse("/settings", status_code=303)

    # ----- diagnostics -----
    @app.get("/diagnostics", response_class=HTMLResponse)
    async def diagnostics(request: Request):
        user = await current_user(request)
        async with database.session() as session:
            audit_rows = (await session.execute(select(AuditLog).order_by(desc(AuditLog.id)).limit(50))).scalars().all()
            states = await all_states(session)
        return await render(request, "diagnostics.html", user, audit=audit_rows, states=states)

    @app.get("/diagnostics/export.zip")
    async def diagnostics_export(request: Request, frames: int = 0):
        user = await current_user(request)
        bundle = await build_bundle(database, settings, include_frames=bool(frames))
        async with database.transaction() as session:
            await services.audit(session, user.username, "diagnostics_export", {"frames": bool(frames)})
        name = f"xvm-diagnostics-{utcnow().strftime('%Y%m%dT%H%M%SZ')}.zip"
        return StreamingResponse(io.BytesIO(bundle), media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/status")
    async def api_status(request: Request):
        await current_user(request)
        async with database.session() as session:
            states = await all_states(session)
            counts = await services.dashboard_counts(session)
            rt = await load_runtime(session)
        return JSONResponse({"version": __version__, "states": states, "counts": counts,
                             "mode": rt.monitor_mode, "dry_run": rt.dry_run,
                             "notifications_enabled": rt.notifications_enabled, "reply_mode": rt.reply_mode,
                             "reply_send_enabled": rt.reply_send_enabled})

    return app
