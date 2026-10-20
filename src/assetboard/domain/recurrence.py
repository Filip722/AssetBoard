from __future__ import annotations

import calendar
from collections.abc import Container, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from assetboard.domain.months import MAX_YEAR, MIN_YEAR


class Frequency(StrEnum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"


class MonthDay(StrEnum):
    FIRST_WORKING = "first_working_day"
    LAST_WORKING = "last_working_day"


class WeekendPolicy(StrEnum):
    NONE = "none"
    PREVIOUS = "previous_working_day"
    NEXT = "next_working_day"


@dataclass(frozen=True, slots=True)
class RecurrenceRule:
    start_date: date
    freq: Frequency = Frequency.MONTHLY
    interval: int = 1
    weekdays: tuple[int, ...] = ()
    month_day: int | MonthDay = 1
    month: int = 1
    end_date: date | None = None
    max_count: int | None = None
    weekend_policy: WeekendPolicy = WeekendPolicy.NONE

    def __post_init__(self) -> None:
        if not MIN_YEAR <= self.start_date.year <= MAX_YEAR:
            raise ValueError("start_date")
        if self.end_date is not None and (
            self.end_date < self.start_date or self.end_date.year > MAX_YEAR
        ):
            raise ValueError("end_date")
        if self.interval < 1:
            raise ValueError("interval")
        if self.max_count is not None and self.max_count < 1:
            raise ValueError("max_count")
        if self.freq == Frequency.WEEKLY and (
            not self.weekdays or any(day not in range(7) for day in self.weekdays)
        ):
            raise ValueError("weekdays")
        if isinstance(self.month_day, int) and self.month_day not in (-1, *range(1, 32)):
            raise ValueError("month_day")
        if self.freq == Frequency.YEARLY and (
            not 1 <= self.month <= 12 or not isinstance(self.month_day, int) or self.month_day == -1
        ):
            raise ValueError("month")


def _working(day: date, holidays: Container[date]) -> bool:
    return day.weekday() < 5 and day not in holidays


def _month_day(year: int, month: int, choice: int | MonthDay, holidays: Container[date]) -> date:
    last = calendar.monthrange(year, month)[1]
    if isinstance(choice, int):
        return date(year, month, last if choice == -1 else min(choice, last))
    days = range(1, last + 1) if choice == MonthDay.FIRST_WORKING else range(last, 0, -1)
    for number in days:
        candidate = date(year, month, number)
        if _working(candidate, holidays):
            return candidate
    raise ValueError("no_working_day")


def _nominal_dates(rule: RecurrenceRule, holidays: Container[date]) -> Iterator[date]:
    if rule.freq == Frequency.WEEKLY:
        anchor = rule.start_date - timedelta(days=rule.start_date.weekday())
        while anchor.year <= MAX_YEAR:
            for weekday in sorted(set(rule.weekdays)):
                yield anchor + timedelta(days=weekday)
            if (date(MAX_YEAR, 12, 31) - anchor).days < rule.interval * 7:
                return
            anchor += timedelta(weeks=rule.interval)
    elif rule.freq == Frequency.MONTHLY:
        index = rule.start_date.year * 12 + rule.start_date.month - 1
        while index // 12 <= MAX_YEAR:
            year, month = divmod(index, 12)
            yield _month_day(year, month + 1, rule.month_day, holidays)
            index += rule.interval
    else:
        for year in range(rule.start_date.year, MAX_YEAR + 1, rule.interval):
            yield _month_day(year, rule.month, rule.month_day, holidays)


def occurrences(
    rule: RecurrenceRule,
    from_date: date,
    to_date: date,
    holidays: Container[date],
    *,
    limit: int | None = None,
) -> list[date]:
    """Inclusive effective dates. Count nominal dates before shifting and deduplication."""
    result: set[date] = set()
    count = 0
    if from_date > to_date:
        return []
    for nominal in _nominal_dates(rule, holidays):
        if nominal < rule.start_date:
            continue
        if nominal > (rule.end_date or date(MAX_YEAR, 12, 31)):
            break
        count += 1
        if rule.max_count is not None and count > rule.max_count:
            break
        due = nominal
        if rule.weekend_policy != WeekendPolicy.NONE:
            step = -1 if rule.weekend_policy == WeekendPolicy.PREVIOUS else 1
            while not _working(due, holidays):
                if due in (date.min, date.max):
                    raise ValueError("date_out_of_range")
                due += timedelta(days=step)
        if due > to_date:
            break
        if from_date <= due:
            result.add(due)
            if limit is not None and len(result) >= limit:
                break
    return sorted(result)
