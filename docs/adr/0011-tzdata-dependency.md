# ADR-0011: Lock timezone database data as a dependency

**Status:** Accepted
**Date:** 2026-10-10

## Context

The slim runtime image may not contain an operating-system timezone database, but application code
and tests need `zoneinfo` data to resolve named time zones reliably.

## Decision

Declare `tzdata` as a runtime Python dependency and include its resolved version in `uv.lock`. The
container smoke test verifies named-zone support with `ZoneInfo("Europe/Warsaw")`.

## Consequences

Timezone behavior is available consistently across supported container environments and is updated
through the normal dependency review process. The locked package adds a small amount of runtime
data to the installed application.
