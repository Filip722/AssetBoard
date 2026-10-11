# ADR-0007: SQLite transaction control

**Status:** Accepted
**Date:** 2026-10-10 (amended 2026-10-11)

## Context

Python's sqlite3 driver historically starts transactions implicitly and may omit a transaction for DDL. This makes transactional DDL and SQLAlchemy savepoints unreliable, including for migrations and rollback-based tests.

## Decision

Disable sqlite3's implicit transaction management on connect by setting `isolation_level` to `None`. Use SQLAlchemy's `begin` event to emit an explicit `BEGIN`. Apply SQLite pragmas when each DBAPI connection is created.

`BEGIN IMMEDIATE` can acquire SQLite's write reservation before a migration starts changing the schema, making writer contention fail early rather than halfway through the migration. The current engine emits ordinary deferred `BEGIN`; do not substitute `BEGIN IMMEDIATE` globally, because ordinary read transactions would unnecessarily reserve the single-writer slot. Startup migrations are not coordinated across application processes; deploy and run migrations with a single worker, then start additional workers after migration completion.

## Consequences

Schema changes and nested transactions use consistent SQLAlchemy transaction boundaries. The engine owns transaction starts, so all SQLite connections must be created through `create_database_engine`.

## Amendment (2026-10-11): opt-in `BEGIN IMMEDIATE` per transaction

**Context.** Authentication needs a few short transactions where a read decides a write:
claiming first-run setup, and reserving a login or password-change attempt against the throttle
before the password is verified. In a deferred transaction, two connections can both read,
then the second one's upgrade to a write fails immediately with `SQLITE_BUSY` (the busy timeout
does not apply to that upgrade), or both see a stale value.

**Decision.** The default stays a deferred `BEGIN`. A transaction can opt in to
`BEGIN IMMEDIATE` through the `assetboard_begin_immediate` execution option, which the engine's
`begin` hook reads. Code uses it only through `assetboard.db.engine.immediate_transaction` (or
`run_immediate`), and only for short write transactions that do no slow work (no password
hashing) while holding the lock. Savepoints (`begin_nested`) work inside it. Other dialects
ignore the option.

**Consequences.** Concurrent writers queue at `BEGIN` for up to the busy timeout, so these
transactions are serialised without retry logic. When the timeout is exceeded, SQLite raises
"database is locked"; callers map that to the same response as losing the race (for example
`SetupAlreadyCompleted`). Ordinary read transactions still do not reserve the writer slot.
