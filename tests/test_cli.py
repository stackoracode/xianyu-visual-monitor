import json

from xianyu_monitor.cli import build_parser, main


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
