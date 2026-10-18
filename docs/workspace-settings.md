# Workspace settings

Owners open "Workspace settings" from the workspace menu. The page has:

- **Workspace name**: 1 to 120 characters.
- **Base currency**: the currency every amount in the workspace is entered and stored in.
- **Country**: optional.
- **Time zone**: decides when a day and a month begin for the workspace (for example, which
  month "Today" opens; see [Months](months.md)).
- **Formatting default**: the language used for numbers and dates where no personal preference
  applies. Each member's interface language follows their account and browser.

Changes are recorded in the workspace audit log under "Workspace settings" (which fields
changed, not their values).

## The base currency is locked once amounts exist

Amounts are stored in the base currency and are never converted. So the base currency can be
changed only while the workspace has no transactions. Once one exists, the field is shown locked
with the reason, and a change is refused. Deleted transactions count too: they still hold an
amount in that currency and can be restored. (When planned values arrive, they will lock the
currency in the same way.)

Choose the base currency before you start entering amounts. All other settings can still be
changed at any time.
