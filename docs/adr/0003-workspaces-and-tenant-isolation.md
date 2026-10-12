# ADR-0003: Workspaces and tenant isolation

**Status:** Accepted
**Date:** 2026-10-10

## Context

Users need to separate financial records into workspaces, and every query must prevent records from
other workspaces from leaking into a request.

## Decision

Workspace-owned records carry a `workspace_id`. Access goes through repositories that enforce the
active workspace scope. Services receive a `Context` containing the user, workspace, role, and
clock; authorization is enforced in services.

## Consequences

Tenant scoping is an explicit repository responsibility and can be tested systematically. New
workspace-owned models require isolation coverage, and routes cannot bypass services to query ORM
models directly.

## Amendment (2026-10-12): how isolation is enforced and tested (F1.2)

**Decision.**
- Workspace-owned models carry the `WorkspaceOwned` marker, through `WorkspaceScopedMixin` or
  directly for `AuditEvent`, whose `workspace_id` is NULL for instance-level events. The
  isolation test asserts that the marked set equals the set of mapped classes with a
  `workspace_id` column.
- `ScopedRepository` subclasses register themselves by model. The isolation test requires a
  repository and a test factory for every discovered model, then checks in both directions that
  one workspace cannot read, list, update or delete another's rows. Positive controls run first,
  and rows are compared column by column afterwards.
- Every public repository method is mapped to the test that covers it.
- A `before_flush` guard rejects any change of a persistent row's `workspace_id`, including
  `NULL` to a value.
  The same guard rejects changing or deleting a row marked `AppendOnly` (audit events).
- Foreign keys between workspace-owned tables must include `workspace_id` (composite).
- Models that are not workspace-owned have no ORM relationships into workspace-owned models.
- A static scan of `src/` rejects references to workspace-owned models outside
  `db/repositories/`: queries, attribute access and instantiation, with import aliases resolved.
  Outside `db/` it also rejects raw SQL outright (`text()`, `exec_driver_sql()`, `execute()`
  with a literal).
  The only exceptions are a bootstrap allowlist keyed by `(module, function)`, each entry with a
  reason, and stale entries fail. Today the entries are workspace creation during setup and
  registration, and the auth audit writer.
- `UserMembershipReader` is the one user-scoped reader: the user's own memberships, needed
  before a workspace is known.
- A row of another workspace is reported as not found (404 with no workspace hint), never as
  forbidden.

**Considered and deferred: a global `with_loader_criteria` filter.** A session-wide ORM filter
on `workspace_id` would add a second line of defence. It is deferred because legitimate
cross-workspace reads (the user's memberships, a future instance-administrator view) would need
to switch it off. It also doesn't cover Core statements. Today the repositories, the flush guard
and the static scan enforce the boundary, and the isolation test proves it. Revisit if code
outside repositories ever needs to query workspace data.
