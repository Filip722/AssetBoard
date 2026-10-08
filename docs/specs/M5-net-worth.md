# M5 — Net worth

**Goal:** list what you own and what you owe, grouped into categories, compare the two, and track net worth over time.

**Exit criteria:** assets and liabilities (manual values with history), category groupings, a summary with net worth, and monthly snapshots producing a trend chart. Linked valuations are *designed for* here and *filled in* by M6 (portfolio) and M7 (loans).

Every agent prompt starts with: *"Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go)."*

**Data model (sketch)**
- `NetWorthCategory(id, workspace_id, side[asset|liability], name, color, sort_order, archived_at)`
- `NetWorthItem(id, workspace_id, side, category_id, name, notes, valuation_mode[manual|linked], link_type?, link_id?, include_in_net_worth bool default true, investable bool default false, acquired_on?, archived_at)`
- `ItemValuation(id, workspace_id, item_id, as_of date, value_minor, currency, note)` — append-only; "current manual value" = latest `as_of ≤ today`.
- `NetWorthSnapshot(id, workspace_id, as_of date, assets_minor, liabilities_minor, net_minor, currency, breakdown JSON[{category_id, side, total_minor}], source[auto|manual|backfill])`; unique `(workspace_id, as_of, source)`.
- `link_type` values reserved now: `portfolio_asset_class`, `portfolio_holding`, `loan_balance`. Until the owning module exists, the UI does not offer linked mode.

---

## F5.1 Categories and items (assets and liabilities)

**Story:** I maintain a list of my possessions and debts, grouped by category.

**Acceptance**
- Two tabs (Assets, Liabilities) each listing items grouped under their categories with subtotals; add/edit/archive items inline; category CRUD (separate category sets for assets and liabilities), reorder.
- Item fields: name, category, current value, currency (base currency by default), notes, optional acquired date, `include_in_net_worth`, `investable` (feeds M8).
- Changing a manual value appends an `ItemValuation` (with optional note); an "as of" date can be set (back-dating allowed).
- Valuation history shown per item (small table + sparkline).
- Bulk add: paste multiple lines (`name; value; category`) into a textarea → preview → create.
- Seed categories (en + pl): assets — Vehicles, Electronics, Audio equipment, Instruments, Appliances, Jewelry & watches, Photography, Other; liabilities — Installments, Mortgage, Other.
- Soft delete/archive keeps history.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F5.1 from docs/specs/M5-net-worth.md: NetWorthCategory, NetWorthItem, ItemValuation models + migration, workspace-scoped repositories/services,
grouped lists with subtotals and inline editing (HTMX), append-only valuation history, bulk-paste add with preview, seed categories (en/pl).
Money is Money only. Tests: valuation append semantics, back-dated valuations, current value resolution, archive, role checks.
```

---

## F5.2 Summary: assets vs liabilities and net worth

**Acceptance**
- Pure domain function `summarize_net_worth(items_with_values) -> NetWorthSummary`: total assets, total liabilities, net worth, per-category totals and shares; items with `include_in_net_worth=false` excluded.
- Summary page: big net-worth figure, assets-vs-liabilities bar, donut of assets by category, table of categories with totals and % of assets; liabilities likewise.
- "Debt-to-assets ratio" and "liquid vs illiquid" are **not** required (do not implement).
- Currency conversion for non-base-currency items uses the FX service if M6 exists; before that, non-base items are shown separately with a warning and excluded from totals (documented limitation).

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F5.2 from docs/specs/M5-net-worth.md: pure summarize_net_worth domain function, summary page with totals, bar, donut (ECharts) and category tables.
Property tests: net = assets - liabilities; category totals sum to side totals; excluded items never counted.
```

---

## F5.3 Snapshots and net-worth history

**Story:** I see how net worth changed over time.

**Acceptance**
- Monthly APScheduler job (1st of month 04:00 workspace time) stores a `NetWorthSnapshot` (values as of the last day of the previous month). Idempotent.
- "Take snapshot now" button (editor+); the latest manual snapshot for a date replaces the previous manual one for the same date.
- **Backfill** from valuation history: for each past month-end, compute from `ItemValuation`s (latest valuation ≤ date); marked `source=backfill` and replaceable by real snapshots.
- Line chart of net worth (with assets and liabilities lines toggleable) over time; range selector (1y/3y/5y/all); hover shows the breakdown by category.
- Linked items are included in snapshots using their value at that moment (once M6/M7 exist); if a link cannot be resolved the snapshot records the item as "unavailable" and the report flags it.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F5.3 from docs/specs/M5-net-worth.md: NetWorthSnapshot model + migration, monthly job (idempotent), manual snapshot button,
backfill from ItemValuation history (source=backfill), history chart with range selector and category breakdown tooltip.
Tests with the fixed Clock: month-end selection, idempotency, backfill semantics, replacement rules.
```

---

## F5.4 Link framework (for portfolio and loans)

**Acceptance**
- `ValuationResolver` interface in `services/networth/links.py`: `resolve(link_type, link_id, as_of) -> Money | Unavailable`; registry where later modules register resolvers.
- UI: item form shows "Link to…" only for registered link types; linked items display value read-only with a "linked to <name>" chip and a click-through.
- A linked item cannot be given manual valuations; unlinking converts to manual keeping the last resolved value as a first valuation.
- Tests with a fake resolver.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F5.4 from docs/specs/M5-net-worth.md: the ValuationResolver registry and the linked-item UI/service behaviour described.
Only a fake resolver exists now; real ones are added in M6 and M7. Tests for link/unlink semantics and the 'unavailable' path.
```

---

## F5.5 Importer extension: assets and liabilities

**Acceptance:** importer reads the assets/liabilities sheet (observed layout: assets in columns B–D = description, value, category; liabilities in F–H). The parser reads the sheet twice: once for cached values (`data_only=True`) and once to read the formula *text* (never evaluated) so it can detect references. Rows whose value cell is a **formula referencing another sheet** (e.g., investments or loan balance) are not imported as manual items; they are listed as "linked in the workbook — configure link later". Manual rows become items with an initial valuation dated today (user may choose the date). Category mapping reuses the F4.3 UI. Dry-run, rollback and idempotency follow F4.1.

**Agent prompt**
```
Follow the standard workflow in docs/specs/00-overview.md §8 (plan first, wait for my go).
Implement F5.5 from docs/specs/M5-net-worth.md as an extension of the M4 importer framework: parse the assets/liabilities sheet layout described,
skip cross-sheet formula rows (report them as 'linked'), create items + initial valuations, reuse the category mapping UI.
Extend scripts/make_sample_workbook.py with a synthetic assets/liabilities sheet (including cross-sheet formula cells). Tests on it.
```
