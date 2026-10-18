# Your account

The profile page (`/account`, "Profile" in the account menu) holds your details, your
language and theme, and the ways you sign in. Password changes and signing out other devices
are on "Account security".

## Signing in

- A wrong email or password shows "The email or password is not correct." The page never says
  whether an account exists.
- By default, 5 sign-in attempts for an email per hour are free, successful ones included (and
  20 failed attempts from one network address); the 6th (or the 21st) starts a pause, and
  attempts during it say "Too many attempts. Try again in N minutes." The pause starts at 1
  minute and doubles with each further attempt, up to 15 minutes. The count covers every
  sign-in attempt for that email in the hour since the first one, and signing in successfully
  does not clear it: it expires after the hour. If you sign in more than 5 times within an
  hour (for example on several devices), expect a short pause. Your administrator can change
  these limits.
- Wrong passwords when changing your password or confirming a change are counted separately
  (5 free, then the same pause); they never block signing in, and a correct password clears
  them.

## Details, language and theme

- **Display name**: 1 to 120 characters, no control or invisible characters.
- **Language**: "Browser language" (the default) follows your browser's language setting;
  English or Polish pins that language on every device.
- **Theme**: the saved theme applies on every device. The dark-mode button in the menu changes
  the theme on one device only; "Use the saved theme on this device" undoes that.

## Sign-in methods

The profile lists the identity providers (OIDC) and the proxy sign-in linked to your account.

- **Link your identity provider**: choose "Link <provider>", confirm, and sign in at the
  provider. You come back to the profile; nothing else about your session changes. The profile
  does not link a second account of a provider that is already linked: remove the old link
  first. (Linking by email at sign-in can still add one; the list shows every linked account
  and each can be removed.)
- **Link the proxy sign-in**: shown when you browse through a reverse proxy that signs you in
  (see [docs/auth/proxy-auth.md](auth/proxy-auth.md)) and that proxy user is not linked yet.
- **Remove a method**: allowed only if another way to sign in remains: a password the current
  settings allow, your identity provider, or the proxy. Removing an identity provider also
  signs out the other devices that signed in with it.

## Confirming changes

Linking, removing a sign-in method and signing out other devices ask for confirmation:

- **With a password**, you enter it. Wrong passwords are counted (separately from sign-in);
  after too many, the page says how many minutes to wait.
- **Without a password** (your account was created through an identity provider or the proxy),
  a sign-in from the last 10 minutes confirms it. After that, the page says "sign in again":
  - with an identity provider: sign out, then "Continue with <provider>" on the sign-in page;
  - through a proxy: sign out, then open any page; the proxy signs you in again.

  You then have 10 minutes for the change. For linking an identity provider, the 10 minutes
  are for starting the link; finishing the sign-in at the provider may take up to 10 more
  minutes. This is weaker than a password: anyone holding your signed-in browser during those
  minutes could make the change.

## Managing accounts (instance administrators)

"Users" in the account menu lists every account with its sign-in methods (password, identity
provider, proxy), its status and its last sign-in. Each change has its own confirmation page
and is confirmed like a profile change: your password, or a sign-in from the last 10 minutes
if your account has none.

- **Deactivate**: the person can no longer sign in, and every session of the account ends at
  once (password, identity provider and proxy sessions alike). **Activate** undoes it.
  Deactivating the only owner of a workspace leaves that workspace without an active owner
  until the account is activated again.
- **Make administrator / Remove administrator**: instance administrators manage every account.
  With `ASSETBOARD_OIDC_ADMIN_GROUP` set, the provider's group decides again at each OIDC
  sign-in of that person ([docs/auth/oidc.md](auth/oidc.md#administrators-from-a-group)), so a
  change made here lasts only until then; the Users page says so and marks those accounts
  "(from the identity provider)". Deactivation does not depend on the group: a deactivated
  account stays deactivated whatever the provider says.
- You cannot deactivate yourself or remove your own administrator role, and the last active
  administrator always stays.
- **Create an account** (not under `ASSETBOARD_OIDC_ONLY`): enter an email and a display name.
  A temporary password is generated and shown **once**, on the page that confirms the account;
  it is not stored in readable form, logged or sent anywhere. Give it to the person. At the
  first sign-in they can only change it (or sign out) until they choose a new password, which
  must differ from the temporary one. An unused temporary password stays valid until it is
  used or the account is deactivated. The new account has no workspace: until a workspace
  owner invites it, it sees "Your account is not a member of any workspace".

## Audit log

The audit log lists who did what and when (in UTC, to the second), newest first, in pages of
50. Each log has its own entry in the account menu; the address `/audit` on its own opens the
workspace log for the owner of the current workspace, otherwise the instance log for an
instance administrator.

- **Workspace** ("Audit log: workspace", for the owner of the current workspace): that
  workspace's events: setup, member role changes and removals, invitations, settings. Each row
  shows who acted and which member it is about, by display name only ("Former member" if that
  account no longer exists).
- **Instance** ("Audit log: instance", for instance administrators): sign-ins and sign-outs,
  failed and refused sign-ins, linked and removed sign-in methods, password changes and account
  administration. Each row also shows the account it is about, the acting account's email and
  the network (an IPv4 /24 or IPv6 /48 prefix, never the full address). It never shows any
  workspace's events: those are only in that workspace's log.

Filters: the kind of event and a date range in UTC (both days included). In the instance log,
"History" next to an account on the Users page shows only the events about that account.
Details are shown only for known events and known fields: reasons, methods, roles and counts.
Names are shown as they are now, not as they were when the event happened. Identity-provider
subjects, tokens and the email addresses a provider reported are never recorded, so they never
appear.
