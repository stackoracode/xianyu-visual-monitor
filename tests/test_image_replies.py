"""Image reply state machine; no native input or external messages."""
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from synth import FakeDesktop, Harness, SBubble, SRow
from xianyu_monitor.replies.executor import ReplyExecutor, ReplyTarget
from xianyu_monitor.replies.images import ImageReplyExecutor
from xianyu_monitor.runtime_settings import RuntimeSettings
from xianyu_monitor.vision.reply_image import ImageMatch, load_asset

RT = RuntimeSettings({'reply_step_delay_seconds': .1, 'reply_verify_timeout_seconds': .2,
                      'reply_image_timeout_seconds': .5, 'reply_image_verify_seconds': .2,
                      'full_validation_every_n_frames': 1000, 'challenge_probe_every_n_frames': 1000})


class ImageDesktop(FakeDesktop):
    def __init__(self):
        h = Harness({'image_attach_click': (480, 545), 'image_dialog_title': (490, 30, 800, 56),
                     'image_dialog_path': (280, 62, 1196, 92)})
        super().__init__(h, [SRow('买家甲', '你好', '刚刚', badge=1, avatar=1), SRow('买家乙', '旧消息', avatar=2)],
                         {'买家甲': [SBubble('incoming', '你好')], '买家乙': []})
        self.dialog = False
        self.path = ''
        self.image_sent = False
        self.bad_path = False
        self.drop_upload = False
        self.no_preview_update = False
        self.wrong_target = False
        self.failure_icon = False
        self.baseline_image = False
        self.elapsed = 0.
        self.upload_delay = 0.
        self.upload_ready_at = None

    async def sleep(self, seconds):
        self.elapsed += seconds

    def capture(self):
        if self.upload_ready_at is not None and self.elapsed >= self.upload_ready_at:
            self.image_sent = True
            self.rows[0].preview = '[图片]'
            self.upload_ready_at = None
        if self.dialog:
            frame = Image.new('RGB', (1280, 720), (170, 170, 170))
            draw = ImageDraw.Draw(frame)
            draw.text((510, 36), 'Open Files', fill='black')
            self.h.fake.register(frame.crop(self.h.profile.image_dialog_title), 'Open Files', .99)
            if self.path:
                draw.text((285, 70), self.path, fill='black')
                self.h.fake.register(frame.crop(self.h.profile.image_dialog_path),
                                     '/wrong/image.png' if self.bad_path else self.path, .99)
            return frame
        frame = super().capture()
        if self.failure_icon and self.image_sent:
            x, y, _, _ = self.match_box()
            ImageDraw.Draw(frame).rectangle((x - 20, y + 10, x - 13, y + 20), fill=(240, 30, 30))
        return frame

    async def click(self, x, y):
        if (x, y) == self.h.profile.image_attach_click:
            self.actions.append(('click_image', x, y))
            self.dialog = True
        else:
            await super().click(x, y)

    async def type_text(self, text):
        if self.dialog:
            self.actions.append(('file_path', text))
            self.path = text
        else:
            await super().type_text(text)

    async def press(self, key):
        if self.dialog:
            self.actions.append(('dialog_key', key))
            if key == 'Escape':
                self.dialog = False
            elif key == 'Return':
                self.dialog = False
                if not self.drop_upload:
                    self.image_sent = not self.upload_delay
                    if self.upload_delay:
                        self.upload_ready_at = self.elapsed + self.upload_delay
                    elif not self.no_preview_update:
                        self.rows[0].preview = '[图片]'
                    if self.wrong_target:
                        self.selected = 1
        else:
            await super().press(key)

    def match_box(self):
        _, _, right, bottom = self.h.profile.message_pane
        return (right - 210, bottom - 110, right - 50, bottom - 20)

    def matcher(self, frame, reference, pane, threshold):
        if self.image_sent or self.baseline_image:
            return ImageMatch(self.match_box(), .99)
        return None


def make_asset(tmp_path):
    path = tmp_path / 'reply.png'
    image = Image.new('RGB', (320, 180), 'white')
    ImageDraw.Draw(image).rectangle((10, 20, 210, 80), fill=(40, 70, 180))
    image.save(path)
    return path


