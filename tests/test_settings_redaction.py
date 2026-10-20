import pytest

from xianyu_monitor.redact import pseudonym, redact_mapping, redact_text
from xianyu_monitor.runtime_settings import SPEC_BY_KEY, RuntimeSettings, SettingError, coerce
from xianyu_monitor.settings import load_settings, parse_env_file


def test_env_file_parsing_and_precedence(tmp_path):
    env_file = tmp_path / "monitor.env"
    env_file.write_text('# comment\nXVM_WEB_PORT=9000\nXVM_NOTIFY_TO="a@example.com, b@example.com"\n'
                        "export XVM_LOG_JSON=false\n", encoding="utf-8")
    assert parse_env_file(env_file)["XVM_WEB_PORT"] == "9000"
    settings = load_settings({"XVM_ENV_FILE": str(env_file), "XVM_WEB_PORT": "9100"})
    assert settings.web_port == 9100  # real environment wins
    assert settings.notify_to == ["a@example.com", "b@example.com"]
    assert settings.log_json is False


def test_static_settings_redaction():
    settings = load_settings({"XVM_DATABASE_URL": "mysql+aiomysql://xvm:s3cret@db:3306/x", "XVM_SMTP_PASSWORD": "pw",
                              "XVM_SECRET_KEY": "k" * 40, "XVM_NOTIFY_TO": "owner@example.com"})
    red = settings.redacted()
    assert "s3cret" not in str(red) and red["smtp_password"] == "***" and red["secret_key"] == "***"
    assert "owner@example.com" not in str(red)
    assert "s3cret" not in repr(settings)


def test_safe_runtime_defaults():
    rt = RuntimeSettings()
    assert rt.monitor_mode == "baseline"
    assert rt.dry_run is True
    assert rt.notifications_enabled is False
    assert rt.reply_mode == "off"
    assert rt.reply_send_enabled is False
    assert not rt.auto_reply_allowed


def test_runtime_coercion_and_bounds():
    assert coerce(SPEC_BY_KEY["capture_interval_seconds"], "7.5") == 7.5
    assert coerce(SPEC_BY_KEY["dry_run"], "on") is True
    assert coerce(SPEC_BY_KEY["system_conversation_names"], "a\nb\n\n") == ["a", "b"]
    with pytest.raises(SettingError):
        coerce(SPEC_BY_KEY["capture_interval_seconds"], "0.1")
    with pytest.raises(SettingError):
        coerce(SPEC_BY_KEY["reply_mode"], "always")


def test_text_redaction():
    text = "login password=hunter2 to smtp://user:pw@mail.example.com from owner@example.com at 192.0.2.10"
    out = redact_text(text)
    assert "hunter2" not in out and "owner@example.com" not in out and "192.0.2.10" not in out and ":pw@" not in out
    assert redact_mapping({"smtp_password": "x", "nested": {"token": "y"}}) == {"smtp_password": "***", "nested": {"token": "***"}}
    assert pseudonym("用户_1") == pseudonym("用户_1") != pseudonym("用户_2")
