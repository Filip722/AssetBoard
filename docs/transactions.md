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

## Audit log

Adding, changing, deleting and restoring entries is recorded in the workspace audit log under
"Transactions", with the date, the category and the amount. The description is never recorded
there.
