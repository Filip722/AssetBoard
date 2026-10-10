# ADR-0006: UUID primary keys

**Status:** Accepted
**Date:** 2026-10-10

## Context

Records may be created across local deployments and eventually synchronized or imported. Sequential
database identifiers expose insertion order and complicate merging records from different sources.

## Decision

Use UUID version 4 primary keys, generated with Python's `uuid4`, through SQLAlchemy's
`Uuid(as_uuid=True)` type. SQLite stores the UUID as a 32-character hexadecimal value; databases
with native UUID support use their native UUID column type.

## Consequences

Identifiers can be generated without a central sequence and are suitable for future import and
sync workflows. UUIDs are larger than integer keys and should not be treated as authorization
secrets. SQLite stores the current identifiers as text rather than native UUID values.
