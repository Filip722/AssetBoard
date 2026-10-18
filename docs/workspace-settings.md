# Workspace settings

Owners open "Workspace settings" from the workspace menu. The page has:

- **Workspace name**: 1 to 120 characters.
- **Base currency**: the currency every amount in the workspace is entered and stored in.
- **Country**: optional.
- **Time zone**: decides when a day and a month begin for the workspace (for example, which
  month "Today" opens; see [Months](months.md)).
- **Formatting default**: the language used for numbers and dates where no personal preference
  applies. Each member's interface language follows their account and browser.
- **Copy the previous month's plan when a month is opened for the first time** (off by
  default): when an editor or owner opens the current month or a later one that has never had
  planned values, the previous month's planned values are copied into it once. Planned values
  of archived categories and transfers are not copied, and existing planned values are never
  overwritten. A month that once had planned values (even if they were all removed later) and
  past months are never filled automatically; they offer the copy as a button instead.

Changes are recorded in the workspace audit log under "Workspace settings" (which fields
changed, not their values).

## The base currency is locked once amounts exist

Amounts are stored in the base currency and are never converted. So the base currency can be
changed only while the workspace has no transactions and no planned values. Once one exists,
the field is shown locked with the reason, and a change is refused. Deleted transactions count
too: they still hold an amount in that currency and can be restored. A removed planned value
is gone for good (there is no undo for planned values), so it no longer locks the currency:
with no transaction and no planned value left, the currency can change again.

Choose the base currency before you start entering amounts. All other settings can still be
changed at any time.
