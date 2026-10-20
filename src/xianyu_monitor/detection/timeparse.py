"""Parse the relative time label shown in conversation rows.

Relative labels age on their own ("刚刚" -> "1分钟前"); aging is never an event. A label is a
*recency refresh* only when the parsed age is clearly younger than the previous label's age
plus the elapsed wall-clock time between the two observations.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from xianyu_monitor.detection.textnorm import normalize


@dataclass(frozen=True)
class RelTime:
    kind: str  # just_now | minutes | hours | yesterday | date | clock | unknown
    # Lower bound of the age implied by the label, in minutes.
    age_minutes: float | None
    # Width of the label's uncertainty band: "5分钟前" is 5..6 minutes old.
    resolution_minutes: float = 1.0

    @property
    def max_age_minutes(self) -> float | None:
        return None if self.age_minutes is None else self.age_minutes + self.resolution_minutes


_MIN = re.compile(r"(\d+)\s*(分钟前|分鐘前|mins?\s*ago|minutes?\s*ago)")
_HOUR = re.compile(r"(\d+)\s*(小时前|小時前|hours?\s*ago|hrs?\s*ago)")
_DATE = re.compile(r"^(\d{1,2})[-/月](\d{1,2})日?$")
_FULL_DATE = re.compile(r"^(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?$")
_CLOCK = re.compile(r"^(\d{1,2})[:：](\d{2})$")


def parse_relative(text: str | None, now: dt.datetime | None = None) -> RelTime:
    value = normalize(text).replace(" ", "") if text else ""
    if not value:
        return RelTime("unknown", None)
    if value in {"刚刚", "剛剛", "justnow", "now"}:
        # How long the site keeps showing "just now" is not measured; assume up to 3 minutes.
        return RelTime("just_now", 0.0, 3.0)
    match = _MIN.search(value) or _MIN.search(normalize(text))
    if match:
        return RelTime("minutes", float(match.group(1)), 1.0)
    match = _HOUR.search(value) or _HOUR.search(normalize(text))
    if match:
        return RelTime("hours", float(match.group(1)) * 60.0, 60.0)
    if value in {"昨天", "yesterday"}:
        since_midnight = 0.0
        if now is not None:
            since_midnight = (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 60.0
        return RelTime("yesterday", since_midnight, 24 * 60.0)
    if now is not None:
        match = _CLOCK.match(value)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
            if hour < 24 and minute < 60:
                stamp = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if stamp > now:
                    stamp -= dt.timedelta(days=1)
                return RelTime("clock", (now - stamp).total_seconds() / 60.0, 1.0)
        match = _FULL_DATE.match(value)
        if match:
            try:
                stamp = dt.datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                return _date_age(now, stamp)
            except ValueError:
                return RelTime("unknown", None)
        match = _DATE.match(value)
        if match:
            try:
                stamp = dt.datetime(now.year, int(match.group(1)), int(match.group(2)))
            except ValueError:
                return RelTime("unknown", None)
            if stamp > now:
                stamp = stamp.replace(year=now.year - 1)
            return _date_age(now, stamp)
    return RelTime("unknown", None)


def _date_age(now: dt.datetime, day_start: dt.datetime) -> RelTime:
    day_end = day_start + dt.timedelta(days=1)
    lower = max(0.0, (now - day_end).total_seconds() / 60.0)
    upper = max(lower, (now - day_start).total_seconds() / 60.0)
    return RelTime("date", lower, upper - lower)


def is_recency_refresh(prev: RelTime, prev_at: dt.datetime, new: RelTime, new_at: dt.datetime,
                       slack_minutes: float = 1.0) -> bool:
    """True when the new label is younger than the old label could possibly have aged to."""
    if prev.age_minutes is None or new.max_age_minutes is None:
        return False
    elapsed = max(0.0, (new_at - prev_at).total_seconds() / 60.0)
    earliest_possible_age_now = prev.age_minutes + elapsed
    return new.max_age_minutes < earliest_possible_age_now - slack_minutes


def is_recent(rel: RelTime, max_age_minutes: float) -> bool:
    return rel.age_minutes is not None and rel.age_minutes <= max_age_minutes
