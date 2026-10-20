"""End-to-end detection persistence on synthetic frames (SQLite locally, MySQL in CI)."""

import datetime as dt

from sqlalchemy import func, select

from synth import Harness, Scene, SRow
from xianyu_monitor.models import Customer, Job, KeywordRule, MessageEvent, ReplyIntent, ReviewItem
from xianyu_monitor.runtime_settings import RuntimeSettings
from xianyu_monitor.timeutil import utcnow
from xianyu_monitor.worker.pipeline import FrameProcessor

T0 = dt.datetime(2026, 10, 20, 21, 0, 0)


async def run(h, proc, scene, rt, seconds=0):
    obs = await h.analyzer.analyze(h.render(scene), T0 + dt.timedelta(seconds=seconds), rt, force_full=True)
    return await proc.process(obs, rt)


async def count(db, model, *where):
    async with db.session() as s:
        query = select(func.count()).select_from(model)
        for clause in where:
            query = query.where(clause)
        return int((await s.execute(query)).scalar_one())


def baseline_rows():
    return [SRow("老客户A", "REF-TEST-001", "刚刚", badge=1, avatar=11),
            SRow("闲小蜜", "[卡片消息]", "27分钟前", badge=1, avatar=12),
            SRow("老客户B", "你好", "10-11", avatar=13)]


async def seeded(db, rt):
    h = Harness()
    proc = FrameProcessor(db, "test")
    await run(h, proc, Scene(rows=baseline_rows(), overall=2), rt.with_overrides(monitor_mode="baseline"))
    return h, proc


async def test_baseline_seeding_creates_no_alerts_and_skips_system_rows(db, rt):
    await seeded(db, rt)
    async with db.session() as s:
        customers = (await s.execute(select(Customer))).scalars().all()
    assert sorted(c.display_name for c in customers) == ["老客户A", "老客户B"]
    assert all(c.status == "baseline" for c in customers)
    assert await count(db, Job) == 0
    assert await count(db, MessageEvent) == 0


async def test_existing_customer_message_creates_event_but_no_new_customer_email(db, rt):
    h, proc = await seeded(db, rt)
    rows = baseline_rows()
    rows[0].badge, rows[0].preview = 2, "REF-TEST-002 shipping?"
    result = await run(h, proc, Scene(rows=rows, overall=3), rt, 30)
    assert len(result.events) == 1
    assert await count(db, MessageEvent, MessageEvent.kind == "incoming") == 1
    assert await count(db, Job) == 0


async def test_new_customer_notified_once_across_messages_frames_and_restart(db, rt):
    h, proc = await seeded(db, rt)
    rows = [SRow("新买家C", "你好 还在吗", "刚刚", badge=1, avatar=21)] + baseline_rows()
    first = await run(h, proc, Scene(rows=rows, overall=3), rt, 30)
    assert len(first.new_customers) == 1 and len(first.jobs) == 1
    # same screen again, then a second message from the same customer
    await run(h, proc, Scene(rows=rows, overall=3), rt, 36)
    rows[0].badge, rows[0].preview = 2, "多久发货"
    second = await run(h, proc, Scene(rows=rows, overall=4), rt, 60)
    assert second.new_customers == [] and second.jobs == [] and len(second.events) == 1
    # restart: a fresh processor (state comes only from the database) sees the same screen
    again = await run(h, FrameProcessor(db, "test"), Scene(rows=rows, overall=4), rt, 90)
    assert again.events == [] and again.jobs == [] and again.new_customers == []
    assert await count(db, Job, Job.dedup_key.like("new_customer:%")) == 1
    assert await count(db, Customer, Customer.status == "new") == 1


async def test_mobile_read_transition_is_not_a_message(db, rt):
    h, proc = await seeded(db, rt)
    rows = baseline_rows()
    rows[0].badge, rows[0].preview = 3, "REF-TEST-003"
    await run(h, proc, Scene(rows=rows, overall=4), rt, 30)
    rows[0].badge, rows[0].time = None, "1分钟前"
    result = await run(h, proc, Scene(rows=rows, overall=1), rt, 100)
    async with db.session() as s:
        kinds = [e.kind for e in (await s.execute(select(MessageEvent).order_by(MessageEvent.id))).scalars()]
    assert kinds == ["incoming", "read_elsewhere"] and len(result.events) == 1


async def test_unchanged_overall_unread_does_not_hide_new_message(db, rt):
    h, proc = await seeded(db, rt)
    rows = baseline_rows()
    rows[0].badge, rows[0].preview = 1, "REF-TEST-004"
    await run(h, proc, Scene(rows=rows, overall=4), rt, 30)
    # target row 1 -> 2 while another row's badge disappears: overall stays 4
    rows[0].badge, rows[0].preview = 2, "REF-TEST-005"
    rows[1].badge = None
    result = await run(h, proc, Scene(rows=rows, overall=4), rt, 60)
    assert len(result.events) == 1
    async with db.session() as s:
        event = (await s.execute(select(MessageEvent).where(MessageEvent.id == result.events[0]))).scalar_one()
    assert event.kind == "incoming" and event.badge_before == 1 and event.badge_after == 2


