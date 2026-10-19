# Transactions

Expenses and income are entered on the month page ("Month" in the main navigation, or an
address such as `/m/2026-10`). The page has two tables, **Expenses** and **Income**, with one row
per entry, ordered by date and then by the order you entered them.

Every entry has a date, an amount, an optional description and a category. Amounts are in the
workspace's base currency (Workspace settings); once an entry exists, the base currency can no
longer be changed.

## Adding entries

Editors and owners see an empty **entry row** at the bottom of each table. Type into it the way
you would type into a spreadsheet:

1. **Day**: the day of the month you are looking at (`7`), or a full date (`2026-10-07` or
   `07.10.2026`). A full date in another month is saved to that month; the page says so and
   links to it.
2. **Amount**: as you write it in your language, for example `12.50`, `12,5`, `1,234.56` or
   `1 234,56`. It must be greater than zero, with no more decimal places than the currency has.
3. **Description** (optional, up to 200 characters).
4. **Category**: only the active categories of the table's kind are offered (expense categories
   in the Expenses table, income categories in the Income table).

Keys:

- **Tab** moves to the next field (Shift+Tab to the previous one).
- **Enter** saves the row, from any field. The row is ready for the next entry at once: it
  keeps the day (as a day of the month, or the full date you typed if the entry went to another
  month), the other fields are empty, and the cursor is in the Day field with the day selected.
  Type a new day to replace it, or press Tab to keep it. You can go on typing while the entry
  is being saved; nothing you type is lost. Each table keeps its own day.
- **Esc** clears the entry row, the day included.

If something is wrong, nothing is saved: every field with a problem is marked and explained,
and the cursor goes to the first one. The row shows what you had entered for that entry.

A table without any active category of its kind shows a link to the Categories page instead of
an entry row.

## Suggestions

While you type a description, earlier descriptions of the same table that start with what you
typed are listed (most used first; upper and lower case do not matter). Use the arrow keys and
Enter, or click, to pick one: it fills the description and selects the category it was used with
most often. Esc closes the list. Deleted entries and archived categories are never suggested.

Suggestions need JavaScript; without it, type the description and choose the category yourself.

## Editing and deleting

- **Edit** turns the row into fields. Change what you need and press Enter (or **Save**); Esc
  (or **Cancel**) leaves the row unchanged. A row keeps its archived category if it has one,
  but archived categories cannot be chosen anew. Changing the date to another month moves the
  entry there.
- **Delete** removes the entry at once. A message at the bottom of the page offers **Undo** for
  ten seconds and counts them down; after that the deletion stays. Anyone who can edit can undo,
  not only the person who deleted. An entry you are typing in the entry row meanwhile stays
  there.

## Closed months and viewers

In a closed month (see [Months](months.md)) the tables are read-only for everyone: there is no
entry row and no Edit or Delete. If someone closes the month while you are entering, your entry
is not saved and the page reloads with the closed-month message. A full date in another month
that is closed is refused with a message on the Day field.

Viewers see the tables but no entry row, Edit or Delete.

## Without JavaScript

Every form also works as a plain web form: saving, editing and deleting reload the page. After a
save the entry row keeps the day, as with JavaScript. After a deletion the page offers Undo
while the ten seconds have not passed.

## Searching all entries

**Transactions** in the main navigation lists the entries of every month, newest first, 50 per
page. Filter them by:

- **From** and **To**: dates, both included (`2026-10-07` or `07.10.2026`);
- **Smallest amount** and **Largest amount**: typed as on the month page (`12,5`, `1 234,56`),
  both included;
- **Description contains**: any part of the description; upper and lower case do not matter;
- **Kind**: expenses, income or both;
- **Categories**: tick one or more (archived categories are listed too); none ticked means all.

**Order** sorts by date or by amount. The filters are part of the page address, so you can
bookmark a search. Each date links to its month, where you edit or delete the entry. Entries in
closed months are marked "closed". Deleted entries are never listed.

## Exporting as CSV

**Download CSV** saves every entry that matches the filters (not only the current page), oldest
first. The file has the columns of `transactions.csv` in the workspace archive:
`date,direction,amount,currency,category,description,category_id,id`. Dates are `YYYY-MM-DD`,
directions are `expense` or `income`, and amounts are plain numbers with the currency's
decimals (`12.50`), whatever your language.

Options:

- **Delimiter**: comma (standard) or semicolon.
- **Decimal separator**: dot (standard) or comma. A decimal comma needs the semicolon delimiter.
  Spreadsheets set up for Polish usually open semicolon + comma files directly.
- **For Excel**: adds a byte order mark at the start, so that Excel reads letters such as "ż"
  correctly.
- **Protect against spreadsheet formulas** (off unless you tick it): puts an apostrophe before
  `=`, `+`, `-` or `@` (also their full-width forms) at the start of a description or category
  name (also after leading spaces, or when it starts with a tab), and after a comma or semicolon
  inside it (`Rolls, -5 off` becomes `Rolls, '-5 off`), so that a spreadsheet shows such text
  instead of running it as a formula. Use it when you open the file in a spreadsheet; the
  protected values then differ from the stored ones, and a description near the 200-character
  limit may become too long to import again.

With the standard options the file can be imported again as a `transactions.csv`; with the
semicolon and decimal comma, choose the same options when importing. Every member, viewers
included, can export. Exports are not recorded in the audit log.

## Changing the category of many entries

Editors and owners can move entries to another category in one step:

1. Filter the list, then tick the entries to change, or use **Change all N matching entries**
   for every entry that matches the filters (all pages).
2. Choose the **New category** and press the button. A confirmation page shows how many entries
   move, from which categories and in which months. Nothing changes until you confirm.

The new category must be active and of the same kind as the entries; to change expenses, filter
by expenses first. If any of the entries is in a closed month, nothing changes and the message
lists those months (an owner can reopen them). If the selected entries changed in the meantime
(someone added or deleted one, or an edit moved one into or out of the filters), nothing changes
and you review the change again. One change
covers at most 10 000 entries (the **Change all** button is greyed out above that), and at most
500 ticked entries.

## Audit log

Adding, changing, deleting and restoring entries is recorded in the workspace audit log under
"Transactions", with the date, the category and the amount. The description is never recorded
there. A change of category for many entries is one event, with the new category, the number of
entries, the previous categories and the months (no amounts).
