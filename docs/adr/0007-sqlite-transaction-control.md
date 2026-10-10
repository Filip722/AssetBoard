# ADR-0007: SQLite transaction control

**Status:** Accepted
**Date:** 2026-10-10

## Context

Python's sqlite3 driver historically starts transactions implicitly and may omit a transaction for DDL. This makes transactional DDL and SQLAlchemy savepoints unreliable, including for migrations and rollback-based tests.

## Decision

Disable sqlite3's implicit transaction management on connect by setting `isolation_level` to `None`. Use SQLAlchemy's `begin` event to emit an explicit `BEGIN`. Apply SQLite pragmas when each DBAPI connection is created.

`BEGIN IMMEDIATE` can acquire SQLite's write reservation before a migration starts changing the schema, making writer contention fail early rather than halfway through the migration. The current engine emits ordinary deferred `BEGIN`; do not substitute `BEGIN IMMEDIATE` globally, because ordinary read transactions would unnecessarily reserve the single-writer slot. Startup migrations are not coordinated across application processes; deploy and run migrations with a single worker, then start additional workers after migration completion.

## Consequences

Schema changes and nested transactions use consistent SQLAlchemy transaction boundaries. The engine owns transaction starts, so all SQLite connections must be created through `create_database_engine`.
