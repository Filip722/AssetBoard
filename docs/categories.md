# Categories

Categories label your expenses and income. Open "Categories" in the main navigation. Everyone in
the workspace can see them; editors and owners can change them, and viewers see the list
read-only.

## Kind and treatment

Each category has a **kind** and a **treatment**:

| Kind | Treatment | Counted as |
|---|---|---|
| Expense | Regular | Spending |
| Expense | Savings | Money put aside or invested ("saved", not "spent") |
| Expense | Transfer | Money moved between your own accounts: left out of the totals |
| Income | Regular | Earnings |
| Income | Savings | A withdrawal from savings (not counted as earnings) |
| Income | Transfer | Money moved between your own accounts: left out of the totals |

Names are unique per kind among active categories, ignoring upper and lower case (also for
letters such as "Ż" and "ż"): you can have an expense "Other" and an income "Other", but not two
active expense categories "Food" and "FOOD". Names have 1 to 80 characters, without control or
invisible characters; emoji, including joined ones such as "👨‍👩‍👧", are fine. A few
characters expand a lot when names are compared (for example "ﷺ"), so a name made of many of
them can be refused. Each category also has a colour.

## Suggested categories

Every new workspace starts with these categories, named in the language you use the app in
when you create it (at first-run setup, registration, or your first sign-in through an
identity provider or a proxy). `assetboard create-admin` names them in its `--locale`, or in
the instance's default language:

- Expenses: Food, Eating out, Gifts, Health, Housing, Transport, Cosmetics, Hobbies, Utilities,
  Travel, Entertainment, Education, Taxes, Clothes, Other.
- Savings (expense, treatment savings): Savings, Investments.
- Income: Salary, Bonus, Interest, Sale of items, Other.
- Savings withdrawal (income, treatment savings).

The names follow the language the creating page was shown in, which may differ from the
workspace's formatting default chosen in the same form.

After that they are ordinary categories: rename, reorder, archive or delete them as you like.

A workspace created before this, or one whose categories were all deleted, has none: while a
workspace has no categories at all (archived ones count), the page offers **Add suggested
categories**, which adds the same set.

## Changing categories

- **Edit** changes the name, kind, treatment and colour in place.
- **↑ / ↓** move a category within its list. The order is used wherever categories are listed.
- **Archive** hides a category from new entries but keeps its history; archived categories are
  listed under "Archived categories", where **Restore** brings one back at the end of its list.
  A category cannot be restored while an active category of the same kind has its name.
- **Delete** is offered only for categories that nothing uses. Deleting a category that is in
  use (for example, by transactions) archives it instead, and the page says so.

Once a category is in use:

- its **kind cannot change** (an expense category cannot become an income category);
- its **treatment can change**: the edit form shows how many transactions the change applies
  to. The change applies to all of them, in every month, **closed months included**: when some
  of them lie in closed months, the form warns and names those months, for example "This
  changes the totals of 2 closed months: 2026-08, 2026-09." (at most 12 are listed). Closing a
  month protects its entries, not the categories they use.

Changes to categories (but not reordering) are recorded in the workspace audit log under
"Categories".
