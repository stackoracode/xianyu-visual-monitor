"""Idle review chats leave the pane without sending or discarding owner drafts."""
from sqlalchemy import select

from synth import FakeDesktop, Harness, SBubble, SRow
from test_service import add_burst_rules, configure, drain_jobs, make_service
from xianyu_monitor.models import ReplyIntent
from xianyu_monitor.replies.executor import ReplyOutcome
from xianyu_monitor.runtime_settings import save_runtime


async def setup_idle(db, settings):
    h = Harness()
    await configure(db, h, monitor_mode='baseline', reply_mode='off', dry_run=False)
    desk = FakeDesktop(h, [SRow('买家甲', '旧消息', '10-11', avatar=1, selected=True)],
                       {'买家甲': [SBubble('incoming', '旧消息')]})
    service = make_service(settings, db, h, desk.capture, input_factory=lambda: desk)
    await service.start(use_file_lock=False)
    await service.handle_frame(await service.capture_backend.capture())
    async with db.transaction() as s:
        await save_runtime(s, {'monitor_mode': 'active'}, 'test')
    await service.runtime(max_age=0)
    return desk, service


async def test_idle_selected_chat_returns_even_with_replies_off(db, settings):
    desk, service = await setup_idle(db, settings)
    await service.handle_frame(await service.capture_backend.capture())
    assert desk.actions == [('press', 'F5')] and desk.selected is None
    await service.release_lease()


async def test_idle_return_preserves_owner_draft(db, settings):
    desk, service = await setup_idle(db, settings)
    desk.composer = '店主自己的草稿'
    await service.handle_frame(await service.capture_backend.capture())
    assert ('press', 'F5') not in desk.actions and desk.selected == 0
    await service.release_lease()


async def test_pre_send_review_leaves_chat_without_sending(db, settings, monkeypatch):
    desk, service = await setup_idle(db, settings)
    await add_burst_rules(db)
    async with db.transaction() as s:
        await save_runtime(s, {'reply_mode': 'auto', 'reply_send_enabled': True}, 'test')
    await service.runtime(max_age=0)
    desk.history['买家甲'].append(SBubble('incoming', '还在吗'))
    desk.rows[0].preview = '还在吗'
    await service.handle_frame(await service.capture_backend.capture())
    assert ('press', 'F5') not in desk.actions  # queued reply owns the next action

    async def unreadable(self, target, rt, set_phase, **kwargs):
        await self.input.click(*self.analyzer.profile.row_center(0))
        return ReplyOutcome('aborted', 'cannot_read_latest_bubble', clicked=True)

    monkeypatch.setattr('xianyu_monitor.worker.service.ReplyExecutor.execute', unreadable)
    await drain_jobs(service, 'reply')
    async with db.session() as s:
        i = (await s.execute(select(ReplyIntent))).scalar_one()
        assert i.status == 'review' and i.verification['return_to_list']['returned']
    assert desk.actions.count(('press', 'F5')) == 1 and desk.selected is None
    assert not any(a[0] == 'type' for a in desk.actions)
    await service.release_lease()

async def test_failed_idle_refresh_is_not_repeated_forever(db, settings):
    desk, service = await setup_idle(db, settings)
    desk.refresh_stuck = True
    async with db.transaction() as s:
        await save_runtime(s, {'reply_refresh_timeout_seconds': 1}, 'test')
    await service.runtime(max_age=0)
    await service.handle_frame(await service.capture_backend.capture())
    assert desk.actions.count(('press', 'F5')) == 1
    service._last_idle_return_check = 0  # test the failure latch, not its time throttle
    await service.handle_frame(await service.capture_backend.capture())
    assert desk.actions.count(('press', 'F5')) == 1
    await service.release_lease()
