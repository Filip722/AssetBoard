# Your account

The profile page (`/account`, "Profile" in the account menu) holds your details, your
language and theme, and the ways you sign in. Password changes and signing out other devices
are on "Account security".

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

- **With a password**, you enter it. Wrong passwords count like failed sign-ins.
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
