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
  optional email lock. A workspace can have at most 50 pending invitations.
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
  - Not `no-referrer`: under it, browsers send `Origin: null` on form POSTs, and the Origin
    check (ADR-0012) refuses those. Same-origin requests still carry a `Referer` to our own
    server, which the app never logs. A reverse proxy that logs `Referer` (for example nginx's
    `combined` format) can record these URLs; it already logs them as request lines.
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
