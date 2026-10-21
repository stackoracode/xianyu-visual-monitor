"""Reply targeting and send verification against a fake desktop (no real input is sent)."""

from PIL import ImageDraw

from synth import FakeDesktop, Harness, SBubble, SRow
from xianyu_monitor.detection.textnorm import normalize_name
from xianyu_monitor.replies.executor import ReplyExecutor, ReplyTarget
from xianyu_monitor.runtime_settings import RuntimeSettings
from xianyu_monitor.vision.analyzer import PaneObservation

RT = RuntimeSettings({"reply_step_delay_seconds": 0.1, "reply_verify_timeout_seconds": 0.3,
                      "full_validation_every_n_frames": 1000, "challenge_probe_every_n_frames": 1000})


async def no_sleep(_seconds):
    return None


def setup(**desktop_flags):
    h = Harness()
    rows = [SRow("买家甲", "还在吗", "刚刚", badge=1, avatar=1), SRow("买家乙", "谢谢", "5分钟前", avatar=2),
            SRow("买家丙", "好的", "10-11", avatar=3, selected=True)]
    history = {"买家甲": [SBubble("incoming", "你好"), SBubble("incoming", "还在吗")],
               "买家乙": [SBubble("incoming", "谢谢")], "买家丙": [SBubble("outgoing", "好的")]}
    desk = FakeDesktop(h, rows, history)
    for key, value in desktop_flags.items():
        setattr(desk, key, value)
    phases: list[str] = []

    async def set_phase(phase):
        phases.append(phase)
        return True

    executor = ReplyExecutor(h.analyzer, desk.capture_async, desk, sleep=no_sleep)
    target = ReplyTarget("c1", {normalize_name("买家甲")}, set(), "还在吗", "在的，可以直接拍")
    return desk, executor, target, set_phase, phases


async def test_verified_send():
    desk, executor, target, set_phase, phases = setup()
    outcome = await executor.execute(target, RT, set_phase)
    assert outcome.status == "sent_verified", outcome
    assert phases[:4] == ["precheck", "targeting", "verifying_target", "typing"]
    assert ("type", "在的，可以直接拍") in desk.actions and ("press", "Return") in desk.actions
    assert desk.history["买家甲"][-1].direction == "outgoing"


async def test_new_incoming_after_observed_send_does_not_erase_send_evidence():
    desk, executor, target, set_phase, _ = setup()
    capture = executor.capture
    polls = 0

    async def incoming_during_verification():
        nonlocal polls
        if ("press", "Return") in desk.actions:
            polls += 1
            if polls == 2:
                desk.history["买家甲"].append(SBubble("incoming", "多久发货"))
                desk.rows[0].preview = "多久发货"
        return await capture()

    executor.capture = incoming_during_verification
    outcome = await executor.execute(target, RT, set_phase)
    assert outcome.status == "sent_verified" and polls >= 2
    assert desk.actions.count(("press", "Return")) == 1


async def test_dry_run_sends_no_input():
    desk, executor, target, set_phase, phases = setup()
    outcome = await executor.execute(target, RT, set_phase, dry_run=True)
    assert outcome.status == "dry_run" and desk.actions == [] and phases == ["precheck"]


async def test_wrong_conversation_opened_aborts_before_typing_and_pauses():
    desk, executor, target, set_phase, phases = setup(misroute=1)
    outcome = await executor.execute(target, RT, set_phase)
    assert outcome.status == "aborted" and outcome.reason == "conversation_title_mismatch"
    assert outcome.pause_replies and outcome.clicked and not outcome.typed
    assert not any(a[0] == "type" for a in desk.actions) and "typing" not in phases


async def test_owner_typing_in_composer_aborts():
    desk, executor, target, set_phase, _ = setup()
    desk.composer = "店主正在输入"
    outcome = await executor.execute(target, RT, set_phase)
    assert outcome.status == "aborted" and outcome.reason == "composer_not_empty_owner_may_be_typing"
    assert not any(a[0] == "type" for a in desk.actions)


