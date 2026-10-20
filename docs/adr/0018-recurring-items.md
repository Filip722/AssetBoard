# ADR-0018: Structured recurrence and in-process generation

**Status:** Proposed
**Date:** 2026-10-20

## Context

F3.1 needs calendar-based planned occurrences, independent from posted transactions. Rules must
clamp short months, adjust for holidays, survive edits without rewriting history, and remain
isolated by workspace. The application already requires one worker and supports SQLite and
PostgreSQL. ADR number 0017 is reserved by the pending F1.6 contribution (#127).

## Decision

- Store structured recurrence fields and implement the small weekly/monthly/yearly engine with
  standard-library calendar/date operations. Using `dateutil.rrule` directly would skip invalid
  dates rather than provide the required clamping. `python-dateutil` is only a transitive
  dependency of holidays, not a second recurrence engine.
- Count nominal scheduled dates before shifting and deduplicating them. Start/end constrain
  nominal dates; the query window constrains effective dates, inclusively. Intervals are anchored
  to the start period; weekly periods begin on Monday. Keep the existing 1900–2199 app date range.
- Obtain national/observed holiday dates locally through `holidays`; keep the domain pure by
  passing a holiday container. No country means weekends only; unsupported countries fail when
  working-day calculations are requested. Subdivisions are outside F3.1.
- Separate `ScheduledItem` and `ScheduledOccurrence` tables, both workspace-scoped with composite
  category/item foreign keys. Add only the fields consumed by F3.1: income/tax references,
  transaction links, reminder lead times and auto-post settings arrive with their features.
- Serialize generation and rule changes with the exclusive workspace lock (`BEGIN IMMEDIATE`
  on SQLite, `FOR UPDATE` on PostgreSQL), plus the unique item/due-date constraint. Regenerate
  pending dates from today onward; preserve older, posted and skipped rows. The same lock
  orders category state/kind and currency changes against new scheduled references.
- Pausing/archiving removes pending dates from today onward. Deletion is physical only without
  history. Resuming starts from today. Past generation is explicit at creation only.
- Use APScheduler 3's in-memory background scheduler owned by the application lifespan. Rebuild
  per-workspace 03:00 cron jobs at startup, reconcile new workspaces/time zones every minute,
  and generate the forward horizon on startup as catch-up. Shutdown waits before disposing the
  database engines. Trusted generation receives an explicit workspace scope and injected Clock;
  it does not impersonate an owner or depend on whether an owner is currently active.

## Consequences

The default horizon is 13 calendar months, as required by the feature, and is configurable with
`RECURRENCE_HORIZON_MONTHS`. A future workflow can consume pending rows without changing how the
recurrence engine works. Posting and reminders remain outside this change. Changing the country
or time zone affects subsequent generation; editing/saving a rule refreshes its future pending
rows under the current settings.

New runtime dependencies are **APScheduler** (MIT), the maintained in-process scheduler selected
by the stack, and **holidays** (MIT), the maintained national-calendar provider requested by the
specification. The standard library does not provide either national holiday rules or an
in-process time-zone-aware job scheduler. Both licenses are compatible with AGPL-3.0-or-later.
No new test dependency is needed. The scheduler adapter contains the third-party integration;
service/domain modules continue to pass strict type checking.

References: [APScheduler guide](https://apscheduler.readthedocs.io/en/3.x/userguide.html),
[holidays API](https://holidays.readthedocs.io/en/latest/api/).
