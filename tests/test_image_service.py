"""Text/image durability integration. All desktop input and SMTP are fake."""
import asyncio

import pytest
from sqlalchemy import select

from synth import FakeDesktop, Harness, SBubble, SRow
from test_service import configure, drain_jobs, make_service
from xianyu_monitor import state_keys
from xianyu_monitor.db import get_state
from xianyu_monitor.models import Job, KeywordRule, ReplyIntent
from xianyu_monitor.replies.images import ImageOutcome
from xianyu_monitor.runtime_settings import save_runtime
from xianyu_monitor.timeutil import utcnow


async def prepared(db, settings):
    h = Harness()
    await configure(db, h, monitor_mode='baseline', reply_mode='auto', reply_send_enabled=True, dry_run=False,
                    reply_image_send_enabled=True, reply_default_image_path='/private/reply.png')
    async with db.transaction() as s:
        s.add(KeywordRule(name='image greeting', enabled=True, priority=100, match_type='contains', match_mode='any',
                          patterns=['你好'], exclude_patterns=[], template='您好，请提供需求', cooldown_seconds=0,
                          scope='all', allow_sensitive=False, requires_review=False, send_image=True,
                          created_at=utcnow(), updated_at=utcnow()))
    rows = [SRow('买家甲', '旧消息', '10-11', avatar=1)]
    desk = FakeDesktop(h, rows, {'买家甲': [SBubble('incoming', '旧消息')]})
    service = make_service(settings, db, h, desk.capture, input_factory=lambda: desk)
    await service.start(use_file_lock=False)
    await service.handle_frame(await service.capture_backend.capture())
    async with db.transaction() as s:
        await save_runtime(s, {'monitor_mode': 'active'}, 'test')
    await service.runtime(max_age=0)
    rows[0].preview, rows[0].badge, rows[0].time = '你好', 1, '刚刚'
    desk.history['买家甲'].append(SBubble('incoming', '你好'))
    await service.handle_frame(await service.capture_backend.capture())
    return h, desk, service


@pytest.mark.parametrize('result,job_status', [('sent_verified', 'succeeded'), ('failed', 'failed'),
                                              ('uncertain', 'uncertain')])
async def test_image_part_cannot_demote_or_repeat_verified_text(db, settings, monkeypatch, result, job_status):
    _, desk, service = await prepared(db, settings)

    async def image_part(self, target, path, rt, set_phase, remember, expected_sha256=None, **kwargs):
        async with db.session() as s:
            intent = (await s.execute(select(ReplyIntent))).scalar_one()
            job = await s.get(Job, intent.job_id)
            assert intent.status == 'sent_verified' and job.status == 'leased'
        assert await set_phase('image_preparing')
        await remember('a' * 64)
        desk.actions.append(('image_part',))
        return ImageOutcome(result, 'synthetic_image_outcome', submitted=result != 'failed', pause_replies=result == 'uncertain')

    monkeypatch.setattr('xianyu_monitor.worker.service.ImageReplyExecutor.execute', image_part)
    await drain_jobs(service, 'reply')
    async with db.session() as s:
        intent = (await s.execute(select(ReplyIntent))).scalar_one()
        job = await s.get(Job, intent.job_id)
        assert intent.status == 'sent_verified' and intent.image_status == result
        assert intent.image_sha256 == 'a' * 64 and job.status == job_status
        assert intent.verification['return_to_list']['returned']
        assert (await get_state(s, state_keys.REPLY_PAUSE)).get('paused', False) == (result == 'uncertain')
    assert desk.actions.count(('type', '您好，请提供需求')) == 1
    assert desk.actions.index(('press', 'Return')) < desk.actions.index(('image_part',)) < desk.actions.index(('press', 'F5'))
    await drain_jobs(service, 'reply')
    assert desk.actions.count(('press', 'Return')) == 1
    await service.release_lease()


async def test_restart_during_image_preserves_sent_text_and_does_not_requeue(db, settings, monkeypatch):
    h, desk, service = await prepared(db, settings)

    async def interrupted(self, target, path, rt, set_phase, remember, expected_sha256=None, **kwargs):
        await remember('b' * 64)
        assert await set_phase('image_submitting')
        raise asyncio.CancelledError

    monkeypatch.setattr('xianyu_monitor.worker.service.ImageReplyExecutor.execute', interrupted)
    job = (await service.jobs.claim(['reply']))[0]
    with pytest.raises(asyncio.CancelledError):
        await service._run_reply(job)
    await service.release_lease()
    restarted = make_service(settings, db, h, desk.capture, input_factory=lambda: desk)
    await restarted.start(use_file_lock=False)
    async with db.session() as s:
        intent = (await s.execute(select(ReplyIntent))).scalar_one()
        assert intent.status == 'sent_verified' and intent.image_status == 'uncertain'
        assert (await s.get(Job, intent.job_id)).status == 'uncertain'
    assert not await restarted.jobs.claim(['reply'])
    assert desk.actions.count(('press', 'Return')) == 1
    await restarted.release_lease()
