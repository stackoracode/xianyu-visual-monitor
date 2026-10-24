"""Recovery state machine: bounded inputs, identity checks and durable attempt markers."""
import copy
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError

from synth import base_profile_dict
from xianyu_monitor.ocr.base import OcrResult
from xianyu_monitor.runtime_settings import RuntimeSettings
from xianyu_monitor.vision.imageops import dhash
from xianyu_monitor.vision.profile import Profile
from xianyu_monitor.worker.page_maintenance import PageMaintenance


def calibration():
    return {**{key: [10 + i * 30, 10, 30 + i * 30, 30] for i, key in enumerate(
        ('network_title', 'reconnect_button', 'login_heading', 'quick_button', 'quick_account',
         'quick_avatar', 'seller'))}, 'expected_account': 'a**1', 'expected_seller': 'DemoSeller',
        'expected_avatar_hash': dhash(Image.new('RGB', (20, 20), (2, 0, 0)))}


class Rig:
    def __init__(self):
        self.now, self.stage, self.reachable = 1000, 1, True
        self.draft, self.selected, self.foreign, self.challenge = '', False, False, []
        self.foreground, self.low_conf, self.changed, self.different_account = True, False, False, False
        self.actions, self.saved = [], []
        self.captures = 0
        data = base_profile_dict()
        data['page_recovery'] = calibration()
        self.profile = Profile.model_validate(data)
        self.analyzer = SimpleNamespace(profile=self.profile, ocr=SimpleNamespace(recognize=self.ocr),
            check_screen=self.check_screen, analyze=self.analyze, analyze_pane=self.pane,
            _probe_challenge=self.probe, reset=lambda: None)
        self.rt = RuntimeSettings({'dry_run': False, 'monitor_mode': 'active', 'page_recovery_enabled': True,
            'page_quick_enter_enabled': True, 'page_refresh_enabled': True, 'page_refresh_interval_minutes': 5})
        self.state = {}
        self.maintenance = PageMaintenance(self.analyzer, self.capture, self, self.online, self.persist,
                                           clock=lambda: self.now)

    async def capture(self):
        self.captures += 1
        img = Image.new('RGB', (self.profile.frame_width, self.profile.frame_height), (self.stage, 0, 0))
        if self.changed and self.captures % 2 == 0:
            img.putpixel((100, 10), (self.stage, 255, 0))  # quick button changed, text still agrees
        return img

    async def ocr(self, crops):
        out = {}
        for key, image in crops:
            stage = image.getpixel((0, 0))[0]
            text = {'network_title': '连接中断，请重连', 'reconnect_button': '重连'} if stage == 1 else (
                {'login_heading': '手机扫码安全登录', 'quick_button': '快速进入',
                 'quick_account': 'b**2' if self.different_account or stage == 4 else 'a**1'} if stage in (2, 4) else
                {'seller': 'SomeoneElse' if self.foreign else 'DemoSeller'})
            out[key] = OcrResult(text.get(key, ''), 0.2 if self.low_conf else 0.99)
        return out

    async def check_screen(self, frame, rt):
        return 'ok' if self.stage == 3 else 'layout_mismatch'

    async def analyze(self, *args, **kwargs):
        return SimpleNamespace(status='ok', rows=[SimpleNamespace(present=True, selected=self.selected)])

    async def pane(self, frame):
        return SimpleNamespace(title='Buyer' if self.selected else '', composer_text=self.draft, composer_conf=0.99)

    async def probe(self, *args):
        return self.challenge

    async def online(self):
        return self.reachable

    async def persist(self, state):
        self.saved.append(copy.deepcopy(state))

    async def is_browser_active(self):
        return self.foreground

    async def click(self, x, y):
        assert self.saved[-1]['phase'] in ('reconnect', 'quick')
        assert self.saved[-1][self.saved[-1]['phase']]
        self.actions.append(('click', x, y))

    async def press(self, key):
        assert self.saved[-1]['refreshed']
        self.actions.append(('press', key))

    async def tick(self, **kw):
        return await self.maintenance.tick(self.rt, self.state, **kw)

    def advance(self, seconds=31):
        self.now += seconds


