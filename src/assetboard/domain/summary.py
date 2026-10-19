"""The month summary (F2.4): planned, actual and difference per category, totals and KPIs.

Pure: no I/O, no clock. Amounts are integer minor units (``Money``) in one currency, the
workspace's base currency; nothing is rounded except the savings rate, which is rounded half up
(away from zero) to whole basis points.

**Blocks** (docs/specs/00-overview.md §3.3): expense/regular is "expense" (spent),
expense/savings "saved", income/regular "income" (earned), income/savings "withdrawn" (from
savings). Transfers are in no block and in no total or KPI.

**Difference** (§3.5: positive is always favourable): expense ``planned - actual`` (under
budget); income ``actual - planned`` (more than planned). For the savings blocks the owner
decided (F2.4, 2026-10-18) to keep "positive is favourable" rather than follow the kind:
saved ``actual - planned`` (saved more than planned), withdrawn ``planned - actual``
(withdrew less than planned).

**KPIs**: earned = income actual, spent = expense actual, saved = saved actual,
``net = earned - spent - saved`` (withdrawals are shown as their own block only),
``savings_rate = (earned - spent) / earned`` in basis points (and, for display, in tenths of a
percent, each rounded once from the exact ratio), ``None`` when earned is 0.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from assetboard.domain.categories import Kind, Treatment
from assetboard.domain.money import Money

BlockKey = Literal["expense", "saved", "income", "withdrawn"]
BLOCK_ORDER: tuple[BlockKey, ...] = ("expense", "saved", "income", "withdrawn")
SortOrder = Literal["actual", "manual"]
SORT_ORDERS: tuple[SortOrder, ...] = ("actual", "manual")
BASIS_POINTS = 10_000


@dataclass(frozen=True, slots=True)
class SummaryCategory:
    id: UUID
    kind: str
    treatment: str
    archived: bool = False


@dataclass(frozen=True, slots=True)
class Entry:
    """One non-deleted transaction of the month (its direction equals its category's kind)."""

    category_id: UUID
    amount: Money


@dataclass(frozen=True, slots=True)
class PlanLine:
    category_id: UUID
    planned: Money


@dataclass(frozen=True, slots=True)
class SummaryRow:
    category_id: UUID
    planned: Money
    actual: Money
    difference: Money


@dataclass(frozen=True, slots=True)
class SummaryBlock:
    key: BlockKey
    rows: tuple[SummaryRow, ...]
    planned: Money
    actual: Money
    difference: Money


@dataclass(frozen=True, slots=True)
class Kpis:
    earned: Money
    spent: Money
    saved: Money
    net: Money
    savings_rate_bp: int | None  # None: nothing earned
    savings_rate_tenths: int | None  # the same rate for display, in tenths of a percent


@dataclass(frozen=True, slots=True)
class MonthSummary:
    currency: str
    blocks: tuple[SummaryBlock, ...]  # always the four, in BLOCK_ORDER
    kpis: Kpis

    def block(self, key: BlockKey) -> SummaryBlock:
        return next(b for b in self.blocks if b.key == key)

    def row(self, category_id: UUID) -> SummaryRow | None:
        return next((r for b in self.blocks for r in b.rows if r.category_id == category_id), None)


def block_of(kind: str, treatment: str) -> BlockKey | None:
    """The block of a category; ``None`` for transfers (never counted)."""
    if treatment == Treatment.TRANSFER:
        return None
    if kind == Kind.EXPENSE:
        return "saved" if treatment == Treatment.SAVINGS else "expense"
    if kind == Kind.INCOME:
        return "withdrawn" if treatment == Treatment.SAVINGS else "income"
    raise ValueError(f"unknown kind: {kind}")


def difference(key: BlockKey, planned: int, actual: int) -> int:
    """The favourable-is-positive difference of a row or block (see the module docstring)."""
    if key in ("expense", "withdrawn"):
        return planned - actual
    return actual - planned


def _ratio(earned: int, spent: int, scale: int) -> int | None:
    """``(earned - spent) / earned * scale``, rounded once, half away from zero (exact)."""
    if earned == 0:
        return None
    numerator = (earned - spent) * scale
    sign = -1 if (numerator < 0) != (earned < 0) else 1
    quotient, remainder = divmod(abs(numerator), abs(earned))
    if 2 * remainder >= abs(earned):
        quotient += 1
    return sign * quotient


def savings_rate_bp(earned: int, spent: int) -> int | None:
    """``(earned - spent) / earned`` in basis points, rounded half away from zero.

    Exact integer arithmetic; ``None`` when nothing was earned (the rate is undefined).
    """
    return _ratio(earned, spent, BASIS_POINTS)


def savings_rate_tenths(earned: int, spent: int) -> int | None:
    """The savings rate in tenths of a percent, for display ("-43.3%"): rounded **once**, from
    the exact ratio, half away from zero (rounding the basis points again would turn
    12.3495% into 12.4%). ``None`` when nothing was earned."""
    return _ratio(earned, spent, 1000)


def summarize_month(
    transactions: Iterable[Entry],
    budget_lines: Iterable[PlanLine],
    categories: Sequence[SummaryCategory],
    *,
    currency: str,
    sort: SortOrder = "actual",
) -> MonthSummary:
    """The month's summary.

    ``categories`` are the workspace's categories **in the user's order** (archived included);
    every transaction and line must reference one of them. Rows: every non-transfer category
    with a planned line or actuals, and every active non-transfer category (so it can be
    planned); a category with actuals but no line has planned 0. ``sort="actual"`` orders the
    rows of each block by actual, largest first (ties: the user's order); ``"manual"`` keeps the
    user's order.
    """
    if sort not in SORT_ORDERS:
        raise ValueError(f"unknown sort: {sort}")
    known = {c.id: c for c in categories}
    actual: dict[UUID, int] = {}
    planned: dict[UUID, int] = {}
    for entry in transactions:
        _check(entry.amount, currency, entry.category_id, known)
        actual[entry.category_id] = actual.get(entry.category_id, 0) + entry.amount.amount_minor
    for line in budget_lines:
        _check(line.planned, currency, line.category_id, known)
        planned[line.category_id] = planned.get(line.category_id, 0) + line.planned.amount_minor
    rows: dict[BlockKey, list[tuple[int, SummaryRow]]] = {key: [] for key in BLOCK_ORDER}
    for position, category in enumerate(categories):
        key = block_of(category.kind, category.treatment)
        if key is None:
            continue
        has_data = category.id in actual or category.id in planned
        if category.archived and not has_data:
            continue
        p, a = planned.get(category.id, 0), actual.get(category.id, 0)
        row = SummaryRow(
            category_id=category.id,
            planned=Money(p, currency),
            actual=Money(a, currency),
            difference=Money(difference(key, p, a), currency),
        )
        rows[key].append((position, row))
    blocks: list[SummaryBlock] = []
    for key in BLOCK_ORDER:
        ordered = rows[key]
        if sort == "actual":
            ordered = sorted(ordered, key=lambda item: (-item[1].actual.amount_minor, item[0]))
        block_rows = tuple(row for _, row in ordered)
        p = sum(r.planned.amount_minor for r in block_rows)
        a = sum(r.actual.amount_minor for r in block_rows)
        blocks.append(
            SummaryBlock(
                key=key,
                rows=block_rows,
                planned=Money(p, currency),
                actual=Money(a, currency),
                difference=Money(difference(key, p, a), currency),
            )
        )
    totals = {b.key: b.actual.amount_minor for b in blocks}
    earned, spent, saved = totals["income"], totals["expense"], totals["saved"]
    kpis = Kpis(
        earned=Money(earned, currency),
        spent=Money(spent, currency),
        saved=Money(saved, currency),
        net=Money(earned - spent - saved, currency),
        savings_rate_bp=savings_rate_bp(earned, spent),
        savings_rate_tenths=savings_rate_tenths(earned, spent),
    )
    return MonthSummary(currency=currency, blocks=tuple(blocks), kpis=kpis)


def _check(
    amount: Money, currency: str, category_id: UUID, known: Mapping[UUID, SummaryCategory]
) -> None:
    if amount.currency != currency:
        raise ValueError(f"amount in {amount.currency}, summary in {currency}")
    if category_id not in known:
        raise ValueError(f"unknown category {category_id}")
