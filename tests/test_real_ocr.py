"""Optional: real OCR on synthetic frames drawn with an installed CJK font.

Runs only where RapidOCR (or Tesseract with chi_sim) and a CJK font are installed, e.g. the
ARM64 Ubuntu 20.04 CI job. It validates the OCR integration path on *synthetic* text; it is
not an accuracy measurement on the real Xianyu UI.
"""

import datetime as dt
import glob
import importlib.util
import shutil

import pytest
from PIL import Image, ImageDraw, ImageFont

import synth
from synth import Scene, SRow, base_profile_dict, draw_scene
from xianyu_monitor.detection.textnorm import normalize, similarity
from xianyu_monitor.ocr.pool import OcrPool
from xianyu_monitor.runtime_settings import RuntimeSettings
from xianyu_monitor.vision.analyzer import FrameAnalyzer
from xianyu_monitor.vision.profile import Profile

pytestmark = pytest.mark.real_ocr

FONT_GLOBS = ["/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
              "/usr/share/fonts/**/*CJK*Regular*.tt*", "/usr/share/fonts/**/wqy*.tt*"]


def cjk_font():
    for pattern in FONT_GLOBS:
        hits = sorted(glob.glob(pattern, recursive=True))
        if hits:
            return ImageFont.truetype(hits[0], 14)
    return None


BACKENDS = []
if importlib.util.find_spec("rapidocr_onnxruntime") is not None:
    BACKENDS.append("rapidocr")
if shutil.which("tesseract"):
    BACKENDS.append("tesseract")


@pytest.mark.parametrize("backend", BACKENDS or ["none"])
async def test_real_ocr_reads_synthetic_rows(backend):
    if backend == "none":
        pytest.skip("no OCR backend installed")
    font = cjk_font()
    if font is None:
        pytest.skip("no CJK font installed")
    synth.REAL_FONT = font
    try:
        profile = Profile.model_validate(base_profile_dict())
        rows = [SRow("用户_20261020", "REF-TEST-005", "刚刚", badge=2, avatar=1),
                SRow("闲小蜜", "[卡片消息]", "27分钟前", avatar=2),
                SRow("buyer-test", "还在吗", "10-11", avatar=3)]
        frame = draw_scene(Scene(rows=rows, overall=4), profile)
        pool = OcrPool(backend, use_processes=False)
        analyzer = FrameAnalyzer(profile, pool)
        obs = await analyzer.analyze(frame, dt.datetime(2026, 10, 20, 21), RuntimeSettings(), force_full=True)
        pool.close()
    finally:
        synth.REAL_FONT = None
    report = []
    good = 0
    for expected, row in zip(rows, obs.rows, strict=False):
        for field, value in (("name", row.name), ("preview", row.preview), ("time", row.time_text)):
            want = getattr(expected, field if field != "time" else "time")
            score = similarity(normalize(want).replace(" ", ""), normalize(value).replace(" ", ""))
            good += score >= 0.8
            report.append((field, want, value, round(score, 2)))
    print(backend, report, "badge:", obs.rows[0].badge_count, "overall:", obs.overall_unread, "ocr_ms:", obs.ocr_ms)
    assert obs.status == "ok"
    assert obs.rows[0].badge_present and not obs.rows[2].badge_present
    assert good >= 7, report  # at least 7 of 9 fields read correctly


@pytest.mark.parametrize("backend", BACKENDS or ["none"])
async def test_real_ocr_reads_every_wrapped_reply_line(backend):
    if backend == "none":
        pytest.skip("no OCR backend installed")
    font = cjk_font()
    if font is None:
        pytest.skip("no CJK font installed")
    image = Image.new("RGB", (360, 75), (248, 228, 8))
    draw = ImageDraw.Draw(image)
    lines = ["已收到在售咨询", "卖家会确认当前状态"]
    for index, text in enumerate(lines):
        draw.text((12, 10 + index * 27), text, font=font, fill=(20, 20, 20))
    pool = OcrPool(backend, use_processes=False)
    try:
        result = (await pool.recognize([("block:wrapped", image)]))["block:wrapped"]
    finally:
        pool.close()
    actual = normalize(result.text).replace(" ", "")
    assert similarity(actual, "".join(lines)) >= 0.9, (backend, result.text)
    assert "状态" in actual, (backend, result.text)
