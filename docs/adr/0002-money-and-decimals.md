# ADR-0002: Money and decimal values

**Status:** Accepted
**Date:** 2026-10-10

## Context

Binary floating point cannot represent many decimal fractions exactly, and financial calculations
must have predictable precision and rounding.

## Decision

Represent money as integer minor units plus an ISO currency code. Represent quantities and unit
prices with `Decimal` and the project's `DecimalType`. Store rates in integer basis points. Round
half up once per computed line to the currency minor unit; do not use floating point for financial
values.

## Consequences

Arithmetic and persistence preserve explicit precision. Decimal values are ordered or aggregated in
Python rather than through SQL operations on `DecimalType` columns.