async def test_outage_reconnect_refresh_quick_enter_and_two_ready_frames():
    r = Rig()
    r.reachable = False
    assert await r.tick() and not r.actions
    r.advance(3600)
    assert await r.tick() and not r.actions  # long outage does not expire online recovery budget
    r.reachable = True
    r.advance()
    await r.tick()
    assert len(r.actions) == 1 and r.state['reconnect']
    r.advance()
    await r.tick()
    assert r.actions[-1] == ('press', 'F5')
    r.advance()
    r.stage = 2
    r.challenge = ['手机扫码安全登录']
    await r.tick()
    assert len(r.actions) == 3 and r.state['quick']
    r.stage, r.challenge = 3, []
    await r.tick()
    assert r.state['active']
    await r.tick()
    assert not r.state['active'] and r.state['phase'] == 'ready'
    assert len(r.actions) == 3


@pytest.mark.parametrize('flag,reason', [('different_account', 'cached_account_not_verified'),
    ('foreground', 'browser_not_active'), ('changed', 'button_changed_before_input')])
async def test_quick_enter_refuses_wrong_account_foreground_or_changed_button(flag, reason):
    r = Rig()
    r.stage = 2
    setattr(r, flag, flag != 'foreground')
    await r.tick()
    assert r.state['blocked'] and r.state['phase'] == reason and not r.actions


async def test_avatar_and_low_confidence_do_not_authorize_click():
    r = Rig()
    r.stage = 2
    r.profile.page_recovery.expected_avatar_hash = 'ffffffffffffffff'
    await r.tick()
    assert r.state['blocked'] and not r.actions
    r = Rig()
    r.stage, r.low_conf = 2, True
    await r.tick()
    assert not r.actions


async def test_restart_never_repeats_quick_enter_and_owner_can_clear_block():
    r = Rig()
    r.stage = 2
    await r.tick()
    state = copy.deepcopy(r.state)
    r.maintenance = PageMaintenance(r.analyzer, r.capture, r, r.online, r.persist, clock=lambda: r.now)
    r.state = state
    r.advance()
    await r.tick()
    assert r.state['phase'] == 'waiting_for_quick_enter' and len(r.actions) == 1
    r.advance(181)
    await r.tick()
    assert r.state['blocked'] and len(r.actions) == 1
    r.stage = 3
    await r.tick()
    await r.tick()
    assert not r.state['active'] and not r.state['blocked']


@pytest.mark.parametrize('variant', ['disabled', 'captcha', 'draft'])
async def test_disabled_quick_enter_captcha_or_draft_stops_input(variant):
    r = Rig()
    r.stage = 2
    if variant == 'disabled':
        r.rt = r.rt.with_overrides(page_quick_enter_enabled=False)
    elif variant == 'captcha':
        r.challenge = ['请完成安全验证']
    else:
        r.stage = 1
        r.state = {'active': True, 'reconnect': True}
        r.draft = 'owner draft'
    await r.tick()
    assert r.state['blocked'] and not r.actions


async def test_periodic_refresh_initializes_then_defers_for_selected_chat_draft_or_wrong_seller():
    r = Rig()
    r.stage = 3
    assert not await r.tick(periodic=True)
    r.advance(301)
    for attr in ('selected', 'foreign', 'draft'):
        setattr(r, attr, 'owner draft' if attr == 'draft' else True)
        assert not await r.tick(periodic=True)
        setattr(r, attr, '' if attr == 'draft' else False)
    assert not r.actions
    await r.tick(periodic=True)
    assert r.actions == [('press', 'F5')]
    await r.tick()
    await r.tick()
    r.advance(1)
    assert not await r.tick(periodic=True) and len(r.actions) == 1


async def test_unknown_page_timeout_and_wrong_seller_never_resume_replies():
    r = Rig()
    await r.tick()
    r.stage, r.foreign = 3, True
    r.advance(181)
    await r.tick()
    assert r.state['active'] and r.state['blocked']


async def test_default_off_dry_run_and_wrong_geometry_never_act():
    r = Rig()
    for overrides in ({'dry_run': True}, {'monitor_mode': 'baseline'},
                      {'page_recovery_enabled': False, 'page_refresh_enabled': False}):
        await r.maintenance.tick(r.rt.with_overrides(**overrides), r.state)
    r.profile.frame_width += 1
    original = r.capture

    async def wrong_size():
        return (await original()).crop((0, 0, 50, 50))

    r.maintenance.capture = wrong_size
    await r.tick()
    assert not r.actions


