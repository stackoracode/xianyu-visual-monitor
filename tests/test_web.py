"""Admin UI: authentication, CSRF, i18n rendering, guarded controls and admin workflows."""

import io
import json
import re
import zipfile

import httpx
import pytest
from sqlalchemy import select
from synth import Harness

from xianyu_monitor import state_keys
from xianyu_monitor.db import get_state, set_state
from xianyu_monitor.models import AdminUser, CalibrationProfile, Customer, Job, KeywordRule, ReviewItem
from xianyu_monitor.runtime_settings import load_runtime
from xianyu_monitor.web.app import create_app
from xianyu_monitor.web.security import hash_password, verify_password
from xianyu_monitor.web.services import create_customer

PASSWORD = "correct horse battery"
PAGES = ["/", "/calibration", "/customers", "/review", "/observations", "/notifications", "/rules", "/replies",
         "/settings", "/diagnostics"]


@pytest.fixture
async def client(db, settings):
    async with db.transaction() as s:
        s.add(AdminUser(username="owner", password_hash=hash_password(PASSWORD), language="en"))
    app = create_app(settings, db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def csrf_of(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "csrf token missing"
    return match.group(1)


async def login(client) -> str:
    page = await client.get("/login")
    token = csrf_of(page.text)
    resp = await client.post("/login", data={"username": "owner", "password": PASSWORD, "csrf_token": token, "next": "/"})
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    return csrf_of((await client.get("/")).text)


def test_password_hashing():
    encoded = hash_password(PASSWORD)
    assert verify_password(PASSWORD, encoded) and not verify_password("wrong password!", encoded)
    with pytest.raises(ValueError):
        hash_password("short")


async def test_pages_require_login(client):
    for path in PAGES:
        resp = await client.get(path)
        assert resp.status_code == 303 and resp.headers["location"].startswith("/login")
    assert (await client.get("/api/status")).status_code == 401


async def test_healthz_is_public_and_minimal(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200 and set(resp.json()) == {"web", "worker"}


async def test_all_pages_render_in_english_and_chinese(client, db):
    await login(client)
    async with db.transaction() as s:
        await create_customer(s, "买家甲", "baseline")
    for path in PAGES:
        resp = await client.get(path)
        assert resp.status_code == 200, path
        assert "Xianyu Visual Monitor" in resp.text
        assert resp.headers["content-security-policy"].startswith("default-src 'self'")
    await client.get("/lang/zh?next=/")
    for path in PAGES:
        resp = await client.get(path)
        assert resp.status_code == 200 and "闲鱼视觉监控" in resp.text, path
    assert "买家甲" in (await client.get("/customers")).text


async def test_login_failure_and_lockout(client, db):
    token = csrf_of((await client.get("/login")).text)
    for _ in range(5):
        await client.post("/login", data={"username": "owner", "password": "nope-nope-nope", "csrf_token": token})
    resp = await client.post("/login", data={"username": "owner", "password": PASSWORD, "csrf_token": token})
    assert resp.headers["location"] == "/login"  # locked even with the right password
    async with db.session() as s:
        user = (await s.execute(select(AdminUser))).scalar_one()
    assert user.locked_until is not None


async def test_csrf_is_enforced(client):
    await login(client)
    resp = await client.post("/control", data={"action": "pause_monitor", "csrf_token": "forged"})
    assert resp.status_code == 403


async def test_guarded_controls_need_confirmation(client, db):
    token = await login(client)
    await client.post("/control", data={"action": "enable_reply_send", "csrf_token": token})
    async with db.session() as s:
        assert (await load_runtime(s)).reply_send_enabled is False
    await client.post("/control", data={"action": "enable_reply_send", "csrf_token": token, "confirm": "ENABLE"})
    await client.post("/control", data={"action": "pause_replies", "csrf_token": token})
    async with db.session() as s:
        assert (await load_runtime(s)).reply_send_enabled is True
        assert (await get_state(s, state_keys.REPLY_PAUSE))["paused"] is True


async def test_settings_validation_and_guarded_keys_ignored(client, db):
    token = await login(client)
    await client.post("/settings", data={"csrf_token": token, "capture_interval_seconds": "0.01"})
    async with db.session() as s:
        assert (await load_runtime(s)).capture_interval_seconds == 6.0
    await client.post("/settings", data={"csrf_token": token, "capture_interval_seconds": "8",
                                         "reply_send_enabled": "1", "dry_run": "0", "alert_emails_enabled": "1"})
    async with db.session() as s:
        rt = await load_runtime(s)
    assert rt.capture_interval_seconds == 8.0 and rt.reply_send_enabled is False and rt.dry_run is True


async def test_calibration_save_validates_geometry(client, db):
    token = await login(client)
    bad = Harness().profile_dict | {"conversation_list": [0, 0, 5000, 10]}
    await client.post("/calibration", data={"csrf_token": token, "profile": json.dumps(bad)})
    async with db.session() as s:
        assert (await s.execute(select(CalibrationProfile))).scalars().all() == []
    await client.post("/calibration", data={"csrf_token": token, "profile": json.dumps(Harness().profile_dict)})
    async with db.session() as s:
        assert (await s.execute(select(CalibrationProfile.active))).scalar_one() is True


async def test_rules_crud_and_tester(client, db):
    token = await login(client)
    await client.post("/rules", data={"csrf_token": token, "name": "avail", "priority": "100", "match_type": "contains",
                                      "match_mode": "any", "patterns": "还在吗\n在吗", "template": "在的",
                                      "cooldown_seconds": "60", "scope": "all"})
    async with db.session() as s:
        rule = (await s.execute(select(KeywordRule))).scalar_one()
    assert rule.enabled is False and rule.patterns == ["还在吗", "在吗"]
    resp = await client.post("/rules/test", data={"csrf_token": token, "text": "还在吗", "customer_status": "known",
                                                  "include_disabled": "1"})
    assert "在的" in resp.text
    bad = await client.post("/rules", data={"csrf_token": token, "name": "x", "patterns": "a", "template": "{0}"})
    assert bad.status_code == 303


async def test_review_resolution_link_and_new_customer_notification(client, db):
    token = await login(client)
    async with db.transaction() as s:
        existing = await create_customer(s, "老客户", "baseline")
        s.add(ReviewItem(kind="identity_uncertain", status="open", dedup_key="rv:1", observed_name="老客戶",
                         observed_preview="hi", reasons=["similar_name_possible_ocr_error_or_rename"],
                         candidates=[existing.id]))
        s.add(ReviewItem(kind="new_without_incoming_evidence", status="open", dedup_key="rv:2", observed_name="新人",
                         observed_preview="hello", reasons=["new_without_incoming_evidence"], candidates=[]))
    async with db.session() as s:
        items = {i.dedup_key: i.id for i in (await s.execute(select(ReviewItem))).scalars()}
    await client.post(f"/review/{items['rv:1']}/resolve", data={"csrf_token": token, "action": "link",
                                                                 "customer_id": existing.id})
    await client.post(f"/review/{items['rv:2']}/resolve", data={"csrf_token": token, "action": "new_customer"})
    async with db.session() as s:
        statuses = {i.dedup_key: i.status for i in (await s.execute(select(ReviewItem))).scalars()}
        jobs = (await s.execute(select(Job))).scalars().all()
        new = (await s.execute(select(Customer).where(Customer.display_name == "新人"))).scalar_one()
    assert statuses == {"rv:1": "resolved", "rv:2": "resolved"}
    assert [j.dedup_key for j in jobs] == [f"new_customer:{new.id}"]
    # asking again does not create a second notification job
    await client.post(f"/customers/{new.id}/action", data={"csrf_token": token, "action": "notify"})
    async with db.session() as s:
        assert len((await s.execute(select(Job))).scalars().all()) == 1


async def test_merge_customers(client, db):
    token = await login(client)
    async with db.transaction() as s:
        a = await create_customer(s, "旧名", "baseline")
        b = await create_customer(s, "新名", "new")
    await client.post(f"/customers/{b.id}/action", data={"csrf_token": token, "action": "merge", "target_id": a.id})
    async with db.session() as s:
        merged = await s.get(Customer, b.id)
    assert merged.status == "merged" and merged.merged_into == a.id
    detail = await client.get(f"/customers/{a.id}")
    assert "新名" in detail.text and "旧名" in detail.text


async def test_diagnostics_export_is_redacted(client, db, settings):
    await login(client)
    async with db.transaction() as s:
        await create_customer(s, "隐私名字", "baseline")
        await set_state(s, state_keys.HEALTH, {"status": "ok", "note": "owner@example.com"})
    resp = await client.get("/diagnostics/export.zip")
    assert resp.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(resp.content))
    blob = "".join(archive.read(name).decode("utf-8", "replace") for name in archive.namelist())
    assert "隐私名字" not in blob and "owner@example.com" not in blob
    assert "x" * 48 not in blob  # secret key
    assert "manifest.json" in archive.namelist()


async def test_logout(client):
    token = await login(client)
    await client.post("/logout", data={"csrf_token": token})
    assert (await client.get("/")).status_code == 303
