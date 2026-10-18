"""Planned values (F2.3): the amount rule for budget lines. Pure: no I/O, no clock.

A planned value is entered in the locale's format, parsed by ``i18n.numbers`` into a
``Decimal`` and stored as positive integer minor units. Zero (and an empty field, which the
service maps to zero) means "no plan": the line is removed. Amounts are never rounded: more
fraction digits than the currency has are refused, as for transactions.
"""

from __future__ import annotations

from decimal import Decimal

from assetboard.domain.transactions import MAX_AMOUNT_MINOR, EntryError, amount_minor

__all__ = ["MAX_AMOUNT_MINOR", "EntryError", "planned_minor"]


def planned_minor(amount: Decimal, currency: str) -> int:
    """Minor units of a planned ``amount`` in ``currency``; ``0`` means remove the line.

    Error codes: ``invalid_amount`` (not a finite Decimal), ``amount_negative``,
    ``amount_too_large``, ``too_many_decimals``.
    """
    if type(amount) is not Decimal or not amount.is_finite():
        raise EntryError("invalid_amount")
    if amount < 0:
        raise EntryError("amount_negative")
    if amount == 0:
        return 0
    return amount_minor(amount, currency)
