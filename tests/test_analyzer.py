import datetime as dt

from synth import Harness, Scene, SRow, base_profile_dict, draw_scene
from xianyu_monitor.runtime_settings import RuntimeSettings
from xianyu_monitor.vision import imageops
from xianyu_monitor.vision.pane import find_bubbles
from xianyu_monitor.vision.profile import Profile

NOW = dt.datetime(2026, 10, 20, 21, 0, 0)
RT = RuntimeSettings({"full_validation_every_n_frames": 1000, "challenge_probe_every_n_frames": 1000})


def rows():
    return [SRow("用户_1001", "REF-TEST-001", "刚刚", badge=1, avatar=1),
            SRow("闲小蜜", "[卡片消息]", "27分钟前", badge=1, avatar=2),
            SRow("buyer-b", "你好", "10-11", avatar=3, selected=True)]


def test_example_profile_is_valid_and_geometry_matches_reference_annotations():
    profile = Profile.model_validate(base_profile_dict())
    assert profile.row_box(0) == (45, 290, 373, 358)
    assert profile.field_box(0, "badge") == (82, 300, 101, 319)
    assert profile.field_box(0, "name") == (110, 299, 272, 320)


async def test_rows_are_parsed():
    h = Harness()
    obs = await h.analyzer.analyze(h.render(Scene(rows=rows(), overall=4)), NOW, RT)
    assert obs.status == "ok"
    present = [r for r in obs.rows if r.present]
    assert [r.name for r in present] == ["用户_1001", "闲小蜜", "buyer-b"]
    assert present[0].badge_present and present[0].badge_count == 1
    assert not present[2].badge_present and present[2].selected
    assert present[0].avatar_hash and present[0].avatar_hash != present[2].avatar_hash
    assert obs.overall_unread == 4
    assert not obs.rows[3].present and not obs.rows[4].present


async def test_unchanged_frame_skips_ocr_and_changed_row_only_reocrs_that_row():
    h = Harness()
    scene = Scene(rows=rows(), overall=4)
    await h.analyzer.analyze(h.render(scene), NOW, RT)
    calls = h.fake.calls
    again = await h.analyzer.analyze(h.render(scene), NOW + dt.timedelta(seconds=6), RT)
    assert again.status == "unchanged" and not again.ocr_performed and h.fake.calls == calls
    scene.rows[0].badge = 2
    scene.rows[0].preview = "REF-TEST-005"
    changed = await h.analyzer.analyze(h.render(scene), NOW + dt.timedelta(seconds=12), RT)
    assert changed.status == "ok" and changed.rows[0].badge_count == 2
    # one row: name/preview/time/badge (+ overall badge digest unchanged -> not re-read)
    assert h.fake.calls - calls <= 5


async def test_small_badge_change_is_detected_as_change():
    h = Harness()
    scene = Scene(rows=rows(), overall=4)
    await h.analyzer.analyze(h.render(scene), NOW, RT)
    scene.rows[2].badge = 1
    obs = await h.analyzer.analyze(h.render(scene), NOW, RT)
    assert obs.changed and obs.rows[2].badge_present


async def test_avatar_fingerprint_ignores_badge_overlap():
    h = Harness()
    scene = Scene(rows=[SRow("a", "x", badge=None, avatar=7)])
    first = (await h.analyzer.analyze(h.render(scene), NOW, RT)).rows[0].avatar_hash
    scene.rows[0].badge = 3
    second = (await h.analyzer.analyze(h.render(scene), NOW, RT)).rows[0].avatar_hash
    assert first == second


async def test_frame_size_change_is_layout_mismatch():
    h = Harness()
    obs = await h.analyzer.analyze(h.render(Scene(rows=rows(), size=(1366, 768))), NOW, RT)
    assert obs.status == "layout_mismatch" and "frame_size" in obs.layout_issues[0]
    assert obs.rows == []


async def test_covered_window_fails_anchor_check():
    h = Harness()
    obs = await h.analyzer.analyze(h.render(Scene(rows=rows(), cover_header=True)), NOW, RT)
    assert obs.status == "layout_mismatch"
    assert any(issue.startswith("anchor:") for issue in obs.layout_issues)


async def test_challenge_prompt_is_detected():
    h = Harness()
    obs = await h.analyzer.analyze(h.render(Scene(rows=rows(), challenge="请完成安全验证 拖动滑块")), NOW, RT)
    assert obs.status == "challenge"
    assert "安全验证" in obs.challenge_hits


async def test_safety_ocr_reuses_only_identical_pixels_and_keywords():
    h = Harness()
    frame = h.render(Scene(rows=rows()))
    assert await h.analyzer.check_screen(frame, RT) == "ok"
    calls = h.fake.calls
    assert await h.analyzer.check_screen(frame.copy(), RT) == "ok"
    assert h.fake.calls == calls
    challenge = h.render(Scene(rows=rows(), challenge="请完成安全验证"))
    assert await h.analyzer.check_screen(challenge, RT) == "challenge"
    calls = h.fake.calls
    assert await h.analyzer.check_screen(challenge, RT) == "challenge"
    assert h.fake.calls == calls  # cached positive must still block input
    changed = RT.with_overrides(challenge_keywords=["different"])
    assert await h.analyzer.check_screen(challenge, changed) == "ok"
    assert h.fake.calls == calls + 1
    h.analyzer.reset()
    assert await h.analyzer.check_screen(challenge, RT) == "challenge"
    assert h.fake.calls == calls + 2
    assert await h.analyzer.check_screen(frame, RT) == "ok"


async def test_blank_frame():
    h = Harness()
    from PIL import Image

    obs = await h.analyzer.analyze(Image.new("RGB", (1280, 720), (0, 0, 0)), NOW, RT)
    assert obs.status == "blank"


async def test_low_confidence_ocr_is_reported_not_guessed():
    h = Harness()
    scene = Scene(rows=[SRow("用户_1001", "x", name_conf=0.3)])
    obs = await h.analyzer.analyze(h.render(scene), NOW, RT)
    assert obs.rows[0].name_conf == 0.3


def test_bubble_segmentation_direction():
    h = Harness()
    from synth import SBubble

    frame = draw_scene(Scene(bubbles=[SBubble("incoming", "hello"), SBubble("outgoing", "hi there")]), h.profile)
    bubbles = find_bubbles(frame, h.profile.message_pane, h.profile.colors)
    assert [b.direction for b in bubbles] == ["incoming", "outgoing"]


async def test_pane_analysis_reads_title_bubbles_and_composer():
    h = Harness()
    from synth import SBubble

    scene = Scene(rows=rows(), title="buyer-b", bubbles=[SBubble("incoming", "还在吗"), SBubble("outgoing", "在的")],
                  composer="")
    pane = await h.analyzer.analyze_pane(h.render(scene))
    assert pane.title == "buyer-b"
    assert [(b.direction, b.text) for b in pane.bubbles] == [("incoming", "还在吗"), ("outgoing", "在的")]
    assert pane.composer_text == ""


def test_badge_glyph_ignores_background_outside_badge():
    h = Harness()
    frame = draw_scene(Scene(rows=[SRow("a", "b", badge=None)]), h.profile)
    crop = imageops.crop(frame, h.profile.field_box(0, "badge"))
    glyph = h.analyzer.badge_ocr_image(crop)
    assert all(v == 255 for v in glyph.convert("L").getdata())
