import re
from pathlib import Path

from xianyu_monitor.i18n import catalog, translate
from xianyu_monitor.runtime_settings import GROUPS, SPECS

ROOT = Path(__file__).resolve().parents[1] / "src" / "xianyu_monitor"


def test_catalogs_have_identical_keys():
    assert set(catalog("en")) == set(catalog("zh"))
    assert all(catalog("en").values()) and all(catalog("zh").values())


def test_all_static_keys_used_in_templates_and_code_exist():
    pattern = re.compile(r"""\b(?:t|translate)\(\s*['"]([a-zA-Z0-9_.]+)['"]""")
    sources = list((ROOT / "web" / "templates").glob("*.html")) + [ROOT / "web" / "app.py", ROOT / "notify" / "emailer.py"]
    keys = set()
    for path in sources:
        keys |= {k for k in pattern.findall(path.read_text(encoding="utf-8")) if not k.endswith(".")}
    missing = sorted(k for k in keys if k not in catalog("en"))
    assert not missing, missing


def test_dynamic_keys_exist():
    en = catalog("en")
    for spec in SPECS:
        assert f"setting.{spec.key}" in en
    for group in GROUPS:
        assert f"settings.group.{group}" in en
    for status in ("baseline", "known", "new", "ignored", "system", "merged"):
        assert f"status.{status}" in en
    for kind in ("incoming", "possible", "outgoing", "read_elsewhere"):
        assert f"kind.{kind}" in en
    for status in ("review", "planned", "approved", "queued", "sending", "sent_verified", "sent_unverified",
                   "dry_run", "rejected", "skipped", "failed", "cancelled"):
        assert f"intent.{status}" in en


def test_english_is_default_and_fallback():
    assert translate("nav.overview") == "Overview"
    assert translate("nav.overview", "zh") == "概览"
    assert translate("nav.overview", "fr") == "Overview"
    assert translate("reason.screen_not_ready:blank") == "screen_not_ready:blank"
    assert "张三" in translate("email.new_customer.subject", "en", name="张三")
