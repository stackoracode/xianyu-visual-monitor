import datetime as dt

from xianyu_monitor.detection.textnorm import is_truncated, normalize, normalize_name, parse_badge, similarity
from xianyu_monitor.detection.timeparse import is_recency_refresh, is_recent, parse_relative

NOW = dt.datetime(2026, 10, 20, 21, 0, 0)


def test_normalization_preserves_meaning_and_ignores_noise():
    assert normalize("  ＲＥＦ－ＴＥＳＴ  ００５ ") == "ref-test 005"
    assert normalize_name("买家 _1234​5678") == "买家_12345678"
    assert similarity("ref-test-003", "ref-test-003") == 1.0
    assert similarity("ref-test-003", "ref-test-005") < 1.0


def test_truncation_detection():
    assert is_truncated("请问这个尺寸适合多大的...")
    assert is_truncated("hello…")
    assert not is_truncated("REF-TEST-005")


def test_badge_parsing():
    assert parse_badge("2") == 2
    assert parse_badge("99+") == 99
    assert parse_badge("l2") == 12
    assert parse_badge("") is None
    assert parse_badge("abc") is None


def test_relative_time_labels():
    assert parse_relative("刚刚", NOW).age_minutes == 0
    assert parse_relative("1分钟前", NOW).age_minutes == 1
    assert parse_relative("3小时前", NOW).age_minutes == 180
    assert parse_relative("10-11", NOW).kind == "date"
    assert parse_relative("20:30", NOW).age_minutes == 30
    assert parse_relative("昨天", NOW).kind == "yesterday"
    assert parse_relative("???", NOW).kind == "unknown"


def test_relative_time_aging_is_not_a_refresh():
    t0 = NOW
    t1 = NOW + dt.timedelta(seconds=70)
    assert not is_recency_refresh(parse_relative("刚刚", t0), t0, parse_relative("1分钟前", t1), t1)
    assert not is_recency_refresh(parse_relative("1分钟前", t0), t0, parse_relative("2分钟前", t1), t1)
    assert not is_recency_refresh(parse_relative("刚刚", t0), t0, parse_relative("刚刚", NOW + dt.timedelta(seconds=6)),
                                  NOW + dt.timedelta(seconds=6))


def test_relative_time_refresh_detected():
    t0 = NOW
    t1 = NOW + dt.timedelta(seconds=10)
    assert is_recency_refresh(parse_relative("8分钟前", t0), t0, parse_relative("刚刚", t1), t1)
    assert is_recency_refresh(parse_relative("10-11", t0), t0, parse_relative("刚刚", t1), t1)
    assert is_recency_refresh(parse_relative("昨天", t0), t0, parse_relative("刚刚", t1), t1)
    assert is_recent(parse_relative("5分钟前", NOW), 120)
    assert not is_recent(parse_relative("10-07", NOW), 120)
