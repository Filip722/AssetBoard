# ADR-0013: Invitation links

**Status:** Accepted
**Date:** 2026-10-12

## Context

Workspace owners need to bring other people into a workspace (F1.2). AssetBoard does not send
email, so an invitation is a link the owner shares however they like. Open registration is off
by default (`ASSETBOARD_ALLOW_REGISTRATION=false`), so an invitee may not be able to create an
account otherwise. A link that grants access is a bearer secret: it must not leak through logs,
`Referer` headers or caches, and it must work exactly once.

## Decision

- **Token storage.** A token is 256 bits from `secrets.token_urlsafe(32)` (43 URL-safe
  characters). Only its SHA-256 is stored, with a unique index. A full-entropy token needs no
  slow hash. The link is shown to the owner once, in the response that creates it, and cannot
  be shown again. An invitation has a role, an expiry of 1, 7 (default) or 30 days, and an
  email lock (optional until the amendment of 2026-10-18, required since). A workspace can
  have at most 50 pending invitations.
- **Single use.** Accepting runs in one `BEGIN IMMEDIATE` transaction. It finds the invitation by
  token hash, checks that it is pending (not used, revoked or expired), creates the membership,
  marks the invitation used and appends the audit event. Of two concurrent accepts, exactly one
  succeeds. Expired, revoked, used, unknown and malformed tokens all get the same "not valid"
  page. A user who is already a member is told so; the role stays unchanged and the invitation
  stays unused.
- **Scope from the invitation row.** The invitee has no Context. `InvitationTokenReader` finds an
  invitation by its token hash and returns only where it lives. A `WorkspaceScope` is built from
  that row, never from request input, and the membership and audit events are written through
  the workspace-scoped repositories. There is no new bootstrap allowlist entry.
- **The token is in the URL path** (`/invite/<token>`), so a pasted link just works. Instead of a
  cookie handoff:
  - The app's request log replaces the token with `[redacted]` and never logs query strings,
    which may carry `/login?next=/invite/<token>`.
  - Uvicorn's access log, which prints full URLs, is turned off in `assetboard serve`. The app's
    own request log already records every request.
  - `/invite/*`, `/login` and `/register` responses send `Referrer-Policy: same-origin` (also as
    a meta tag); sign-in and registration can carry `next=/invite/<token>`. No other site
    receives a `Referer` from these pages. Every page is `Cache-Control: no-store`.
  - Not `no-referrer` for the page: under it, browsers send `Origin: null` on form POSTs, and
    the Origin check (ADR-0012) refuses those. Instead, every stylesheet and script tag carries
    `referrerpolicy="no-referrer"` and the icon is inline, so loading a page sends no `Referer`
    at all. Only navigations and form submissions from these pages to our own server carry
    one, which the app never logs. A reverse proxy that logs `Referer` (for example nginx's
    `combined` format) can record those; it already logs the same URLs as request lines.
  - Reverse-proxy logs remain outside our control; that is an accepted risk.
- **Invitation-only sign-up.** On `/invite/<token>`, a signed-out visitor can sign in (and is
  returned to the invitation) or create an account. That account is created even when open
  registration is off, and it gets no workspace of its own: it joins only the invitation's
  workspace. Sign-up through an invitation uses the same per-address registration throttle and
  password-hashing limit as open registration.
- **Email lock.** When an invitation names an email address, only an account with that
  (normalized) address can accept it. The invite page never displays or pre-fills that address.
  - **Owner invitations must have an email lock.** An owner can change everything, including
    members, so an unlocked owner link would be too easy to misuse.
  - **A wrong email is counted per invitation.** After 5 wrong attempts the invitation is
    revoked; each attempt and the revocation are audited.
  - **Specific mismatch message.** It tells the visitor that the invitation was sent to a
    different address, without naming it.
  - **The lock is not proof of ownership.** AssetBoard does not verify email addresses, so
    someone who holds the link can create an account with the locked address. It only stops a
    link from being accepted by the wrong existing account.

## Consequences

- An owner who loses a link revokes it and creates a new one.
- Anyone who holds an email-locked link can burn it with 5 wrong attempts. The owner then
  creates a new link.
- Anyone who holds an unlocked link can join with its role until it expires or is used. Owners
  should prefer short expiries and email locks, and share links privately.
- Turning off uvicorn's access log changes the log output for existing deployments. This is
  documented under "Upgrading" in `docs/deployment.md`.
- Sign-up through an invitation discloses whether an email is already registered, as open
  registration does (ADR-0012).

## Amendment (2026-10-12): invitations through OIDC (F1.3)

An invitee may also continue with the configured OIDC provider (ADR-0014). The same rules
apply, with one stricter point:

- A signed-in visitor (whatever the sign-in method) accepts on the invite page; the email lock
  compares the account's email.
- A visitor without an account gets one that joins only the invitation's workspace, even when
  `ASSETBOARD_OIDC_AUTO_CREATE` is off, under the same per-address registration limit. Creating
  the user, linking the identity and using the invitation are one transaction, so a link used
  meanwhile rolls everything back.
- **The provider's email must be verified** to create an account. The email lock then compares
  that email; a mismatch is counted per invitation (5 revoke it). An unverified email is refused
  before the lock is compared and is not counted. So, unlike a local sign-up, an OIDC sign-up
  does prove control of the locked address, as far as the provider verifies it.
- If the email already belongs to an account and linking is off, the visitor is told the
  account exists and should sign in with it; the invitation stays unused.

## Amendment (2026-10-18): every invitation is locked to an email (pre-release A)

The owner decided that an invitation of any role (owner, editor, viewer) must name an email
address. An unlocked link could be forwarded and accepted by anyone with its role; requiring the
lock closes that for every role, not only for owners.

- `services.workspaces.create_invitation` refuses a missing or blank email (`email_required`).
  The owner's form marks the field required. Creating an invitation still looks up no account,
  so it reveals nothing about which addresses are registered.
- **No data migration.** `invitations.email` stays nullable: invitations created before this
  change have no lock and stay valid, for any account, until they expire (30 days at most), are
  used or are revoked. The accept and sign-up paths treat a NULL email as "no lock", exactly as
  before. The members page labels them "Anyone with the link"; they are not revoked
  automatically. A database constraint is not added: one that applies only to new rows is not
  portable between SQLite and PostgreSQL.
- The database CHECK constraint `owner_requires_email` (owner ⇒ email) stays; the service error
  code of that name is replaced by `email_required`.
- Unchanged: the invitee side never displays or pre-fills the locked address (the owner still
  sees it in the pending list), wrong emails are counted (5 revoke the invitation), and the
  lock is still not proof of ownership.
- `invitation.created` keeps its `email_locked` key; it is now always `true`.
