import json
from types import SimpleNamespace

import pytest

from xianyu_monitor.cli import build_parser, main


@pytest.mark.parametrize("mismatch", [None, "cold", "warm", "warm_score", "warm_missing"])
def test_image_benchmark_checks_both_cache_states_and_resets_each_frame(monkeypatch, mismatch):
    from xianyu_monitor.cli import _benchmark_image_matches
    from xianyu_monitor.vision import reply_image

    expected = reply_image.ImageMatch((10, 20, 110, 80), .98)
    changed = reply_image.ImageMatch((10, 21, 110, 81), .98)
    calls = []

    def exhaustive(*args):
        assert not reply_image._THUMBS
        calls.append("exhaustive")
        return expected

    def fast(*args):
        state = "warm" if reply_image._THUMBS else "cold"
        calls.append(state)
        reply_image._THUMBS[("fixture",)] = object()
        if state == mismatch:
            return changed
        if state == "warm" and mismatch == "warm_score":
            return reply_image.ImageMatch(expected.box, .97)
        if state == "warm" and mismatch == "warm_missing":
            return None
        return expected

    monkeypatch.setattr(reply_image, "_THUMBS", {})
    monkeypatch.setattr(reply_image, "find_outgoing_image_exhaustive", exhaustive)
    monkeypatch.setattr(reply_image, "find_outgoing_image", fast)
    profile = SimpleNamespace(message_pane=(0, 0, 200, 100), image_match_threshold=.92)
    for _ in range(2):
        report = _benchmark_image_matches(None, None, profile)
        assert report["image_match_cold_identical"] == (mismatch != "cold")
        assert report["image_match_warm_identical"] == (mismatch not in {"warm", "warm_score", "warm_missing"})
        assert report["image_match_identical"] == (mismatch is None)
        assert report["image_match_found"]
    assert calls == ["exhaustive", "cold", "warm"] * 2


def test_image_benchmark_accepts_agreement_on_absence(monkeypatch):
    from xianyu_monitor.cli import _benchmark_image_matches
    from xianyu_monitor.vision import reply_image

    monkeypatch.setattr(reply_image, "find_outgoing_image_exhaustive", lambda *a: None)
    monkeypatch.setattr(reply_image, "find_outgoing_image", lambda *a: None)
    report = _benchmark_image_matches(None, None, SimpleNamespace(message_pane=None, image_match_threshold=.92))
    assert report["image_match_identical"] and not report["image_match_found"]


def test_parser_has_all_commands():
    parser = build_parser()
    for command in ("web", "worker", "migrate", "create-admin", "check-config", "discover-display", "capture-once",
                    "analyze-image", "import-baseline", "import-rules", "load-calibration", "smtp-test",
                    "export-diagnostics"):
        assert parser.parse_args([command] + {"create-admin": ["--username", "a"], "capture-once": ["--out", "x"],
                                              "analyze-image": ["img", "--profile", "p"], "import-baseline": ["f"],
                                              "import-rules": ["f"], "load-calibration": ["f"],
                                              "export-diagnostics": ["--out", "x"]}.get(command, []))


def test_check_config_runs_without_services(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("XVM_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    monkeypatch.setenv("XVM_OCR_BACKEND", "fake")
    monkeypatch.setenv("XVM_SMTP_PASSWORD", "topsecret")
    assert main(["check-config"]) == 0
    out = capsys.readouterr().out
    report = json.loads(out)
    assert "topsecret" not in out and report["checks"]["ocr"] == "fake: ok"


def test_analyze_image_offline(tmp_path, capsys):
    from synth import EXAMPLE, Scene, SRow, base_profile_dict, draw_scene
    from xianyu_monitor.vision.profile import Profile

    image = tmp_path / "frame.png"
    draw_scene(Scene(rows=[SRow("a", "b", badge=1)]), Profile.model_validate(base_profile_dict())).save(image)
    assert EXAMPLE.is_file()
    # tesseract is usually absent in unit-test environments; the fake backend is not offered by the CLI.
    import shutil
    if shutil.which("tesseract") is None:
        return
    assert main(["analyze-image", str(image), "--profile", str(EXAMPLE), "--ocr", "tesseract"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] in {"ok", "layout_mismatch"}
