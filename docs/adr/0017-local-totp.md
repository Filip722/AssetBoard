# ADR-0017: Local TOTP and workspace enforcement

**Status:** Proposed
**Date:** 2026-10-20

## Context

F1.6 was deferred in PR #93. This contribution implements the optional local second factor
from issue #26 and resolves the previously undecided owner policy as workspace-specific.
Global users may belong to unrelated workspaces; a workspace owner must not change another
workspace's access policy. External identity providers remain responsible for their MFA.

## Decision

- Use PyOTP for six-digit SHA-1 TOTP with 30-second steps and one step of drift in either
  direction. These are the authenticator defaults; the one-step tolerance handles modest
  clock drift without accepting arbitrarily old codes. Persist the last accepted step and
  serialize factor updates to reject replay, including simultaneous requests.
- Ask for password and code together on password sign-in, including `/login/local`. Reuse the
  existing reserved account/address attempts; never create a partial login session. A missing
  or invalid code uses the existing generic failure. Password-confirmed identity linking also
  verifies the factor before clearing its confirmation throttle or linking the new identity.
- Enrollment requires password confirmation through the existing confirmation entry point.
  Its pending secret is bound to that session for ten minutes (the existing sensitive-action
  freshness interval); completion requires a code. Enrollment attempts have a separate per-user
  bucket using existing throttle settings, unaffected by restarting enrollment.
- Encrypt pending and enabled secrets with Fernet using `derive_subkey(instance_secret,
  "totp")`. The instance already persists this secret outside the database; there is no new
  operator setting. Generate QR SVG locally through Segno; no enrollment data leaves the app.
- Generate ten 128-bit random recovery codes and show them once after enrollment. Store SHA-256
  hashes, sufficient for random high-entropy values rather than user passwords. Lock the factor
  and consume a code in the same transaction as issuing a session. Both SQLite's existing
  immediate transaction and PostgreSQL row locks serialize consumption.
- `Workspace.require_two_factor` defaults off. Context construction checks it for password
  sessions, and write role guards re-read it. Every member, owners included, needs a verified
  second factor to access that workspace. Profile/security routes and the workspace switcher
  remain available; unrelated workspaces and OIDC/proxy sessions are unaffected.
- Enabling and password-confirmed disabling revoke other password sessions. The current local
  session records whether the second factor was verified. Audit these changes without secrets.
- Regenerating recovery codes requires the current password and TOTP or an unused recovery code;
  old codes are invalidated and the replacement set is shown once. An instance administrator can
  reset another user's factor after confirming their own account; this removes the factor and all
  recovery codes, revokes every target session, and is audited without secret data.

## Consequences

Backups must preserve the persistent instance secret as well as the database. Replacing it
makes authenticator secrets unreadable and authenticator sign-in fails closed; recovery codes
remain usable. Changing passwords does not clear MFA. There is no CLI MFA reset: users who lose
all recovery methods need an instance administrator to reset the factor through the Users page.

New direct dependencies are PyOTP (MIT, the spec's maintained TOTP implementation), Segno
(BSD-3-Clause, a maintained pure-Python QR encoder with no dependencies), and cryptography
(Apache-2.0 OR BSD-3-Clause, maintained by PyCA and already locked transitively through
Authlib). The standard library and existing app dependencies do not supply a TOTP provisioning
API or QR encoder. Declaring cryptography directly supports using its authenticated-encryption
recipe without implementing encryption ourselves. These licenses are compatible with AGPL-3.0.

References: [PyOTP](https://pyauth.github.io/pyotp/),
[Segno](https://segno.readthedocs.io/en/latest/),
[Fernet](https://cryptography.io/en/latest/fernet/),
[RFC 6238](https://www.rfc-editor.org/rfc/rfc6238).
