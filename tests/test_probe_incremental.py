"""Incremental safety OCR must answer exactly like a fresh full read of the probe region."""

import random

from PIL import ImageDraw

from synth import Harness, Scene, SRow
from xianyu_monitor.runtime_settings import RuntimeSettings
from xianyu_monitor.vision.analyzer import FrameAnalyzer

RT = RuntimeSettings({"full_validation_every_n_frames": 1000, "challenge_probe_every_n_frames": 1000})
PROMPT = "请完成安全验证"


def base():
    h = Harness()
    frame = h.render(Scene(rows=[SRow("买家甲", "还在吗", "刚刚", avatar=1), SRow("买家乙", "谢谢", "5分钟前", avatar=2)]))
    return h, frame


def patch(frame, y0, y1, x0=300, x1=420, fill=(30, 30, 30)):
    out = frame.copy()
    ImageDraw.Draw(out).rectangle((x0, y0, x1, y1 - 1), fill=fill)
    return out


def probe_top(h):
    return h.profile.challenge_probe[1]


async def test_small_change_reads_only_the_changed_band():
    h, frame = base()
    assert await h.analyzer._probe_challenge(frame, RT) == []
    calls = h.fake.calls
    assert await h.analyzer._probe_challenge(frame.copy(), RT) == []  # identical pixels: reused
    assert h.fake.calls == calls and h.analyzer.probe_stats["reused"] == 1
    assert await h.analyzer._probe_challenge(patch(frame, 600, 612), RT) == []
    assert h.analyzer.probe_stats == {"full": 1, "incremental": 1, "reused": 1}
    assert h.fake.calls == calls + 1


async def test_prompt_in_a_changed_band_is_detected_and_positive_state_rereads_fully():
    h, frame = base()
    await h.analyzer._probe_challenge(frame, RT)
    changed = patch(frame, 400, 414)
    line = 400 - probe_top(h)
    h.fake.register_lines(h.analyzer.probe_crop(changed), [(line, line + 14, PROMPT)])
    hits = await h.analyzer._probe_challenge(changed, RT)
    assert "安全验证" in hits and h.analyzer.probe_stats["incremental"] == 1
    # A positive answer is never extended incrementally: the next change is read in full.
    later = patch(changed, 620, 630, x0=500, x1=520)
    h.fake.register_lines(h.analyzer.probe_crop(later), [(line, line + 14, PROMPT)])
    assert "安全验证" in await h.analyzer._probe_challenge(later, RT)
    assert h.analyzer.probe_stats["full"] == 2


async def test_band_contains_the_whole_line_when_only_its_edge_changes():
    h, frame = base()
    await h.analyzer._probe_challenge(frame, RT)
    line = 460 - probe_top(h)
    changed = patch(frame, 460 + 14, 460 + 16)  # only the bottom rows of a 16px line changed
    h.fake.register_lines(h.analyzer.probe_crop(changed), [(line, line + 16, PROMPT)])
    assert "安全验证" in await h.analyzer._probe_challenge(changed, RT)
    assert h.analyzer.probe_stats["incremental"] == 1


async def test_large_changes_keyword_changes_and_periodic_refresh_read_fully():
    h, frame = base()
    await h.analyzer._probe_challenge(frame, RT)
    top, bottom = h.profile.challenge_probe[1], h.profile.challenge_probe[3]
    await h.analyzer._probe_challenge(patch(frame, top, top + int((bottom - top) * 0.8)), RT)
    assert h.analyzer.probe_stats["full"] == 2
    other = RT.with_overrides(challenge_keywords=["different"])
    await h.analyzer._probe_challenge(patch(frame, 600, 610), other)
    assert h.analyzer.probe_stats["full"] == 3
    current = frame
    for i in range(FrameAnalyzer.PROBE_FULL_EVERY_N_READS + 2):
        current = patch(current, 600, 610, x0=300 + 2 * i, x1=301 + 2 * i, fill=(i, 0, 0))
        await h.analyzer._probe_challenge(current, RT)
    assert h.analyzer.probe_stats["incremental"] == FrameAnalyzer.PROBE_FULL_EVERY_N_READS
    assert h.analyzer.probe_stats["full"] == 5


async def test_random_sequences_agree_with_fresh_full_reads():
    rng = random.Random(7)
    h, frame = base()
    top, bottom = h.profile.challenge_probe[1], h.profile.challenge_probe[3]
    current = frame
    prompt_rows: list[tuple[int, int]] = []
    for _ in range(25):
        y = rng.randrange(top, bottom - 20)
        current = patch(current, y, y + rng.randrange(2, 18), x0=rng.randrange(50, 900), x1=rng.randrange(900, 1080),
                        fill=(rng.randrange(256), rng.randrange(256), rng.randrange(256)))
        if rng.random() < 0.3:
            prompt_rows.append((y - top, y - top + 14))
        # Prompt lines stay visible only while their pixels are unchanged since drawing.
        lines = [(a, b, PROMPT) for a, b in prompt_rows[-1:]]
        h.fake.register_lines(h.analyzer.probe_crop(current), lines)
        incremental = await h.analyzer._probe_challenge(current, RT)
        fresh = await FrameAnalyzer(h.profile, h.pool)._probe_challenge(current, RT)
        assert incremental == fresh
    assert h.analyzer.probe_stats["incremental"] > 0
