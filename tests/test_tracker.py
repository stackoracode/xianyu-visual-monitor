"""State transitions modeled on the controlled reference sequence (synthetic values)."""

import datetime as dt

from xianyu_monitor.detection.tracker import observe
from xianyu_monitor.runtime_settings import RuntimeSettings
from xianyu_monitor.vision.analyzer import RowObservation

RT = RuntimeSettings()
T0 = dt.datetime(2026, 10, 20, 20, 55, 0)
CID = "cust-1"


def row(preview, badge=None, time="刚刚", index=0, preview_conf=0.97, badge_digest=""):
    return RowObservation(index=index, present=True, name="buyer", name_conf=0.98, preview=preview,
                          preview_conf=preview_conf, time_text=time, time_conf=0.97,
                          badge_present=badge is not None, badge_count=badge if badge != -1 else None,
                          badge_digest=badge_digest or f"b{badge}")


def step(prev, r, seconds, pane=None):
    return observe(prev, r, T0 + dt.timedelta(seconds=seconds), RT, CID, pane)


def test_reference_sequence_count1_count3_mobile_read_count1_count2():
    t1 = step(None, row("新消息", 1), 0)
    assert t1.kind == "incoming"
    t2 = step(t1.new_state, row("REF-TEST-003", 3), 100)
    assert t2.kind == "incoming" and t2.badge_before == 1 and t2.badge_after == 3
    # seller phone read: badge disappears, preview stays, time ages -> not a message
    t3 = step(t2.new_state, row("REF-TEST-003", None, "1分钟前"), 190)
    assert t3.kind == "read_elsewhere"
    t4 = step(t3.new_state, row("REF-TEST-001", 1), 330)
    assert t4.kind == "incoming"
    t5 = step(t4.new_state, row("REF-TEST-005", 2), 420)
    assert t5.kind == "incoming" and t5.badge_before == 1 and t5.badge_after == 2
    keys = {t.dedup_key for t in (t1, t2, t4, t5)}
    assert len(keys) == 4


def test_repeated_identical_text_with_badge_increase_is_new_message():
    t1 = step(None, row("好的", 1), 0)
    t2 = step(t1.new_state, row("好的", 2), 10)
    assert t2.kind == "incoming" and "repeated_or_unchanged_preview" in t2.signals


def test_same_frame_twice_produces_no_event_and_no_new_revision():
    t1 = step(None, row("hello", 1), 0)
    t2 = step(t1.new_state, row("hello", 1), 6)
    assert t2.kind is None and not t2.state_changed


def test_relative_time_aging_alone_is_not_an_event():
    t1 = step(None, row("hello", None, "刚刚"), 0)
    t2 = step(t1.new_state, row("hello", None, "1分钟前"), 65)
    assert t2.kind is None
    assert t2.state_changed  # state follows the label, but no event


def test_preview_change_without_badge_has_unknown_direction():
    t1 = step(None, row("hello", None, "5分钟前"), 0)
    t2 = step(t1.new_state, row("thanks, shipped", None, "刚刚"), 10)
    assert t2.kind == "possible" and t2.direction == "unknown"


def test_open_conversation_pane_resolves_direction():
    t1 = step(None, row("hello", None, "5分钟前"), 0)
    out = step(t1.new_state, row("thanks, shipped", None, "刚刚"), 10, pane="outgoing")
    assert out.kind == "outgoing"
    inc = step(t1.new_state, row("is it available?", None, "刚刚"), 10, pane="incoming")
    assert inc.kind == "incoming"


def test_time_refresh_with_same_preview_is_possible_message():
    t1 = step(None, row("ok", None, "30分钟前"), 0)
    t2 = step(t1.new_state, row("ok", None, "刚刚"), 20)
    assert t2.kind == "possible" and "time_refreshed" in t2.signals


def test_badge_cleared_with_preview_change_is_possible_not_read_only():
    t1 = step(None, row("first", 1), 0)
    t2 = step(t1.new_state, row("second", None), 30)
    assert t2.kind == "possible" and "read_elsewhere_with_change" in t2.signals


def test_unreadable_badge_change_is_flagged():
    t1 = step(None, row("x", -1, badge_digest="aaa"), 0)
    t2 = step(t1.new_state, row("x", -1, badge_digest="bbb"), 10)
    assert t2.kind == "possible" and "badge_changed_unreadable" in t2.signals


def test_low_confidence_preview_does_not_flap_state():
    t1 = step(None, row("hello", None), 0)
    t2 = step(t1.new_state, row("he1l0??", None, preview_conf=0.2), 10)
    assert t2.kind is None
    assert t2.new_state.preview_norm == "hello"
    assert "preview_unreadable" in t2.signals


def test_dedup_key_is_stable_for_same_transition():
    t1 = step(None, row("a", 1), 0)
    a = step(t1.new_state, row("b", 2), 10)
    b = step(t1.new_state, row("b", 2), 10)
    assert a.dedup_key == b.dedup_key
