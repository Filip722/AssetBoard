from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Numeric, Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine


class UtcDateTime(TypeDecorator[datetime]):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError("UtcDateTime requires a datetime value")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UtcDateTime requires a timezone-aware datetime")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class DecimalComparator(TypeDecorator.Comparator[Decimal]):
    def __lt__(self, other: Any) -> Any:
        raise TypeError("DecimalType columns do not support ordering comparisons")

    def __le__(self, other: Any) -> Any:
        raise TypeError("DecimalType columns do not support ordering comparisons")

    def __gt__(self, other: Any) -> Any:
        raise TypeError("DecimalType columns do not support ordering comparisons")

    def __ge__(self, other: Any) -> Any:
        raise TypeError("DecimalType columns do not support ordering comparisons")


class DecimalType(TypeDecorator[Decimal]):
    impl = Text
    cache_ok = True

    @property
    def comparator_factory(self) -> type[DecimalComparator]:
        return DecimalComparator

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(Text())
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Numeric(precision=38, scale=18, asdecimal=True))
        return dialect.type_descriptor(Numeric(precision=38, scale=18, asdecimal=True))

    def process_bind_param(
        self,
        value: Decimal | None,
        dialect: Dialect,
    ) -> str | Decimal | None:
        if value is None:
            return None
        if isinstance(value, float):
            raise TypeError("DecimalType does not accept float values")
        if not isinstance(value, Decimal):
            raise TypeError("DecimalType requires a Decimal value")
        if not value.is_finite():
            raise ValueError("DecimalType requires a finite Decimal value")

        exponent = value.as_tuple().exponent
        if not isinstance(exponent, int):
            raise ValueError("DecimalType requires a finite Decimal value")
        scale = max(-exponent, 0)
        integer_digits = 0 if value.is_zero() else max(value.adjusted() + 1, 0)
        if scale > 18 or integer_digits > 20 or integer_digits + scale > 38:
            raise ValueError("DecimalType supports at most 38 digits with 18 decimal places")

        if dialect.name == "sqlite":
            return format(Decimal(0) if value.is_zero() else value, "f")
        return value

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, float):
            raise TypeError("DecimalType cannot load float values")
        return value if isinstance(value, Decimal) else Decimal(str(value))
