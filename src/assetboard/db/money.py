from __future__ import annotations

from typing import cast

from sqlalchemy.orm import Mapped, composite

from assetboard.domain.money import Money


def money_composite(amount_minor: str, currency: str) -> Mapped[Money]:
    return cast(Mapped[Money], composite(Money, amount_minor, currency))