async def test_unconfirmed_send_is_unverified_and_pauses():
    desk, executor, target, set_phase, phases = setup(drop_send=True)
    outcome = await executor.execute(target, RT, set_phase)
    assert outcome.status == "sent_unverified" and outcome.pause_replies and outcome.typed
    assert "verifying_send" in phases


async def test_stale_trigger_aborts_without_clicking():
    desk, executor, target, set_phase, _ = setup()
    desk.rows[0].preview = "能便宜点吗"
    outcome = await executor.execute(target, RT, set_phase)
    assert outcome.status == "superseded" and outcome.reason == "trigger_stale_or_unreadable"
    assert desk.actions == []


async def test_duplicate_visible_name_aborts():
    desk, executor, target, set_phase, _ = setup()
    desk.rows[1].name = "买家甲"
    outcome = await executor.execute(target, RT, set_phase)
    assert outcome.status == "aborted" and outcome.reason == "target_row_not_unique"
    assert desk.actions == []


async def test_latest_bubble_outgoing_means_already_answered():
    desk, executor, target, set_phase, _ = setup()
    desk.history["买家甲"].append(SBubble("outgoing", "店主已回复"))
    outcome = await executor.execute(target, RT, set_phase)
    assert outcome.status == "aborted" and outcome.reason == "latest_bubble_not_incoming"


async def test_lease_loss_stops_before_input():
    desk, executor, target, _, _ = setup()

    async def lost(phase):
        return phase == "precheck"

    outcome = await executor.execute(target, RT, lost)
    assert outcome.reason == "lease_lost" and desk.actions == []


async def test_bad_paste_is_never_sent():
    desk, executor, target, set_phase, _ = setup()
    original = desk.type_text

    async def corrupt(text):
        await original(text + "多余内容")

    desk.type_text = corrupt
    outcome = await executor.execute(target, RT, set_phase)
    assert outcome.status == "aborted" and outcome.pause_replies and outcome.typed
    assert ("press", "Return") not in desk.actions


async def test_conversation_switch_after_paste_is_never_sent():
    desk, executor, target, set_phase, _ = setup()
    original = desk.type_text

    async def switch(text):
        await original(text)
        desk.selected = 1

    desk.type_text = switch
    outcome = await executor.execute(target, RT, set_phase)
    assert outcome.reason == "draft_or_target_recheck_failed" and outcome.pause_replies
    assert ("press", "Return") not in desk.actions


async def test_prefix_name_does_not_match_target():
    desk, executor, target, set_phase, _ = setup()
    desk.history["买家甲冒名"] = desk.history["买家甲"]
    original = desk.click

    async def rename(x, y):
        await original(x, y)
        desk.rows[0].name = "买家甲冒名"

    desk.click = rename
    outcome = await executor.execute(target, RT, set_phase)
    assert outcome.reason == "conversation_title_mismatch"
    assert not any(a[0] == "type" for a in desk.actions)


def test_masked_account_suffix_is_not_part_of_display_name():
    _, executor, target, _, _ = setup()
    assert executor._title_matches(PaneObservation("买家甲（t***0）", 0.99, [], "", 1), target, RT)
    assert not executor._title_matches(PaneObservation("买家甲冒名（t***0）", 0.99, [], "", 1), target, RT)


async def test_delayed_failed_send_icon_overrides_matching_outgoing_bubble():
    desk, executor, target, set_phase, _ = setup()
    original = desk.capture_async
    polls = 0

    async def capture():
        nonlocal polls
        frame = await original()
        if ("press", "Return") in desk.actions:
            polls += 1
            if polls >= 2:
                pane = await desk.h.analyzer.analyze_pane(frame)
                bubble = pane.last_bubble.box
                x, y = bubble[0] - 20, (bubble[1] + bubble[3]) // 2
                draw = ImageDraw.Draw(frame)
                draw.ellipse((x - 8, y - 8, x + 8, y + 8), fill=(240, 40, 40))
                draw.line((x, y - 5, x, y + 1), fill="white", width=2)
                draw.point((x, y + 4), fill="white")
        return frame

    executor.capture = capture
    outcome = await executor.execute(target, RT, set_phase)
    assert outcome.status == "sent_unverified" and outcome.pause_replies
    assert outcome.reason == "send_failure_or_target_changed" and polls == 2


