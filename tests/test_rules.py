import pytest

from xianyu_monitor import runtime_settings as rs
from xianyu_monitor.replies.rules import Rule, RuleError, evaluate, render_template, validate_rule


def rule(rid=1, priority=100, patterns=("还在吗",), template="在的，{customer_name}", **kw):
    return Rule(id=rid, name=f"r{rid}", priority=priority, match_type=kw.pop("match_type", "contains"),
                match_mode=kw.pop("match_mode", "any"), patterns=list(patterns),
                exclude_patterns=list(kw.pop("exclude", [])), template=template, **kw)


def decide(text, rules, status="known", truncated=None):
    return evaluate(text, rules, customer_status=status, customer_name="买家A", sensitive_terms=rs.DEFAULT_SENSITIVE_TERMS,
                    attachment_markers=rs.DEFAULT_ATTACHMENT_MARKERS, negations=rs.DEFAULT_NEGATIONS, truncated=truncated)


def test_simple_match_renders_template_literally():
    d = decide("你好，还在吗", [rule()])
    assert d.action == "reply" and d.reply_text == "在的，买家A"


def test_priority_wins():
    d = decide("还在吗 什么时候发货", [rule(1, 10), rule(2, 50, ("发货",), "24小时内发货")])
    assert d.action == "reply" and d.rule.id == 2


def test_same_priority_different_templates_is_ambiguous():
    d = decide("还在吗 发货", [rule(1, 50), rule(2, 50, ("发货",), "other")])
    assert d.action == "review" and "ambiguous_rules_same_priority" in d.reasons


def test_exclusion():
    assert decide("还在吗？不在也没关系", [rule(exclude=["不在"])]).action == "none"


def test_negation_goes_to_review():
    d = decide("不要还在吗这种回复", [rule()])
    assert d.action == "review" and "negation_before_keyword" in d.reasons


def test_sensitive_topics_require_review_unless_allowed():
    assert "sensitive_topic" in decide("还在吗 最低多少钱", [rule()]).reasons
    assert decide("还在吗 最低多少钱", [rule(allow_sensitive=True)]).action == "reply"


def test_attachments_and_cards_require_review():
    assert decide("[图片]", [rule(patterns=("图片",))]).reasons == ["attachment_or_card"]


def test_truncated_text_requires_review():
    assert "incomplete_text" in decide("还在吗我想问一下这个...", [rule()]).reasons
    assert "incomplete_text" in decide("还在吗", [rule()], truncated=True).reasons


def test_scope_and_all_mode_and_regex():
    assert decide("还在吗", [rule(scope="new")], status="known").action == "none"
    assert decide("还在吗", [rule(scope="new")], status="new").action == "reply"
    all_rule = rule(patterns=("包邮", "还在"), match_mode="all", allow_sensitive=True)
    assert decide("还在吗", [all_rule]).action == "none"
    assert decide("还在吗 包邮吗", [all_rule]).action == "reply"
    assert decide("order 12345 status", [rule(patterns=(r"order\s+\d+",), match_type="regex")]).action == "reply"


def test_requires_review_flag_and_disabled_rules():
    assert decide("还在吗", [rule(requires_review=True)]).reasons == ["rule_requires_review"]
    assert decide("还在吗", [rule(enabled=False)]).action == "none"


def test_template_cannot_inject_format_fields():
    with pytest.raises(RuleError):
        validate_rule(rule(template="{0.__class__}"))
    assert render_template("hi {customer_name}", "{evil}") == "hi {evil}"
    with pytest.raises(RuleError):
        validate_rule(rule(match_type="regex", patterns=("([",)))
    validate_rule(rule())