async def verified_text(desk):
    executor = ReplyExecutor(desk.h.analyzer, desk.capture_async, desk,
                             sleep=desk.sleep, monotonic=lambda: desk.elapsed)
    target = ReplyTarget('customer', {'买家甲'}, set(), '你好', '您好，请提供需求')
    phases = []

    async def phase(value):
        phases.append(value)
        return True

    assert (await executor.execute(target, RT, phase)).status == 'sent_verified'
    return executor, target, phase, phases


async def test_text_precedes_image_and_f5_is_last(tmp_path):
    desk = ImageDesktop()
    executor, target, phase, phases = await verified_text(desk)
    remembered = []

    async def remember(digest):
        remembered.append(digest)

    image = await ImageReplyExecutor(executor, desk.matcher).execute(target, str(make_asset(tmp_path)), RT, phase, remember)
    assert image.status == 'sent_verified' and image.submitted
    assert len(remembered) == 1 and len(remembered[0]) == 64
    assert desk.actions.index(('press', 'Return')) < next(i for i, a in enumerate(desk.actions) if a[0] == 'click_image')
    assert (await executor.return_to_list(target, RT))['returned']
    assert desk.actions[-1] == ('press', 'F5') and desk.selected is None
    assert 'image_submitting' in phases


async def test_slow_upload_waits_without_selecting_file_twice(tmp_path):
    desk = ImageDesktop()
    executor, target, phase, _ = await verified_text(desk)
    desk.upload_delay = 55.
    rt = RuntimeSettings({'reply_step_delay_seconds': 10., 'reply_image_timeout_seconds': 180.,
                          'reply_image_verify_seconds': 5.})

    async def remember(digest):
        pass

    image = await ImageReplyExecutor(executor, desk.matcher).execute(
        target, str(make_asset(tmp_path)), rt, phase, remember)
    assert image.status == 'sent_verified' and image.submitted
    assert desk.elapsed >= 55.
    assert sum(a[0] == 'click_image' for a in desk.actions) == 1
    assert desk.actions.count(('dialog_key', 'Return')) == 1


@pytest.mark.parametrize('flag,reason', [('drop_upload', 'image_not_confirmed_within_timeout'),
                                       ('no_preview_update', 'image_not_confirmed_within_timeout'),
                                       ('baseline_image', 'image_not_confirmed_within_timeout'),
                                       ('wrong_target', 'image_target_changed'),
                                       ('failure_icon', 'image_send_failure_indicator')])
async def test_image_failure_never_repeats_text(tmp_path, flag, reason):
    desk = ImageDesktop()
    executor, target, phase, _ = await verified_text(desk)
    setattr(desk, flag, True)

    async def remember(_):
        pass

    result = await ImageReplyExecutor(executor, desk.matcher).execute(target, str(make_asset(tmp_path)), RT, phase, remember)
    assert result.status == 'uncertain' and result.reason == reason and result.pause_replies
    assert desk.actions.count(('type', target.reply_text)) == 1
    assert desk.actions.count(('press', 'Return')) == 1
    assert desk.actions.count(('dialog_key', 'Return')) == 1


async def test_wrong_file_path_cancels_before_submission(tmp_path):
    desk = ImageDesktop()
    executor, target, phase, _ = await verified_text(desk)
    desk.bad_path = True

    async def remember(_):
        pass

    result = await ImageReplyExecutor(executor, desk.matcher).execute(target, str(make_asset(tmp_path)), RT, phase, remember)
    assert result.status == 'failed' and not result.submitted
    assert ('dialog_key', 'Return') not in desk.actions and not desk.dialog


async def test_new_input_before_image_cancels_stale_attachment(tmp_path):
    desk = ImageDesktop()
    executor, target, phase, _ = await verified_text(desk)
    desk.history['买家甲'].append(SBubble('incoming', '新问题'))

    async def remember(_):
        pass

    result = await ImageReplyExecutor(executor, desk.matcher).execute(target, str(make_asset(tmp_path)), RT, phase, remember)
    assert result.status == 'cancelled' and not any(a[0] == 'click_image' for a in desk.actions)


def test_asset_integrity_and_type(tmp_path):
    path = make_asset(tmp_path)
    asset = load_asset(str(path))
    path.write_bytes(b'not an image')
    with pytest.raises(ValueError, match='image_changed'):
        load_asset(str(path), asset.sha256)
    with pytest.raises(ValueError, match='absolute'):
        load_asset('relative.png')
    assert asset.image.size == (320, 180) and Path(asset.path).is_absolute()
