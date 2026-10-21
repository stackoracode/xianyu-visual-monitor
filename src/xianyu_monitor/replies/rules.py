"""Keyword rule evaluation. Pure functions; no I/O.

A decision is ``reply`` only when exactly one highest-priority rule matches, the text is
complete enough, no negation precedes the keyword, no sensitive topic or attachment is
involved (unless the rule explicitly allows it) and the rule does not require review.
Everything else becomes ``review`` (owner decides) or ``none``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from xianyu_monitor.detection.textnorm import is_truncated, normalize

TEMPLATE_TOKENS = ("{customer_name}",)


@dataclass
class Rule:
    id: int
    name: str
    priority: int
    match_type: str  # contains | exact | regex
    match_mode: str  # any | all
    patterns: list[str]
    exclude_patterns: list[str]
    template: str
    cooldown_seconds: int = 3600
    scope: str = "all"  # all | new | known
    allow_sensitive: bool = False
    requires_review: bool = False
    enabled: bool = True
    trigger_type: str = "keyword"
    send_image: bool = False


@dataclass
class RuleMatch:
    rule: Rule
    keywords: list[str]


@dataclass
class Decision:
    action: str  # reply | review | none
    rule: Rule | None = None
    reasons: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    reply_text: str = ""


class RuleError(ValueError):
    pass


def validate_rule(rule: Rule) -> None:
    if rule.trigger_type not in {"keyword", "first_contact"}:
        raise RuleError("trigger_type must be keyword or first_contact")
    if not rule.patterns and rule.trigger_type == "keyword":
        raise RuleError("at least one pattern is required")
    if rule.match_type not in {"contains", "exact", "regex"}:
        raise RuleError("match_type must be contains, exact or regex")
    if rule.match_mode not in {"any", "all"}:
        raise RuleError("match_mode must be any or all")
    if rule.scope not in {"all", "new", "known"}:
        raise RuleError("scope must be all, new or known")
    if not rule.template.strip():
        raise RuleError("template must not be empty")
    if len(rule.template) > 500:
        raise RuleError("template is too long (max 500 characters)")
    if rule.match_type == "regex":
        for pattern in rule.patterns + rule.exclude_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise RuleError(f"invalid regex {pattern!r}: {exc}") from exc
    leftover = re.sub(r"\{customer_name\}", "", rule.template)
    if "{" in leftover or "}" in leftover:
        raise RuleError("template may only use the {customer_name} placeholder")


def _contains(text: str, pattern: str) -> list[int]:
    needle = normalize(pattern)
    if not needle:
        return []
    positions, start = [], 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            return positions
        positions.append(index)
        start = index + 1


def _matches(rule: Rule, text: str) -> list[tuple[str, int]]:
    """Return (keyword, position) hits, or [] if the rule does not match."""
    hits: list[tuple[str, int]] = []
    per_pattern: list[bool] = []
    for pattern in rule.patterns:
        found: list[int] = []
        if rule.match_type == "contains":
            found = _contains(text, pattern)
        elif rule.match_type == "exact":
            found = [0] if text == normalize(pattern) else []
        else:
            found = [m.start() for m in re.finditer(pattern, text, flags=re.IGNORECASE)]
        per_pattern.append(bool(found))
        hits += [(pattern, pos) for pos in found]
    if rule.match_mode == "all" and not all(per_pattern):
        return []
    if rule.match_mode == "any" and not any(per_pattern):
        return []
    return hits


def _excluded(rule: Rule, text: str) -> bool:
    for pattern in rule.exclude_patterns:
        if rule.match_type == "regex":
            if re.search(pattern, text, flags=re.IGNORECASE):
                return True
        elif _contains(text, pattern):
            return True
    return False


def _negated(text: str, position: int, negations: list[str], window: int = 4) -> bool:
    prefix = text[max(0, position - window - 6):position]
    for term in negations:
        term_norm = normalize(term)
        if not term_norm:
            continue
        idx = prefix.rfind(term_norm)
        if idx >= 0 and len(prefix) - (idx + len(term_norm)) <= window:
            return True
    return False


def render_template(template: str, customer_name: str) -> str:
    # Only literal token replacement: customer text is never interpreted as a format string.
    return template.replace("{customer_name}", customer_name)


def evaluate(text: str, rules: list[Rule], *, customer_status: str, customer_name: str,
             sensitive_terms: list[str], attachment_markers: list[str], negations: list[str],
             truncated: bool | None = None, first_contact: bool = False) -> Decision:
    raw = text or ""
    norm = normalize(raw)
    if not norm:
        return Decision("none", reasons=["empty_text"])
    for marker in attachment_markers:
        if marker and marker in raw:
            return Decision("review", reasons=["attachment_or_card"])

    scope_ok = {"all"} | ({"new"} if customer_status == "new" else {"known"})
    matches: list[RuleMatch] = []
    negated_rules: list[Rule] = []
    for rule in rules:
        if not rule.enabled or rule.scope not in scope_ok or _excluded(rule, norm):
            continue
        if rule.trigger_type == "first_contact":
            if not first_contact:
                continue
            matches.append(RuleMatch(rule, ["first_contact"]))
            continue
        hits = _matches(rule, norm)
        if not hits:
            continue
        if rule.match_type == "contains" and all(_negated(norm, pos, negations) for _, pos in hits):
            negated_rules.append(rule)
            continue
        matches.append(RuleMatch(rule, sorted({kw for kw, _ in hits})))

    if not matches:
        if negated_rules:
            return Decision("review", negated_rules[0], ["negation_before_keyword"])
        return Decision("none", reasons=["no_rule_matched"])

    matches.sort(key=lambda m: (-m.rule.priority, m.rule.id))
    top = matches[0]
    reasons: list[str] = []
    same_priority = [m for m in matches if m.rule.priority == top.rule.priority]
    if len({(m.rule.template, m.rule.send_image) for m in same_priority}) > 1:
        reasons.append("ambiguous_rules_same_priority")
    if negated_rules:
        reasons.append("another_rule_negated")
    if truncated if truncated is not None else is_truncated(raw):
        reasons.append("incomplete_text")
    if not top.rule.allow_sensitive:
        sensitive = [term for term in sensitive_terms if term and normalize(term) in norm]
        if sensitive:
            reasons.append("sensitive_topic")
    if top.rule.requires_review:
        reasons.append("rule_requires_review")
    reply_text = render_template(top.rule.template, customer_name)
    if reasons:
        return Decision("review", top.rule, reasons, top.keywords, reply_text)
    return Decision("reply", top.rule, ["rule_matched"], top.keywords, reply_text)
