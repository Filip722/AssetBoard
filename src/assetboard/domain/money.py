from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext

_CURRENCY_PATTERN = re.compile(r"[A-Z]{3}\Z")


def _currency_units(codes: str, minor_unit: int) -> dict[str, int]:
    return dict.fromkeys(codes.split(), minor_unit)


CURRENCY_MINOR_UNITS = {
    **_currency_units(
        "AED AFN ALL AMD AOA ARS AUD AWG AZN BAM BBD BDT BGN BMD BND BOB BOV BRL BSD "
        "BTN BWP BYN BZD CAD CDF CHE CHF CHW CNY COP COU CRC CUP CVE CZK DKK DOP DZD "
        "EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GTQ GYD HKD HNL HTG HUF IDR ILS "
        "INR JMD KES KGS KHR KPW KYD KZT LAK LBP LKR LRD LSL MAD MDL MGA MKD MMK MNT "
        "MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD PAB PEN PGK PHP "
        "PKR PLN QAR RON RSD RUB SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD SSP STN "
        "SVC SZL THB TJS TMT TOP TRY TTD TWD TZS UAH USD USN UYU UZS VES WST XCD ZAR "
        "ZMW",
        2,
    ),
    **_currency_units("BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX UYI VND VUV XAF XOF XPF", 0),
    **_currency_units("BHD IQD JOD KWD LYD OMR TND", 3),
    **_currency_units("CLF UYW", 4),
}


def _require_currency_string(currency: object) -> str:
    if not isinstance(currency, str):
        raise TypeError("currency must be a string")
    return currency


def minor_unit_for(currency: object) -> int:
    normalized = _require_currency_string(currency).upper()
    if not normalized.isascii() or _CURRENCY_PATTERN.fullmatch(normalized) is None:
        raise ValueError("currency must be a three-letter ISO currency code")
    try:
        return CURRENCY_MINOR_UNITS[normalized]
    except KeyError:
        raise ValueError(f"unsupported currency: {normalized}") from None


@dataclass(frozen=True, slots=True)
class Money:
    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        if type(self.amount_minor) is not int:
            raise TypeError("amount_minor must be an integer")
        currency = _require_currency_string(self.currency).upper()
        minor_unit_for(currency)
        object.__setattr__(self, "currency", currency)

    @classmethod
    def from_decimal(
        cls,
        amount: Decimal,
        currency: str,
        minor_unit: int | None = None,
    ) -> Money:
        if type(amount) is not Decimal:
            raise TypeError("amount must be Decimal")
        if not amount.is_finite():
            raise ValueError("amount must be finite")
        currency_minor_unit = minor_unit_for(currency)
        if minor_unit is None:
            minor_unit = currency_minor_unit
        if type(minor_unit) is not int or not 0 <= minor_unit <= 18:
            raise ValueError("minor_unit must be an integer between 0 and 18")

        quantum = Decimal(1).scaleb(-minor_unit)
        precision = max(
            len(amount.as_tuple().digits),
            amount.adjusted() + minor_unit + 2,
            1,
        )
        with localcontext() as context:
            context.prec = max(context.prec, precision)
            rounded = amount.quantize(quantum, rounding=ROUND_HALF_UP)
            amount_minor = int(rounded.scaleb(minor_unit))
        return cls(amount_minor, currency)

    def to_decimal(self, minor_unit: int | None = None) -> Decimal:
        if minor_unit is None:
            minor_unit = minor_unit_for(self.currency)
        if type(minor_unit) is not int or not 0 <= minor_unit <= 18:
            raise ValueError("minor_unit must be an integer between 0 and 18")
        return Decimal(self.amount_minor).scaleb(-minor_unit)

    def __composite_values__(self) -> tuple[int, str]:
        return self.amount_minor, self.currency

    def __add__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(self.amount_minor + other.amount_minor, self.currency)

    def __sub__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(self.amount_minor - other.amount_minor, self.currency)

    def _require_same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise ValueError("cannot combine Money values with different currencies")
