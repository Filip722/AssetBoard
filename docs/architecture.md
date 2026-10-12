# Architecture

AssetBoard is a single FastAPI process with server-rendered pages. Code is layered top to bottom:

- `web/`: routes, templates and form handling. Routes call services and never touch ORM models
  or sessions for workspace data.
- `services/`: use cases. Each service that works on workspace data takes a `Context` and checks
  the role it needs with `require_role`.
- `db/`: models, migrations and repositories. Workspace data is read and written only through
  repositories.
- `domain/`: pure logic with no I/O, database or clock access.

Decisions are recorded in [ADRs](adr/README.md). Tenant isolation is described in
[ADR-0003](adr/0003-workspaces-and-tenant-isolation.md).

## Workspaces, Context and roles

Every signed-in request that works on workspace data gets a `Context(user, workspace, role,
clock)` from the `CurrentContext` dependency (`web/context.py`), built by
`services.context.build_context`:

- The workspace is the one selected in the session (`user_sessions.current_workspace_id`) when
  the user is still a member of it. Otherwise it is the user's oldest membership (by
  `created_at`, then `id`). The fallback is not stored: only `select_workspace` writes the
  selection.
- The membership and role are read from the database on every request, so a changed role or a
  removed membership applies on the next request.
- A user with no membership gets a 403 page. Account pages and sign-out keep working.

Roles are ranked `owner` > `editor` > `viewer`. Services call
`require_role(ctx, Role.EDITOR)`, which raises `PermissionDenied` (rendered as a 403 page or
toast).

## Repositories

`ScopedRepository[T]` (`db/repositories/base.py`) is built from a session and a `Context`:

- Every query adds `workspace_id = ctx.workspace.id`. Nothing public accepts a SQL statement
  or a workspace id.
- `get` returns `None` and `get_or_raise`, `update` and `delete` raise `NotFound` for a row of
  another workspace, exactly as for a missing row. The web layer renders `NotFound` as a plain
  404 with no hint that the row exists elsewhere.
- `add` sets `workspace_id`. It refuses a row of another type or one that already belongs to
  another workspace.
- `delete(obj)` re-reads the object through the scope before deleting it.
- A repository can declare `unsupported` operations. Audit events, for example, are
  append-only: no update, no delete. Models marked `AppendOnly` are also guarded at flush:
  changing or deleting a loaded row raises `AppendOnlyViolationError`.
- A row's `workspace_id` never changes. A `before_flush` guard raises
  `WorkspaceReassignmentError` if code changes it, including from `NULL` to a value. Bulk
  `update()` and `delete()` statements are not used for workspace data.

`UserMembershipReader` is the one reader scoped by user instead of workspace. It lists the
signed-in user's own memberships, which the Context and the workspace switcher need before a
workspace is known.

## How to add a new workspace-owned model

1. **Model.** Inherit `WorkspaceScopedMixin`, which adds a non-null, indexed `workspace_id`
   foreign key and the `WorkspaceOwned` marker. A model whose `workspace_id` must be nullable
   (only `AuditEvent` today) inherits `WorkspaceOwned` directly and declares the column with
   `active_history=True`.
2. **Foreign keys to other workspace-owned tables must be composite.** Use
   `(workspace_id, parent_id) -> parent(workspace_id, id)`, with a unique constraint on
   `parent(workspace_id, id)`, so a row can never point at a parent in another workspace.
   Foreign keys to `workspaces`, `users` and other tables without a `workspace_id` stay single
   column.
3. **Migration.** Generate it with `make revision m="..."` and check it with `make check`. Use
   `batch_alter_table` for SQLite.
4. **Repository.** Add a `ScopedRepository` subclass in `src/assetboard/db/repositories/`, set
   `model` (and `unsupported` if needed), and import it in `db/repositories/__init__.py` so it
   registers. Build extra queries on `self._scoped_select()`.
5. **Tenant factory.** Add a `TenantCase` for the model to `TENANT_CASES` in
   `tests/tenant/factories.py`: a factory that builds a valid row for a given workspace and,
   unless updates are unsupported, `update_values` with a valid change.
6. **Coverage of extra methods.** For each public method the repository adds beyond the base
   ones, add an entry to `EXTRA_METHOD_COVERAGE` in `tests/tenant/coverage.py` naming the test in
   `tests/integration/test_tenant_isolation.py` that proves it stays in its workspace.
7. **Services only through the repository.** Do not reference the model in `select()`,
   `session.get()`, attribute access or instantiation outside `db/repositories/`. Outside
   `db/`, raw SQL (`text()`, `exec_driver_sql()`, `execute("...")`) is not allowed at all.
8. **No relationships into it from unowned models.** A relationship such as `Workspace.items`
   or `User.items` would load rows without the repository's scope. Relationships from one
   workspace-owned model to another, or to an unowned model, are fine.

What fails if a step is missing:

| Missing | Failing test |
|---|---|
| `WorkspaceOwned` marker or `workspace_id` column, repository or tenant factory | `test_discovery_finds_every_workspace_owned_model_with_repository_and_factory` |
| Scoping in a repository query | `test_user_b_cannot_read_update_or_delete_user_a_rows[<Model>]` |
| Coverage entry for a new public method | `test_every_public_repository_method_is_covered` |
| Composite foreign key | `test_workspace_owned_foreign_keys_include_workspace_id` |
| Model or table used outside a repository | `test_no_unscoped_queries_outside_repositories` |
| Relationship from an unowned model | `test_no_relationship_from_unowned_models_into_workspace_owned_models` |

The only exceptions to the unscoped-query rule are listed with a reason in `BOOTSTRAP_ALLOWLIST`
(`tests/tenant/static_scan.py`), keyed by module and function. They are first-run setup and
registration, which create a workspace before any Context exists, and the auth audit writer. An
entry that no longer matches any code fails the test.
