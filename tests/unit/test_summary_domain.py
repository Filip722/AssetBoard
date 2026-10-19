"""The month summary (F2.4): the golden example, blocks, variance signs, sorting, KPIs, and
property tests (totals equal the sum of rows, transfers never appear, the sign convention)."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.money import Money
from assetboard.domain.summary import (
    BLOCK_ORDER,
    Entry,
    PlanLine,
    SummaryCategory,
    block_of,
    difference,
    savings_rate_bp,
    savings_rate_tenths,
    summarize_month,
)


def _eur(minor: int) -> Money:
    return Money(minor, "EUR")


def _category(kind: str = "expense", treatment: str = "regular", archived: bool = False):
    return SummaryCategory(uuid4(), kind, treatment, archived)


# The golden example (spec F2.4)


def test_golden_example() -> None:
    food, travel = _category(), _category()
    salary = _category("income")
    summary = summarize_month(
        [
            Entry(food.id, _eur(5000)),
            Entry(food.id, _eur(50000)),
            Entry(travel.id, _eur(432300)),
            Entry(salary.id, _eur(340000)),
        ],
        [
            PlanLine(food.id, _eur(55000)),
            PlanLine(travel.id, _eur(535000)),
            PlanLine(salary.id, _eur(300000)),
        ],
        [food, travel, salary],
        currency="EUR",
    )
    rows = {r.category_id: r for b in summary.blocks for r in b.rows}
    assert (rows[food.id].actual, rows[food.id].difference) == (_eur(55000), _eur(0))
    assert rows[travel.id].difference == _eur(102700)  # 5 350.00 - 4 323.00
    assert rows[salary.id].difference == _eur(40000)  # +400.00
    expense = summary.block("expense")
    assert (expense.planned, expense.actual, expense.difference) == (
        _eur(590000),
        _eur(432300 + 55000),
        _eur(102700),
    )
    # Independent hand calculation: earned 3 400.00, spent 550.00 + 4 323.00 = 4 873.00,
    # saved 0.00; net 3 400 - 4 873 - 0 = -1 473.00; rate -1 473 / 3 400 = -0.4332352...,
    # i.e. -4 332.35 bp, rounded half away from zero: -4 332 bp.
    kpis = summary.kpis
    assert (kpis.earned, kpis.spent, kpis.saved) == (_eur(340000), _eur(487300), _eur(0))
    assert kpis.net == _eur(-147300)
    assert kpis.savings_rate_bp == -4332
    assert kpis.savings_rate_tenths == -433  # -43.3 %, rounded once from the exact ratio


# Blocks and signs


@pytest.mark.parametrize(
    ("kind", "treatment", "block"),
    [
        ("expense", "regular", "expense"),
        ("expense", "savings", "saved"),
        ("expense", "transfer", None),
        ("income", "regular", "income"),
        ("income", "savings", "withdrawn"),
        ("income", "transfer", None),
    ],
)
def test_each_treatment_has_its_block(kind: str, treatment: str, block: str | None) -> None:
    assert block_of(kind, treatment) == block


@pytest.mark.parametrize(
    ("key", "planned", "actual", "expected"),
    [
        ("expense", 100, 80, 20),  # under budget: favourable
        ("expense", 100, 120, -20),
        ("income", 100, 120, 20),  # more than planned: favourable
        ("income", 100, 80, -20),
        ("saved", 100, 120, 20),  # saved more than planned: favourable (owner, P1)
        ("saved", 100, 80, -20),
        ("withdrawn", 100, 80, 20),  # withdrew less than planned: favourable (owner, P1)
        ("withdrawn", 100, 120, -20),
    ],
)
def test_positive_difference_is_always_favourable(
    key: str, planned: int, actual: int, expected: int
) -> None:
    assert difference(key, planned, actual) == expected  # type: ignore[arg-type]


def test_rows_cover_planned_actual_and_active_categories_only() -> None:
    planned_only, actual_only, idle = _category(), _category(), _category()
    old_with_actual, old_idle = _category(archived=True), _category(archived=True)
    moving = _category(treatment="transfer")
    summary = summarize_month(
        [Entry(actual_only.id, _eur(700)), Entry(old_with_actual.id, _eur(5)),
         Entry(moving.id, _eur(999))],
        [PlanLine(planned_only.id, _eur(100)), PlanLine(moving.id, _eur(50))],
        [planned_only, actual_only, idle, old_with_actual, old_idle, moving],
        currency="EUR",
        sort="manual",
    )  # fmt: skip
    rows = summary.block("expense").rows
    assert [r.category_id for r in rows] == [
        planned_only.id, actual_only.id, idle.id, old_with_actual.id,
    ]  # fmt: skip
    assert rows[1].planned == _eur(0)  # actuals without a line: planned 0, never hidden
    assert summary.kpis.spent == _eur(705)  # the transfer counts nowhere


def test_sorting_by_actual_breaks_ties_by_the_users_order() -> None:
    a, b, c, d = (_category() for _ in range(4))
    entries = [Entry(a.id, _eur(5)), Entry(b.id, _eur(9)), Entry(c.id, _eur(5))]
    by_actual = summarize_month(entries, [], [a, b, c, d], currency="EUR")
    assert [r.category_id for r in by_actual.block("expense").rows] == [b.id, a.id, c.id, d.id]
    manual = summarize_month(entries, [], [a, b, c, d], currency="EUR", sort="manual")
    assert [r.category_id for r in manual.block("expense").rows] == [a.id, b.id, c.id, d.id]


def test_withdrawals_are_their_own_block_and_not_in_net() -> None:
    salary, withdrawal = _category("income"), _category("income", "savings")
    saving = _category("expense", "savings")
    summary = summarize_month(
        [Entry(salary.id, _eur(1000)), Entry(withdrawal.id, _eur(300)),
         Entry(saving.id, _eur(200))],
        [PlanLine(withdrawal.id, _eur(100))],
        [salary, withdrawal, saving],
        currency="EUR",
    )  # fmt: skip
    assert summary.block("withdrawn").actual == _eur(300)
    assert summary.block("withdrawn").difference == _eur(-200)
    assert (summary.kpis.earned, summary.kpis.saved) == (_eur(1000), _eur(200))
    assert summary.kpis.net == _eur(800)  # 1 000 - 0 - 200
    assert summary.kpis.savings_rate_bp == 10000


@pytest.mark.parametrize(
    ("earned", "spent", "rate"),
    [
        (0, 0, None),
        (0, 500, None),
        (1000, 0, 10000),
        (1000, 1000, 0),
        (3, 1, 6667),  # 6 666.67 bp
        (3, 2, 3333),  # 3 333.33 bp
        (20000, 19999, 1),  # 0.5 bp rounds up
        (20000, 20001, -1),  # -0.5 bp rounds away from zero
        (340000, 487300, -4332),
    ],
)
def test_savings_rate_in_basis_points(earned: int, spent: int, rate: int | None) -> None:
    assert savings_rate_bp(earned, spent) == rate


@pytest.mark.parametrize(
    ("earned", "spent", "tenths"),
    [
        (0, 1, None),
        (340000, 487300, -433),  # -43.32 %
        (200000, 175301, 123),  # 12.3495 %: once, 12.3 % (the bp 1 235 would give 12.4 %)
        (200000, 175300, 124),  # 12.35 %: half away from zero
        (200000, 224700, -124),  # -12.35 %
        (1000, 0, 1000),
    ],
)
def test_the_displayed_rate_is_rounded_once_from_the_exact_ratio(
    earned: int, spent: int, tenths: int | None
) -> None:
    assert savings_rate_tenths(earned, spent) == tenths


def test_mixed_currencies_and_unknown_categories_are_refused() -> None:
    food = _category()
    with pytest.raises(ValueError, match="PLN"):
        summarize_month([Entry(food.id, Money(1, "PLN"))], [], [food], currency="EUR")
    with pytest.raises(ValueError, match="unknown category"):
        summarize_month([], [PlanLine(uuid4(), _eur(1))], [food], currency="EUR")
    with pytest.raises(ValueError, match="sort"):
        summarize_month([], [], [food], currency="EUR", sort="name")  # type: ignore[arg-type]


# Properties

_KINDS = st.sampled_from(
    [("expense", "regular"), ("expense", "savings"), ("expense", "transfer"),
     ("income", "regular"), ("income", "savings"), ("income", "transfer")]
)  # fmt: skip
_AMOUNT = st.integers(min_value=1, max_value=10**12)


@st.composite
def _month(draw: st.DrawFn) -> tuple[list[SummaryCategory], list[Entry], list[PlanLine]]:
    categories = [
        SummaryCategory(uuid4(), kind, treatment, draw(st.booleans()))
        for kind, treatment in draw(st.lists(_KINDS, min_size=1, max_size=8))
    ]
    ids = st.sampled_from([c.id for c in categories])
    entries = [Entry(i, _eur(a)) for i, a in draw(st.lists(st.tuples(ids, _AMOUNT), max_size=20))]
    planned = draw(st.dictionaries(ids, _AMOUNT, max_size=8))
    lines = [PlanLine(i, _eur(a)) for i, a in planned.items()]
    return categories, entries, lines


@given(_month(), st.sampled_from(["actual", "manual"]))
def test_totals_equal_the_sum_of_rows_and_transfers_never_appear(
    month: tuple[list[SummaryCategory], list[Entry], list[PlanLine]], sort: str
) -> None:
    categories, entries, lines = month
    summary = summarize_month(entries, lines, categories, currency="EUR", sort=sort)  # type: ignore[arg-type]
    kinds = {c.id: (c.kind, c.treatment) for c in categories}
    counted: dict[UUID, int] = {}
    for entry in entries:
        if kinds[entry.category_id][1] != "transfer":
            counted[entry.category_id] = (
                counted.get(entry.category_id, 0) + entry.amount.amount_minor
            )
    seen: set[UUID] = set()
    assert [b.key for b in summary.blocks] == list(BLOCK_ORDER)
    for block in summary.blocks:
        assert block.planned.amount_minor == sum(r.planned.amount_minor for r in block.rows)
        assert block.actual.amount_minor == sum(r.actual.amount_minor for r in block.rows)
        assert block.difference.amount_minor == sum(r.difference.amount_minor for r in block.rows)
        for row in block.rows:
            assert kinds[row.category_id][1] != "transfer"
            assert block_of(*kinds[row.category_id]) == block.key
            assert row.difference.amount_minor == difference(
                block.key, row.planned.amount_minor, row.actual.amount_minor
            )
            # The sign convention, independently of difference(): positive is favourable.
            p, a, d = (row.planned.amount_minor, row.actual.amount_minor,
                       row.difference.amount_minor)  # fmt: skip
            assert abs(d) == abs(p - a)
            if block.key in ("expense", "withdrawn"):
                assert (d > 0) == (a < p)
            else:
                assert (d > 0) == (a > p)
            assert row.category_id not in seen
            seen.add(row.category_id)
            assert row.actual.amount_minor == counted.get(row.category_id, 0)
    assert set(counted) <= seen  # every counted actual has its row (planned 0 if no line)
    active = {c.id for c in categories if not c.archived and c.treatment != "transfer"}
    assert active <= seen  # every active non-transfer category has a row (to be planned)
    if sort == "actual":
        for block in summary.blocks:
            actuals = [r.actual.amount_minor for r in block.rows]
            assert actuals == sorted(actuals, reverse=True)
    kpis = summary.kpis
    assert kpis.earned == summary.block("income").actual
    assert kpis.spent == summary.block("expense").actual
    assert kpis.saved == summary.block("saved").actual
    assert kpis.net.amount_minor == (
        kpis.earned.amount_minor - kpis.spent.amount_minor - kpis.saved.amount_minor
    )
    assert (kpis.savings_rate_bp is None) == (kpis.earned.amount_minor == 0)


@given(st.integers(min_value=1, max_value=10**15), st.integers(min_value=0, max_value=10**15))
def test_the_savings_rate_is_within_half_a_basis_point(earned: int, spent: int) -> None:
    rate = savings_rate_bp(earned, spent)
    assert rate is not None
    exact = (earned - spent) * 10000
    assert abs(rate * earned - exact) * 2 <= earned  # |rate - exact/earned| <= 0.5