def test_recovery_calibration_bounds_and_hash_are_validated():
    data = base_profile_dict()
    data['page_recovery'] = calibration()
    data['page_recovery']['quick_button'] = [-1, 1, 10, 10]
    with pytest.raises(ValidationError):
        Profile.model_validate(data)
    data['page_recovery'] = calibration()
    data['page_recovery']['expected_avatar_hash'] = 'bad'
    with pytest.raises(ValidationError):
        Profile.model_validate(data)


async def test_service_owns_only_its_recovery_pause_and_updates_heartbeat(db, settings, monkeypatch):
    from synth import Harness, Scene
    from test_service import configure, make_service
    from xianyu_monitor import state_keys
    from xianyu_monitor.db import get_state, set_state
    h = Harness()
    h.profile_dict['page_recovery'] = calibration()
    await configure(db, h, monitor_mode='active', dry_run=False, page_recovery_enabled=True)
    service = make_service(settings, db, h, lambda: h.render(Scene()), input_factory=lambda: object())
    await service.start(use_file_lock=False)
    calls = []

    async def tick(self, rt, state, **kwargs):
        calls.append(1)
        state.update(active=len(calls) == 1, phase='waiting_for_network' if len(calls) == 1 else 'ready')
        await self.persist(state)
        return True

    monkeypatch.setattr(PageMaintenance, 'tick', tick)
    assert await service._maintain_page(await service.capture_backend.capture())
    assert service.last_status == 'page_recovery' and service.last_frame_at is not None
    async with db.session() as s:
        pause = await get_state(s, state_keys.REPLY_PAUSE)
        assert pause['paused'] and pause['reason'] == 'page_recovery'
    assert await service._maintain_page(await service.capture_backend.capture())
    async with db.transaction() as s:
        pause = await get_state(s, state_keys.REPLY_PAUSE)
        assert not pause['paused']
        await set_state(s, state_keys.REPLY_PAUSE, {'paused': True, 'by': 'owner', 'reason': 'manual'})
    assert not await service._maintain_page(await service.capture_backend.capture())
    assert len(calls) == 2
    async with db.session() as s:
        assert (await get_state(s, state_keys.REPLY_PAUSE))['reason'] == 'manual'
    await service.release_lease()


async def test_service_does_not_refresh_while_reply_job_pending(db, settings, monkeypatch):
    from synth import Harness, Scene
    from test_service import configure, make_service
    from xianyu_monitor.queueing.jobs import enqueue
    h = Harness()
    h.profile_dict['page_recovery'] = calibration()
    await configure(db, h, monitor_mode='active', dry_run=False, page_refresh_enabled=True)
    service = make_service(settings, db, h, lambda: h.render(Scene()), input_factory=lambda: object())
    await service.start(use_file_lock=False)
    async with db.transaction() as s:
        await enqueue(s, 'reply', 'reply:test', {'intent_id': 1})

    async def forbidden(*args, **kwargs):
        raise AssertionError('pending reply must take precedence over scheduled maintenance')

    monkeypatch.setattr(PageMaintenance, 'tick', forbidden)
    assert not await service._maintain_page(await service.capture_backend.capture(), periodic=True)
    await service.release_lease()


async def test_owner_pause_during_ocr_stops_before_input():
    r = Rig()
    async def guard():
        return False
    r.maintenance.guard = guard
    await r.tick()
    assert r.state['blocked'] and r.state['phase'] == 'owner_paused_before_input' and not r.actions


async def test_verification_appearing_in_fresh_frame_blocks_click():
    r = Rig()
    r.stage = 2
    calls = []
    async def probe(*args):
        calls.append(1)
        return ['请完成安全验证'] if len(calls) > 1 else []
    r.analyzer._probe_challenge = probe
    await r.tick()
    assert r.state['phase'] == 'verification_requires_owner' and not r.actions


async def test_ready_samples_must_be_consecutive_even_after_restart():
    r = Rig()
    r.state = {'active': True, 'blocked': True, 'ready_samples': 0}
    r.stage = 3
    await r.tick()
    assert r.state['ready_samples'] == 1
    r.foreign = True
    await r.tick()
    assert r.saved[-1]['ready_samples'] == 0
    r.state = copy.deepcopy(r.saved[-1])
    r.foreign = False
    await r.tick()
    assert r.state['active'] and r.state['ready_samples'] == 1
    await r.tick()
    assert not r.state['active'] and r.state['next_action_at'] == 0