async def test_lease_loss_after_paste_never_presses_enter():
    desk, executor, target, _, _ = setup()

    async def lose(phase):
        return phase != "sending"

    outcome = await executor.execute(target, RT, lose)
    assert outcome.pause_replies and outcome.typed and outcome.reason == "lease_lost_after_typing"
    assert ("press", "Return") not in desk.actions


async def test_slow_ocr_uses_elapsed_window_and_still_checks_final_frame():
    desk, executor, target, set_phase, _ = setup()
    elapsed = 0.0
    post_send_captures = 0
    original = executor._pane

    async def slow_pane(rt, now):
        nonlocal elapsed, post_send_captures
        result = await original(rt, now)
        if ("press", "Return") in desk.actions:
            post_send_captures += 1
            elapsed += 10.0  # one OCR pass already exceeds the requested window
        return result

    executor.monotonic = lambda: elapsed
    executor._pane = slow_pane
    outcome = await executor.execute(target, RT, set_phase)
    assert outcome.status == "sent_verified"
    assert post_send_captures == 2  # first early frame plus a frame after the deadline


async def test_new_message_after_click_supersedes_old_trigger_without_typing():
    desk, executor, target, set_phase, _ = setup()
    click = desk.click

    async def incoming_after_click(x, y):
        await click(x, y)
        desk.history["买家甲"].append(SBubble("incoming", "多久发货"))
        desk.rows[0].preview = "多久发货"

    desk.click = incoming_after_click
    outcome = await executor.execute(target, RT, set_phase)
    assert outcome.status == "superseded" and not outcome.typed
    assert not any(a[0] == "type" for a in desk.actions)


async def test_f5_returns_without_clicking_any_system_row():
    desk, executor, target, set_phase, _ = setup()
    desk.rows[1].name = "闲小蜜"
    desk.history["闲小蜜"] = []
    executor.analyzer.profile.park_system_conversation = True  # ignored legacy option
    outcome = await executor.execute(target, RT, set_phase)
    assert outcome.status == "sent_verified" and outcome.submitted
    before = list(desk.actions)
    assert (await executor.return_to_list(target, RT))["returned"]
    assert desk.selected is None
    assert desk.actions[len(before):] == [("press", "F5")]


async def test_f5_preserves_owner_draft():
    desk, executor, target, set_phase, _ = setup()
    await executor.execute(target, RT, set_phase)
    desk.composer = "店主自己的草稿"
    before = list(desk.actions)
    assert (await executor.return_to_list(target, RT))["reason"] == "owner_changed_target_or_draft_present"
    assert desk.actions == before


async def test_f5_timeout_is_return_failure_not_send_failure():
    desk, executor, target, set_phase, _ = setup()
    assert (await executor.execute(target, RT, set_phase)).status == "sent_verified"
    desk.refresh_stuck = True
    result = await executor.return_to_list(target, RT.with_overrides(reply_refresh_timeout_seconds=0))
    assert result["reason"] == "refresh_return_not_verified" and result["pause_replies"]
    assert desk.actions.count(("press", "F5")) == 1
    assert desk.actions.count(("press", "Return")) == 1


async def test_wrapped_bubble_uses_all_lines_for_send_verification():
    desk, executor, target, set_phase, _ = setup()
    target.reply_text = "第一行回复 第二行回复"
    original = desk.h.fake.recognize_line

    def line_only(image):
        result = original(image)
        if result.text == target.reply_text:
            from xianyu_monitor.ocr.base import OcrResult
            return OcrResult("第一行回复", result.confidence)
        return result

    desk.h.fake.recognize_line = line_only
    result = await executor.execute(target, RT, set_phase)
    assert result.status == "sent_verified" and result.submitted
