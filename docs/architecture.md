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

## Colours and themes

Colours are named tokens (`canvas`, `surface`, `subtle`, `ink`, `muted`, `line`, `primary`,
`accent`, `success`, `danger`, `warning`, `inverse`, ...). Their values are CSS variables set
once for the light theme (`:root`) and once for the dark theme (`:root.dark`) in
`static/css/input.css`; `tailwind.config.js` maps each name to its variable. Templates use only
token classes and no `dark:` colour variants, so changing a colour is a one-place edit followed
by `make css`. `tests/unit/test_theme_tokens.py` checks WCAG AA contrast for every text and
background pair in both themes and that templates use no other colours. Form controls use the
shared `form-control` and `form-select` classes.

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

Two readers are scoped by something other than the workspace, because they are needed before a
Context exists:

- `UserMembershipReader` is scoped by user. It lists the signed-in user's own memberships, which
  the Context and the workspace switcher need.
- `InvitationTokenReader` is scoped by invitation token hash. It returns only where an
  invitation lives; the accept flow builds a `WorkspaceScope` from that row and continues
  through the scoped repositories (ADR-0013).

## Owner actions and stale Contexts

A Context is built when a request starts, so its role can be out of date by the time a write
runs. Services that change workspace settings or memberships (`services/workspaces.py`) run in
one `BEGIN IMMEDIATE` transaction that:

1. re-reads the actor's membership and requires `owner` there;
2. checks the last-owner rule (a workspace always keeps at least one owner) there;
3. makes the change and appends the audit event.

Concurrent owner actions queue at `BEGIN` and each sees the previous one's committed result.

`require_role_now(session, ctx, minimum)` in `services/context.py` is the shared form of step 1
for any role: it re-reads the actor's membership in the open transaction and raises
`PermissionDenied` unless the stored role is at least `minimum` (a removed member counts as a
viewer). Services check `require_role(ctx, ...)` on the Context first, as a cheap early refusal,
and then `require_role_now` inside the write. Every category write (`services/categories.py`,
F2.1) does this with `editor`; `services/workspaces.py` still uses its own owner-only helper.
The workspace row itself is changed only through `CurrentWorkspaceRepository`, which reads and
updates `ctx.workspace.id` and only the fields in `UPDATABLE_FIELDS`.

### Closed months (F2.5)

A write to a month's data (transactions, planned values) calls
`services.months.ensure_month_open(session, ctx, month)` (or `ensure_date_open` for a date) in
its write transaction, right after `require_role_now` and before changing anything; it raises
`MonthClosed` for a closed month (the app answers 409 if a route does not handle it). Writers are
serialised on SQLite by `BEGIN IMMEDIATE`. PostgreSQL does not queue writers at `BEGIN`, so the
guard takes a shared lock on the workspace row (`services.months.lock_month_writes`) and closing
or reopening takes an exclusive one (`CurrentWorkspaceRepository.lock`). **Lock order:** the
workspace row first, before any other row, in every transaction that takes it. A write that
reads stored rows to learn which months it touches (editing a transaction reads its old date;
bulk changes read the selected rows) calls `lock_month_writes` **before** those reads, then the
guard for every month; taking the lock again is harmless.

### Base currency and stored amounts (F2.2)

Amounts are stored in the workspace's base currency and never converted, so the currency is
locked once any amount exists. Each feature that stores amounts adds a check to
`services.workspaces.BASE_CURRENCY_LOCKS` (transactions now; budget lines in F2.3). A write that
stores an amount reads the currency from the workspace row **inside** its transaction, after
the month lock, never from `ctx.workspace` (the Context can be older than a settings change).
The settings write takes the exclusive workspace lock first; on PostgreSQL a currency change and
a concurrent first transaction therefore cannot both pass.

**Lock order (PostgreSQL).** Always the workspace row first, then other rows:

| Write | Workspace row | Then |
|---|---|---|
| Transaction create, edit, delete, undo | `FOR SHARE` (`lock_month_writes`) | edit/delete/undo: the transaction row `FOR UPDATE` (`get_for_update`) |
| Category update, archive, restore, delete | `FOR UPDATE` | reads categories and usage |
| Close or reopen a month; workspace settings | `FOR UPDATE` | month state / workspace fields |

So a kind change or archive and a transaction write on the same category cannot interleave: the
transaction write sees the category as changed, or the category write sees the transaction.

Seams between services are static lists, not import-time registration: features add their
callables to `services.categories.USAGE_SOURCES` (what references a category) and
`BASE_CURRENCY_LOCKS`, so the result never depends on which modules happen to be imported.

## How to add a new workspace-owned model

`Invitation` (F1.2) is a worked example: `WorkspaceScopedMixin`, migration `0004_f12`,
`InvitationRepository` (generic update and delete unsupported; named transitions such as
`revoke` and `mark_used` instead), a `TenantCase` in `tests/tenant/factories.py`, and coverage
entries for each extra method.

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