async def test_new_conversation_without_badge_goes_to_review_not_email(db, rt):
    h, proc = await seeded(db, rt)
    rows = [SRow("卖家先联系的人", "我发出的消息", "刚刚", badge=None, avatar=31)] + baseline_rows()
    result = await run(h, proc, Scene(rows=rows), rt, 30)
    assert result.jobs == [] and len(result.reviews) == 1
    async with db.session() as s:
        item = (await s.execute(select(ReviewItem))).scalar_one()
    assert item.kind == "new_without_incoming_evidence"


async def test_unknown_name_with_old_activity_is_review(db, rt):
    h, proc = await seeded(db, rt)
    rows = baseline_rows() + [SRow("很久以前的买家", "早期消息", "10-07", badge=1, avatar=32)]
    result = await run(h, proc, Scene(rows=rows), rt, 30)
    assert result.jobs == []
    async with db.session() as s:
        assert (await s.execute(select(ReviewItem.kind))).scalar_one() == "new_with_old_activity"


async def test_duplicate_nickname_and_low_confidence_are_review_items(db, rt):
    h, proc = await seeded(db, rt)
    rows = [SRow("同名", "a", badge=1, avatar=41), SRow("同名", "b", badge=1, avatar=42),
            SRow("模糊", "c", badge=1, avatar=43, name_conf=0.4)]
    result = await run(h, proc, Scene(rows=rows), rt, 30)
    assert result.jobs == [] and result.new_customers == []
    async with db.session() as s:
        kinds = sorted((await s.execute(select(ReviewItem.kind))).scalars())
    assert kinds == ["identity_uncertain", "identity_uncertain", "low_ocr_confidence"]
    # seen again: review items are deduplicated and counted
    await run(h, proc, Scene(rows=rows), rt, 40)
    assert await count(db, ReviewItem) == 3


async def test_notifications_disabled_creates_customer_without_job(db, rt):
    h, proc = await seeded(db, rt)
    rows = [SRow("新买家D", "hi", badge=1, avatar=51)] + baseline_rows()
    result = await run(h, proc, Scene(rows=rows), rt.with_overrides(notifications_enabled=False), 30)
    assert len(result.new_customers) == 1 and result.jobs == []


async def add_rule(db, **kw):
    async with db.transaction() as s:
        s.add(KeywordRule(name="avail", enabled=True, priority=100, match_type="contains", match_mode="any",
                          patterns=["还在吗"], exclude_patterns=[], template="在的", cooldown_seconds=3600,
                          scope="all", allow_sensitive=False, requires_review=kw.get("requires_review", False),
                          created_at=utcnow(), updated_at=utcnow()))


async def test_reply_planning_respects_modes(db, rt):
    await add_rule(db)
    h, proc = await seeded(db, rt)
    rows = baseline_rows()
    rows[0].badge, rows[0].preview = 2, "还在吗"
    off = await run(h, proc, Scene(rows=rows), rt, 30)
    assert off.intents == []
    rows[0].badge = 3
    review = await run(h, proc, Scene(rows=rows), rt.with_overrides(reply_mode="review"), 40)
    async with db.session() as s:
        intent = await s.get(ReplyIntent, review.intents[0])
    assert intent.status == "review" and "review_mode" in intent.reasons
    rows[0].badge, rows[0].preview = 4, "请问还在吗"
    auto = await run(h, proc, Scene(rows=rows), rt.with_overrides(reply_mode="auto", reply_send_enabled=True), 50)
    async with db.session() as s:
        intent = await s.get(ReplyIntent, auto.intents[0])
        jobs = (await s.execute(select(Job).where(Job.kind == "reply"))).scalars().all()
    # the previous intent was "review" (not sent), so no cooldown applies
    assert intent.status == "queued" and len(jobs) == 1 and jobs[0].dedup_key == f"reply:{intent.id}"


async def test_reply_cooldown_skips_second_auto_reply(db, rt):
    await add_rule(db)
    h, proc = await seeded(db, rt)
    auto = rt.with_overrides(reply_mode="auto", reply_send_enabled=True)
    rows = baseline_rows()
    rows[0].badge, rows[0].preview = 2, "还在吗"
    await run(h, proc, Scene(rows=rows), auto, 30)
    rows[0].badge, rows[0].preview = 3, "还在吗?"
    second = await run(h, proc, Scene(rows=rows), auto, 40)
    async with db.session() as s:
        intent = await s.get(ReplyIntent, second.intents[0])
    assert intent.status == "skipped" and "cooldown_active" in intent.reasons


async def test_layout_mismatch_frame_records_no_detection(db, rt):
    h, proc = await seeded(db, rt)
    result = await run(h, proc, Scene(rows=baseline_rows(), cover_header=True), rt, 30)
    assert result.events == [] and result.frame_id is not None
    assert await count(db, MessageEvent) == 0


async def test_baseline_mode_does_not_plan_replies_or_emails_for_new_names(db):
    h = Harness()
    proc = FrameProcessor(db, "test")
    rt = RuntimeSettings({"monitor_mode": "baseline", "notifications_enabled": True, "reply_mode": "auto",
                          "reply_send_enabled": True})
    result = await run(h, proc, Scene(rows=[SRow("x", "还在吗", badge=1)]), rt)
    assert result.jobs == [] and result.intents == [] and result.events == []
