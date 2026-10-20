# Recurring items

Open **Recurring** to define planned expenses or income once. Editors and owners can add or
change items; viewers can read the list. An item has a name, an active category matching its
direction, a fixed or estimated amount in the workspace currency, optional notes, and a rule.

Choose weekly weekdays, a monthly day (including the last day or first/last working day), or a
yearly month and day. The interval means every N weeks, months or years. Weeks start on Monday;
intervals are anchored to the start date's week, month or year. Start/end dates are inclusive
limits on scheduled dates, before any holiday adjustment. A day that does not exist is clamped:
31 February becomes the last day of February; yearly 29 February becomes 28 February in a
non-leap year.

The form previews the next six effective due dates as you change the rule. JavaScript is needed
only for the live preview; the form can be saved without it. A finite or ended rule may have
fewer than six dates remaining.

## Working days and holidays

Working days are Monday through Friday, excluding the workspace country's national public
holidays, including observed holidays supplied by `python-holidays`. No subdivision is selected.
If the workspace has no country, only weekends are excluded. Unsupported countries are refused
when the rule needs working-day calculations; ordinary unadjusted dates still work.

Choose **Keep the date**, **Previous working day**, or **Next working day**. An adjustment can
cross a month or year boundary, or shift a date before the rule's start/after its end. The
optional maximum count counts the scheduled dates before shifting; dates that land on the same
effective day merge into one occurrence, so there can be fewer stored occurrences than the count.

## Generation and history

Saving immediately generates planned occurrences from today in the workspace time zone through
the same day 13 calendar months ahead, inclusive (clamped at month end). Administrators can set
`ASSETBOARD_RECURRENCE_HORIZON_MONTHS` to another positive number. **Include past occurrences** on
creation also backfills dates from the start of the rule; it is off by default. Generation is
idempotent: repeating a job never creates a second occurrence for the same item and due date.

An APScheduler job runs daily at 03:00 in each workspace's time zone. Startup catches up the
forward horizon; new workspaces and time-zone changes are picked up within one minute.
Use one application process/worker, as required by the deployment guide. Job failures log the
workspace identifier without financial data and can be retried by saving the rule or restarting.

Editing replaces only **pending** occurrences whose due date is today or later. Overdue,
posted and skipped occurrences keep their identity, amount and other data. Pausing or archiving
removes pending occurrences from today onward and stops generation. Resuming regenerates the
forward horizon without creating new overdue dates. Deleting removes an item only if no history
remains; otherwise it archives it. Archived items remain visible for history.

Categories referenced by rules or occurrences cannot change direction or be deleted; deleting
the category archives it instead. Stored recurring amounts also prevent changing the workspace's
base currency. Choose a new active category when editing a rule whose category was archived.

These are planned dates, not posted transactions. The Upcoming inbox, payment/skip/reschedule
actions, automatic posting and reminders belong to later M3 features (F3.2/F3.6).
