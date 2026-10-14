# Post-1.0 backlog

Work deliberately left out of 1.0. Each item keeps enough context to be planned later with the
standard workflow (docs/specs/00-overview.md §8); none is scheduled yet.

Also deferred from M1 (see M1-identity-and-workspaces.md): workspace deletion, ownership
transfer, and leaving a workspace.

---

## F1.6 TOTP two-factor authentication

Moved out of M1. Until it exists, use OIDC or reverse-proxy authentication backed by an identity
provider that supports MFA.

**Acceptance**
- Optional per user; TOTP via `pyotp`.
- Enrollment shows a QR code and the manual key; it completes only after a valid code.
- Ten single-use recovery codes, stored hashed, shown once.
- Verified at password sign-in (including `/login/local`); OIDC and proxy sign-ins skip it (the
  identity provider's responsibility).
- Code entry is throttled like password sign-in.
- An owner can require a second factor for local logins (scope to be decided when planned).
- Disabling requires the password, through the profile's confirmation entry point
  (`web.confirm.run_confirmed`).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F1.6 from docs/specs/backlog-post-1.0.md: TOTP enrollment (QR + manual key), verification
at login, hashed recovery codes, throttling, disable flow requiring password. Tests with a fixed
Clock for TOTP windows.
```

---

## Administrator password reset from the Users page

Today an administrator can reset someone's password only on the command line
(`assetboard reset-password <email>`). The Users page can create an account with a temporary
password but cannot reset an existing one.

**Acceptance**
- A "Reset password" action on the Users page, with its own confirm page, confirmed by the
  acting administrator like every other admin action (`run_confirmed`).
- Generates a temporary password exactly as account creation does (16 characters, through the
  hash gate, shown once in the response, never logged or audited), sets
  `users.must_change_password`, and ends every session of the account.
- Not offered for the acting administrator's own account (they use the password page), and not
  under `OIDC_ONLY` for accounts without a password.
- Audit action `admin.password_reset` with data keys `temporary_password` and
  `sessions_revoked`, added to `AUDIT_DATA_KEYS` and the audit view's labels.

---

## Warning before deactivating a workspace's only owner

Deactivating an account that is the only active owner of a workspace leaves that workspace
without an active owner until the account is activated again (documented in
docs/accounts.md). The confirm page does not warn about it.

**Acceptance**
- The deactivate confirm page lists the workspaces (by name) where the account is the only
  active owner, and says that nobody can manage those workspaces until it is activated again.
- Deactivation stays allowed; the warning is information, not a block.
- If ownership transfer (above) exists by then, the page links to it.
