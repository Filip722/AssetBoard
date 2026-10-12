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
