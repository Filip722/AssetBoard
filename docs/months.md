# Months

Your money is tracked month by month. Open "Month" in the main navigation: it shows the current
month, with its expenses and income (see [Transactions](transactions.md)). Every month has its
own address, such as `/m/2026-10` for October 2026, so you can bookmark it. The address names
only the month, not the workspace: whoever opens the link sees that month in the workspace they
currently have selected. A member who belongs only to your workspace sees your month; a member
of several workspaces may need to switch first.

"The current month" is the month of today's date in the workspace's time zone (Workspace
settings → Time zone). Just after midnight on the 1st, the new month begins at that time in that
zone, not at midnight UTC.

## Moving between months

- **‹ Previous** and **Next ›** go one month back or forward, across years as well.
- **Today** goes back to the current month.
- **Go to month**: choose a month and press **Go**. Most browsers show a month picker. Firefox
  and Safari on a computer show a text box instead: type the month as `2026-10`, or as `10.2026`
  or `10/2026`. An entry that is not a month leaves you where you were and says so.

Every month from January 1900 to December 2199 can be opened, including months with no data yet
and future months, which you can use for planning.

## The month summary

The **Summary** block at the top of the month page shows how the month is going:

- **Earned**, **Spent**, **Saved**, **Net** (earned minus spent minus saved) and the **Savings
  rate** (earned minus spent, as a share of earned; "—" when nothing was earned). Money moved
  between your own accounts (transfer categories) counts nowhere.
- Two bars compare what you spent and earned with what you planned.
- A table with every category in groups (Expenses, Saved / invested, Income, Withdrawn from
  savings): **Planned**, **Actual** and **Difference**, and a total per group. A positive
  difference is always good news: spent less, earned more, saved more or withdrew less than
  planned. A category with entries but no plan shows a plan of 0.00.
- **Largest first** sorts each group by the actual amount; **Your order** uses the order of
  your categories. This browser remembers the choice.

The summary updates as soon as you add, change or delete an entry or a planned value.

Click a category's name in the summary to see only its entries: the other table is hidden, a
bar says which category is shown, and new entries start with that category. **Show all** (or
your browser's Back button) shows everything again. The address includes the category, so
the filtered month can be bookmarked. Choosing a filter (or Show all) reloads the tables, so
anything typed but not yet saved in an entry row is dropped.

## Planning a month

Type the amount you plan for a category in its **Planned** field in the summary:

- **Enter** saves and moves to the next category; leaving the field (Tab or a click elsewhere)
  saves too. Amounts are typed as for entries (`550`, `1 234,56`, `12,5`).
- **0** or an empty field removes the planned value. **Esc** puts back the saved value.
- Transfers are not part of the plan. A planned value of a category that was later archived or
  turned into a transfer stays visible (marked, or under "Transfers (not counted)") so you can
  change or remove it, but such categories take no new planned values.

When a month has no planned values yet and the previous month has some, the block offers **Copy
plan from <previous month>**. One click copies them all; it never overwrites a planned value
that already exists, and it leaves out archived and transfer categories (and says how many).
To have this happen by itself, an owner can turn on "Copy the previous month's plan when a month
is opened for the first time" in [Workspace settings](workspace-settings.md): then the current
month or a later one is filled the first time an editor or owner opens it, once. A month you
emptied on purpose stays empty, and past months always use the button.

Viewers see the planned values but cannot change them. Changes are recorded in the workspace
audit log under "Planned values".

## Closing a month

When a month is done, close it so nothing in it changes by accident:

- **Close month** is offered to editors and owners, for the current month and past months.
  Future months stay open for planning and cannot be closed. You are asked to confirm first.
- A closed month's entries and planned values are **read-only for everyone, owners included**.
  The page shows a "Closed" badge and a banner that says when the month was closed and by whom.
  Every member sees it, viewers included.
- **Reopen month** is offered to owners only. After reopening, the month's entries and planned
  values can change again.
- If someone else closed or reopened the month in the meantime (another tab, a double click),
  the page says the month was already closed, or is already open, and nothing changes.

Closing and reopening are recorded in the workspace audit log under "Closing months".
