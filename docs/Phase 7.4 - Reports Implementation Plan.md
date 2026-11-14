# Phase 7.4 — Reports Implementation Plan

**Status:** Ready for implementation (awaiting explicit owner approval)
**Date:** 2026-11-13
**Baseline:** commit `956f22f`, branch `fix/security-tests-and-tracking-api`
**Contract:** `docs/Phase 7.4 - Reports Decision Record.md` (decisions A1–A20, H1–H9)
**Architecture:** Rev3 §D.8, §D.10, §D.13, §E.2, §F.4–F.5, §H.4, §H.8

This plan is self-contained: an implementer following it top-to-bottom must never
need to invent a requirement. Where this plan is silent, the decision record wins.

---

## 0. Ground rules for the implementer

1. Read-only reports only — no model/migration/service/template-behavior changes
   outside the files listed in §1.
2. Do not touch any file with pre-existing uncommitted modifications except
   `apps/reports/views.py`, `apps/reports/tests_query_performance.py`,
   `apps/reports/tests_reports_audit.py`, and `templates/reports/index.html`,
   which are themselves in-scope edit targets (§1). When editing them, preserve
   every pre-existing working-tree change — edit additively, never revert.
3. Never restore `avg_order`. Never modify the Rev3 document. Never stage,
   commit, push, or generate migrations during implementation.
4. All quantity rendering follows the Phase 7.3 dashboard conventions:
   `Decimal` sums preformatted as strings (`_format_quantity`-style), consumption
   shown as positive magnitude of negative `on_hand_delta`.
5. Test run command (local quirk — see §11): always use the repo venv python and
   `kape_de_manubag.test_settings`, never `py`.

---

## 1. Files expected to change

| File | Change type | Summary |
|---|---|---|
| `apps/reports/views.py` | Modify (already has pre-existing edits — preserve them) | Add `_parse_date_range_bounded`, `_movement_date_bounds`, 2 new views, `_safe_text_cell` helper, `MAX_SALES_RANGE_DAYS`, `MAX_EXPORT_ROWS`; harden `export_excel`; change summary label support, footnotes, `Line items` header in export |
| `apps/reports/urls.py` | Modify | Add 2 routes: `ingredient-usage/`, `cancellation-dispositions/` |
| `templates/reports/index.html` | Modify (already has pre-existing edits — preserve them) | Add tab strip; `Paid orders` → `Paid & completed orders`; packaging-fee footnotes on Top Products + Sales by Category; clamp notice hook |
| `templates/reports/ingredient_usage.html` | **New** | Report A page |
| `templates/reports/cancellation_dispositions.html` | **New** | Report B page |
| `apps/reports/tests_ingredient_usage.py` | **New** | Report A test suite (§8.1) |
| `apps/reports/tests_cancellation_dispositions.py` | **New** | Report B test suite (§8.2) |
| `apps/reports/tests_sales_hardening.py` | **New** | H1–H6 regression suite (§8.3) |
| `apps/reports/tests_export.py` | Modify | Fixture statuses `pending` → valid (H7); export header test `Items` → `Line items` |
| `apps/reports/tests_cancelled_orders.py` | Modify | Fixture statuses `pending` → `awaiting_payment` (H7) |
| `apps/reports/tests_category_sales.py` | Modify | Fixture statuses `pending` → valid (H7) |
| `apps/reports/tests_daily_sales.py` | Modify | Fixture statuses `pending` → valid (H7) |
| `apps/reports/tests.py` | Modify | Comment-only touch-up at the `pending` comment if desired (optional, cosmetic) |

Not changed: models, migrations, services, `base_admin.html`, static CSS/JS, any
finance/dashboard/inventory/orders app file. Not created: `docs/` files other
than the two Phase 7.4 records.

**Fixture-replacement map (H7) — exact sites verified at `956f22f`:**

- `tests_cancelled_orders.py`: `_order(status='pending', ...)` default (line ~66);
  `admin_pending` setup (line ~142); workflow test steps (lines ~344–381);
  exclusion sweep list `['pending', 'preparing', 'ready', 'cancelled', 'completed']`
  (line ~467) and `['pending', 'preparing', 'ready']`-style sweeps (line ~497) —
  replace the `'pending'` string with `'awaiting_payment'` everywhere in this file.
  Do **not** rename the admin username strings (`admin_pending` etc.) — they are
  identifiers, not statuses.
- `tests_category_sales.py` line ~311: `for status in ['pending', 'preparing', 'ready']` →
  `['awaiting_payment', 'preparing', 'ready']`.
- `tests_daily_sales.py` lines ~301, ~318, ~464: same substitution.
- `tests_export.py` line ~280: same substitution. Its docstring line
  "Cancelled / pending orders absent" → "Cancelled / awaiting_payment orders absent".
- Rule: change **only the status string literals**; every assertion, count, and
  total stays byte-identical.

---

## 2. Implementation sequence

Ordered so each step is independently testable and revertible:

| Step | Work | Verified by |
|---|---|---|
| 1 | Fixture replacements (H7) across the four existing test files | Full existing reports suite still green |
| 2 | Hardening of `export_excel` + `_safe_text_cell` + `MAX_SALES_RANGE_DAYS` clamp + `MAX_EXPORT_ROWS` cap; export header rename; index.html label/footnotes/tab strip; clamp notice | New `tests_sales_hardening.py` (§8.3) + existing suite |
| 3 | URL routes + Report A view + `ingredient_usage.html` + Report A tests | `tests_ingredient_usage.py` |
| 4 | Report B view + `cancellation_dispositions.html` + Report B tests | `tests_cancellation_dispositions.py` |
| 5 | Query-budget assertions finalized; full suite + `makemigrations --check`; manual smoke on dev server (desktop + mobile widths) | §11 commands |

Rationale for order: fixture fixes first (so later runs exercise valid states),
hardening second (isolated, existing page only), then the additive reports.

---

## 3. Backend query design

### 3.0 Shared helpers (in `apps/reports/views.py`)

```python
MAX_SALES_RANGE_DAYS = 92   # H2: one quarter; applies to sales page + export
MAX_EXPORT_ROWS      = 5000 # H3: hard cap on exported order rows
DEFAULT_RANGE_DAYS   = 30   # existing fallback behavior, kept

def _parse_date_range_bounded(request, today, max_days=MAX_SALES_RANGE_DAYS):
    """Existing _parse_date_range semantics, plus H2 clamp.
    Returns (start_date_str, end_date_str, was_clamped: bool).
    Clamp rule: if end - start >= max_days, start = end - (max_days - 1)."""
```

- Keep the existing `_parse_date_range` untouched (sales views keep their exact
  fallback semantics); the new helper wraps it: parse → swap-if-inverted → clamp.
- The Sales page and export both switch to `_parse_date_range_bounded` (H2);
  `was_clamped` drives a template notice; the export inherits the same clamped
  range so page and workbook always agree.

```python
def _movement_date_bounds(start_date_str, end_date_str):
    """Aware datetime bounds [start 00:00, end+1 00:00) in the active TZ.
    Same pattern as dashboard Phase 7.3 widgets:
    timezone.make_aware(datetime.combine(d, time.min))."""
```

- Both new views filter with these bounds (`created_at__gte=start,
  created_at__lt=end_exclusive` / same for `decided_at`) so the DB uses raw
  indexed datetime predicates instead of `__date` transforms.

### 3.1 Report A — `ingredient_usage(request)`

```python
@login_required
@admin_required
def ingredient_usage(request):
    today = timezone.localdate()
    start_date, end_date, _clamped = _parse_date_range_bounded(request, today)
    start_dt, end_dt = _movement_date_bounds(start_date, end_date)

    rows = (
        IngredientMovementLog.objects
        .filter(
            movement_type=IngredientMovementLog.MOVEMENT_CONSUMPTION,  # A3
            created_at__gte=start_dt,          # A5 date basis, indexed range
            created_at__lt=end_dt,
        )
        .values(
            'inventory_item__item_type',       # §6.1 classification join
            'item_name',                       # snapshot (survives renames)
            'base_unit',                       # snapshot; grouping dimension
            'reason_code',                     # order_ready / cancelled_prepared
        )
        .annotate(total=Sum('on_hand_delta'))  # physical delta; display -total
    )
```

Post-processing (pure Python, no extra queries):

- Split rows into `ingredient` / `packaging` lists on
  `inventory_item__item_type` (A7). Never compute a cross-type total.
- Within each list, group by `base_unit`; each unit block shows rows for
  `order_ready` and `cancelled_prepared` separately (A4); unknown reason codes
  render under their raw code (`_humanize_reason`-style, fail-visible).
- Magnitudes: display `-(total)` (positive), keep `Decimal` → string
  preformatting for stable rendering (dashboard convention).
- Context: `start_date`, `end_date`, `ingredient_sections` (list of
  `{base_unit, rows:[{item_name, order_ready, cancelled_prepared, other, unit_total}]}`),
  `packaging_sections`, `has_any_consumption` (drives empty state), plus the
  fixed copy strings from §8.1 of the decision record (A10/A11 notes).
- **No other queries.** No `select_related` needed (values() join only).

### 3.2 Report B — `cancellation_dispositions(request)`

```python
@login_required
@admin_required
def cancellation_dispositions(request):
    today = timezone.localdate()
    start_date, end_date, _clamped = _parse_date_range_bounded(request, today)
    start_dt, end_dt = _movement_date_bounds(start_date, end_date)

    rows = (
        OrderCancellationDisposition.objects
        .filter(decided_at__gte=start_dt, decided_at__lt=end_dt)  # A6
        .values(
            'ledger',                        # A15 classification snapshot
            'source',                        # staff_declared/system_*
            'order_item__product_name',      # PROTECT join, snapshot name
        )
        .annotate(
            line_count=Count('id'),
            prepared_total=Sum('prepared_units'),      # A14 — never summed together
            unprepared_total=Sum('unprepared_units'),
            discarded_total=Sum('discarded_units'),    # A12 — display-only figure
            quantity_total=Sum('order_item_quantity'),
        )
    )
```

Post-processing:

- Group rows by `ledger` → ordered display groups: `legacy` first ("Legacy stock
  ledger"), then `ingredient` ("Ingredient ledger"); unknown ledger values (none
  possible per CHECK) would render under their raw code, fail-visible.
- Within a ledger, group by `source` with display labels from
  `SOURCE_CHOICES`; keep product rows sorted by `order_item__product_name`
  then source for determinism.
- Zero-quantity rows are included automatically: any matching disposition row
  contributes to `line_count` even when all sums are 0 (A13). The empty state
  triggers only when the queryset is empty.
- Context: `legacy_groups`, `ingredient_groups`, `has_any_dispositions`, date
  strings, and the fixed A12 copy ("Discarded units are a cancellation
  disposition, not an ingredient consumption figure").
- **No other queries.** No per-row order lookups (order numbers are not required
  by the approved grouping; if a per-order column is wanted at review time, it is
  a `values('order__order_number')` addition to the same single query — flag for
  review, not invented now).

### 3.3 Hardened `export_excel` (existing view, additive edits)

Preserve every existing behavior except the approved changes:

1. `_parse_date_range_bounded` replaces `_parse_date_range` (H2) — clamped range,
   consistent with the page.
2. After building the base queryset: `if orders.count() > MAX_EXPORT_ROWS:`
   return `HttpResponse(message, status=400, content_type='text/html')` with a
   short HTML body: "Export too large: N orders exceed the 5,000-row limit.
   Narrow the date range and try again." (H3: predictable, no workbook bytes, no
   truncation). The `.count()` is one extra query (see budget below).
3. Header row: `'Items'` → `'Line items'` (H6). Column positions unchanged.
4. All **text** columns (Order #, Date-string, Customer, Type, Payment, Cashier)
   are written through `_safe_text_cell(ws, row, col, value)`; numeric Total
   stays `value=order.total` as today (H1: numeric preserved).

```python
def _safe_text_cell(ws, row, col, value):
    """Write a display string as a strict text cell (H1).
    openpyxl 3.1.5 treats a str starting with '=' as a formula; forcing
    data_type='s' stores it as a string cell, so formula-leading characters
    ('=', '+', '-', '@') in user-controlled text can never execute.
    Verified by save/reopen probe (see decision record §10-H1)."""
    cell = ws.cell(row=row, column=col, value=str(value))
    cell.data_type = 's'
    return cell
```

5. Auto-fit loop, headers styling, filename, and `wb.save(response)` unchanged.

### 3.4 Query budgets (verified with `CaptureQueriesContext`)

| View | Budget | Composition |
|---|---|---|
| `ingredient_usage` | **≤ 3** | 1 grouped aggregation + session/auth machinery (same allowance the dashboard widgets get). No per-row queries — snapshots and values() joins only. |
| `cancellation_dispositions` | **≤ 3** | 1 grouped aggregation + auth machinery. No per-row queries. |
| `reports_index` | unchanged | Existing OPT-1 budget must keep passing (tests_query_performance.py regression). |
| `export_excel` | existing + **1** | `.count()` cap check precedes workbook build; row loop unchanged (it already iterates one evaluated queryset). |

Index usage (verified against model Meta):

- Report A filter `movement_type='consumption'` + `created_at` range →
  `idx_ingmv_type_created (movement_type, -created_at)` — the movement-type
  equality is the leftmost column, so the range scan is index-driven.
- Report B filter `decided_at` range → **no index exists** (accepted limitation,
  decision record §9.1). Table scale: one row per cancelled order item. Do not
  add an index.
- Sales views: untouched; existing `idx_order_paid_status_created` /
  `idx_order_finance_sales` continue to serve.

N+1 avoidance rules: `values()` + `annotate` only; never iterate model instances
with FK access; snapshot fields (`item_name`, `base_unit`, `order_number`) are
preferred over joins; the only joins are the two classification joins
(`inventory_item__item_type`, `order_item__product_name`), both PROTECT-backed.

Unbounded-result guards: date clamps bound Report A rows to consumption events
in ≤92 days; Report B rows to cancellations in ≤92 days; the sales export to
5,000 rows. The Python day-fill loop exists only on the existing sales page
(now ≤92 iterations).

---

## 4. URL and authorization design

`apps/reports/urls.py` (app_name `reports` unchanged):

```python
urlpatterns = [
    path('', views.reports_index, name='index'),                     # unchanged
    path('export/excel/', views.export_excel, name='export_excel'),  # unchanged
    path('ingredient-usage/', views.ingredient_usage, name='ingredient_usage'),
    path('cancellation-dispositions/', views.cancellation_dispositions,
         name='cancellation_dispositions'),
]
```

Authorization: both views are decorated exactly like `reports_index`:

```python
@login_required
@admin_required
def ingredient_usage(request): ...
```

- `admin_required` (`apps/accounts/decorators.py`) handles: unauthenticated →
  login redirect with `next=`; non-admin → error message + redirect to
  `dashboard:index`. No new permission code, no groups, no DRF.
- Both views accept GET only (read-only); POSTs to these URLs 405 naturally via
  Django's method routing (views never branch on `request.method` to mutate).

## 5. Template and responsive UI design

### 5.1 Shared tab strip

Small include-style fragment repeated in all three templates (kept as a plain
`{% include %}`-free block or a shared partial `templates/reports/_report_tabs.html`,
implementer's choice — one file either way):

```html
<nav class="report-tabs" style="display:flex;gap:8px;margin-bottom:24px">
  <a href="{% url 'reports:index' %}" class="btn {% if tab == 'sales' %}btn-primary{% else %}btn-outline{% endif %}">Sales Reports</a>
  <a href="{% url 'reports:ingredient_usage' %}" class="btn {% if tab == 'ingredient' %}btn-primary{% else %}btn-outline{% endif %}">Ingredient Usage</a>
  <a href="{% url 'reports:cancellation_dispositions' %}" class="btn {% if tab == 'cancellations' %}btn-primary{% else %}btn-outline{% endif %}">Cancellation Dispositions</a>
</nav>
```

- Each view passes `tab='sales'|'ingredient'|'cancellations'`.
- The sidebar already highlights for the whole `reports` app
  (`base_admin.html:99`), so no sidebar edit.
- Tabs wrap on mobile (`flex-wrap:wrap`); touch targets ≥ 36px height
  (existing `.btn` sizing).

### 5.2 `templates/reports/index.html` (surgical edits only)

1. Tab strip inserted above the Date Filter card; page passes `tab='sales'`.
2. Line ~53: `<div class="stat-sub">Paid orders</div>` →
   `<div class="stat-sub">Paid & completed orders</div>` (H4). Nothing else on
   the two summary cards changes.
3. Top Products card header footnote (small text under header or below table):
   "Excludes packaging fees." (H5)
4. Sales by Category card: same footnote text (H5).
5. Clamp notice: when `was_clamped`, render an info banner above the filter:
   "Date range limited to {{ max_range_days }} days — showing {{ start_date }}
   to {{ end_date }}." (H2 user-visible behavior.)
6. Everything else — chart, tables, mobile cards, JS — byte-unchanged.

### 5.3 `templates/reports/ingredient_usage.html` (new)

Structure (extends `base_admin.html`, mirrors index.html conventions):

- Page header: title "Ingredient Usage", subtitle "Physically consumed
  ingredients and packaging", export button **absent** (A18).
- Tab strip (`tab='ingredient'`).
- Date Filter card: identical From/To + Apply/Reset markup as index.html.
- Section **Ingredients** — for each `base_unit` block: unit heading
  ("Grams (g)" etc. via `InventoryItem.BASE_UNIT_CHOICES` labels), desktop
  table (`table-wrapper reports-table-wrapper`): columns
  Item | Prepared (order_ready) | Cancelled after prep | Total consumed;
  mobile fallback: `reports-mobile-cards` / `report-card-item` rows with the
  same numbers (`item_name` main line, reason figures as sub, total as value).
- Section **Packaging** — same table shape, its own unit blocks. No combined
  ingredient+packaging total anywhere in the DOM (A7).
- Per-item rows: `item_name` + reason-split quantities; a row's "Total consumed"
  is the row's own two figures summed (single base unit ⇒ safe), never a
  cross-item or cross-unit sum.
- Empty state: when `has_any_consumption` is false, both sections render the
  standard `empty-state` block: "No consumption recorded in this period."
- Footer note (A10/A11 copy): "Figures are period activity from the ingredient
  movement ledger and are not a stock reconciliation or current on-hand balance.
  'Cancelled after prep' consumption is not wastage."
- No JS block (no chart).

### 5.4 `templates/reports/cancellation_dispositions.html` (new)

- Page header: "Cancellation Dispositions", subtitle "Product units recorded at
  cancellation".
- Tab strip (`tab='cancellations'`), Date Filter card (same pattern).
- Two ledger groups, always both rendered as section headings:
  "Legacy stock ledger" then "Ingredient ledger" (A15 label requirement).
- Within each ledger group, a desktop table: columns
  Product | Source | Lines | Prepared | Unprepared | Discarded;
  `source` rendered via the human labels (`staff_declared` → "Staff declared",
  `system_ready` → "System (ready)", `system_unpaid` → "System (unpaid)").
  Mobile: `report-card-item` rows (product name main; "Prepared X · Unprepared
  Y · Discarded Z" sub; lines count value).
- Three quantity columns are never collapsed into one total (A14).
- Rows with all-zero quantities are ordinary rows (A13).
- Empty state per ledger group when that ledger has no rows, and a page-level
  empty state ("No cancellations recorded in this period") when the queryset
  is empty.
- Footer note (A12/A16 copy): "Quantities are product units. Discarded units are
  a cancellation disposition, not an ingredient consumption figure."
- No JS block.

---

## 6. Excel hardening implementation approach (H1–H3, H6 — consolidated)

| Item | Mechanism | Evidence / rationale |
|---|---|---|
| H1 injection fix | `_safe_text_cell` forces `data_type='s'` on all display-text cells; numeric Total untouched | openpyxl 3.1.5 save/reopen probe (decision record §10-H1): `'=cmd` + forced `'s'` reopens as string with exact text; plain assignment reopens as `data_type='f'` (formula). Apostrophe-prefix rejected (probe: openpyxl stores the literal quote). Excel only executes formula-typed cells, so type enforcement neutralizes `=`, `+`, `-`, `@` leading text without mangling names like `'- John'`. |
| H1 regression proof | Tests save the response bytes to `BytesIO`, `load_workbook`, assert every Customer/Cashier/Type/Payment/Order#/Date cell has `data_type == 's'` even when the value starts with `=`/`+`/`-`/`@`; assert Total cells `data_type == 'n'` | §8.3 cases SH-1..SH-3 |
| H2 range clamp | `_parse_date_range_bounded` clamps start to `end - 91d`; page notice; export uses same clamp | Budget: `daily_sales` ≤ 92 entries by construction |
| H3 row cap | `.count()` gate before any workbook work; 400 + HTML message; zero workbook bytes on reject | No truncation path exists |
| H6 rename | Header list literal `'Items'` → `'Line items'` | Column order unchanged |

## 7. Date-range and export-limit behavior (deterministic spec)

- Parse: missing/invalid `start`/`end` → defaults (last 30 days) — unchanged
  semantics from `_parse_date_range`.
- Inverted range: swapped silently — unchanged semantics.
- Span ≥ 92 days (`end - start` ≥ 92 days): `start = end - 91 days`, `was_clamped=True`.
  Page shows the notice; export URL buttons on the page therefore always carry the
  clamped range. A hand-crafted export URL with an oversized range is clamped
  identically (same helper), so the cap check below operates on ≤92-day data.
- Export cap: `orders.count() > 5000` → `HttpResponse` 400, HTML body, message
  names the limit and remedy. `count() == 5000` exports normally. There is no
  scenario in which rows are dropped after the cap check.
- Both constants live at module top of `apps/reports/views.py` with the comments
  from §3.3 so future edits see the rationale.

## 8. Test plan

Conventions (match existing suites): `TestCase`, `_user`/`_order`-style local
helpers, `Client` + `reverse()`, `CaptureQueriesContext` for budgets
(pattern: `apps/reports/tests_query_performance.py`), workbook assertions via
`io.BytesIO` + `openpyxl.load_workbook` (pattern: `apps/reports/tests_export.py`).

### 8.1 `apps/reports/tests_ingredient_usage.py`

Data setup helper `_consumption(item, delta, reason, order=None, days_ago=0)`
writes `IngredientMovementLog` rows directly (read-only report; no service calls
needed — constructing movements via `IngredientMovementLog.record()` keeps CHECK
shape valid: consumption ⇒ `reserved_delta == on_hand_delta`).

| ID | Test name | Asserts |
|---|---|---|
| IU-1 | `test_admin_required` | anonymous → login redirect; cashier → redirected with access-denied message; admin → 200 |
| IU-2 | `test_only_consumption_included` | purchase / reservation / reservation_release / wastage / adjustment rows (same item, in-range) produce no figures; consumption row appears |
| IU-3 | `test_order_ready_vs_cancelled_prepared_separated` | one row each reason code → two separate figure cells, correct magnitudes, `cancelled_prepared` figure not merged into `order_ready` |
| IU-4 | `test_cancelled_prepared_not_labelled_wastage` | response contains "Cancelled after prep" (or approved label) and does not contain "wastage" in the row context |
| IU-5 | `test_ingredient_and_packaging_separate` | ingredient item consumption + packaging item consumption render in their own sections; no combined-total markup (assert the combined figure string is absent) |
| IU-6 | `test_units_never_combined` | same item name in g and pcs (two items, same unit labels differ) → two unit blocks, no summed cross-unit row |
| IU-7 | `test_reserved_delta_never_summed` | a `reservation` row with large `reserved_delta` and a `reservation_release` row change nothing |
| IU-8 | `test_date_range_respects_created_at` | consumption 31 days back excluded on default 30-day window; in-range included; explicit range boundaries honored |
| IU-9 | `test_snapshot_name_and_unit_rendered` | after renaming the `InventoryItem`, page still shows the movement's `item_name`/`base_unit` snapshot |
| IU-10 | `test_magnitudes_positive` | `on_hand_delta=-250.000` renders as `250` (positive magnitude) |
| IU-11 | `test_empty_state` | no consumption → "No consumption recorded in this period." |
| IU-12 | `test_no_reconciliation_copy` | page contains the "not a stock reconciliation" note (A10 copy) |
| IU-13 | `test_unknown_reason_code_fail_visible` | consumption row with `reason_code='weird'` renders under a visible "Weird" label rather than disappearing |
| IU-14 | `test_query_budget` | with 40 items × 30 movements, `CaptureQueriesContext` count ≤ 3 |

### 8.2 `apps/reports/tests_cancellation_dispositions.py`

Setup helpers create `Order` + `OrderItem` (valid statuses only, post-H7) +
`OrderCancellationDisposition` rows directly (append-only record; report is
read-only). Ledger values mirror real writes: `ledger` copied from the item's
`inventory_mode`.

| ID | Test name | Asserts |
|---|---|---|
| CD-1 | `test_admin_required` | same matrix as IU-1 |
| CD-2 | `test_zero_quantity_dispositions_included` | `system_unpaid` row (prepared=0, discarded=0) appears with correct line count and zero sums |
| CD-3 | `test_prepared_unprepared_discarded_separate` | row with prepared=2, unprepared=1, discarded=1 renders three distinct figures; no combined total |
| CD-4 | `test_ledger_labels_present` | legacy group labeled "Legacy stock ledger", ingredient group "Ingredient ledger"; rows land in the correct group |
| CD-5 | `test_product_units_only` | response shows product names and unit counts; no g/ml/pcs strings from inventory items (create ingredient movements too, assert they never leak into this page) |
| CD-6 | `test_discarded_is_disposition_only` | page contains the A12 note; discarded figure equals disposition value, and Report A page (IU context) is unchanged by the disposition's existence |
| CD-7 | `test_date_basis_is_decided_at` | order `created_at` inside window but `decided_at` outside → absent; reversed case → present (pins A6) |
| CD-8 | `test_source_labels` | all three `source` values render their display labels |
| CD-9 | `test_legacy_and_ingredient_both_listed` | one row per ledger → both groups populated |
| CD-10 | `test_empty_state` | empty range → page-level empty state + per-ledger empty copy |
| CD-11 | `test_query_budget` | with 40 orders × 3 items cancelled, query count ≤ 3 |
| CD-12 | `test_deterministic_ordering` | two same-name products/rows render in stable order (name, then source) |

### 8.3 `apps/reports/tests_sales_hardening.py` (H1–H6)

| ID | Test name | Asserts |
|---|---|---|
| SH-1 | `test_formula_customer_name_stays_text` | customer `'=HYPERLINK("http://x","y")` → exported cell reopens `data_type == 's'`, value preserved |
| SH-2 | `test_formula_prefix_variants_stay_text` | names starting `+`, `-`, `@`, `=` all reopen as `'s'` |
| SH-3 | `test_numeric_cells_stay_numeric` | Total cells reopen `data_type == 'n'`; sum of Total column matches page revenue (existing OPT regression spirit) |
| SH-4 | `test_header_renamed_line_items` | header row cell = "Line items"; column count unchanged |
| SH-5 | `test_summary_label_paid_completed` | index page contains "Paid & completed orders", not the bare "Paid orders" sub-label |
| SH-6 | `test_packaging_fee_footnotes` | both footnote strings present on the page |
| SH-7 | `test_range_clamped_over_limit` | request `start = today-200d` → effective `start = end-91d`; notice banner present; page 200 |
| SH-8 | `test_range_within_limit_not_clamped` | 10-day request → no notice |
| SH-9 | `test_export_uses_clamped_range` | oversized export request → filename/rows reflect clamped window (orders outside clamp absent) |
| SH-10 | `test_export_row_cap_rejects` | 5,001 orders (bulk `bulk_create` + `update` for speed) → 400, message mentions limit, body is not an xlsx (`content_type` not spreadsheetml) |
| SH-11 | `test_export_row_cap_boundary_ok` | ≤ cap → 200 spreadsheetml, row count = orders + header |
| SH-12 | `test_index_content_intact` | daily chart data still present; two summary cards still render (existing behavior preserved) |

### 8.4 H7 verification

Not a runtime test: after Step 1, `grep -rn "status='pending'" apps/reports/`
returns zero hits, and the full existing reports suite passes unchanged —
proving the substitutions altered only invalid state strings, not coverage.

---

## 9. Validation commands

Local quirk (project memory): the default settings' staticfiles manifest breaks
`apps.inventory` on untracked CSS — always run tests with CI's settings module,
and always use the repo venv python (not `py`).

```bash
# Full reports suite (new + existing)
venv/Scripts/python.exe manage.py test apps.reports --settings=kape_de_manubag.test_settings

# Targeted per step
venv/Scripts/python.exe manage.py test apps.reports.tests_sales_hardening --settings=kape_de_manubag.test_settings
venv/Scripts/python.exe manage.py test apps.reports.tests_ingredient_usage --settings=kape_de_manubag.test_settings
venv/Scripts/python.exe manage.py test apps.reports.tests_cancellation_dispositions --settings=kape_de_manubag.test_settings

# Adjacent regression (sales path neighbors)
venv/Scripts/python.exe manage.py test apps.reports.tests_query_performance apps.reports.tests_export --settings=kape_de_manubag.test_settings

# No model changes slipped in
venv/Scripts/python.exe manage.py makemigrations --check --dry-run --settings=kape_de_manubag.test_settings

# Tree hygiene
git status --short
git diff --check
```

CI parity: the same suite runs with `kape_de_manubag.test_settings` in CI (both
jobs) — no CI file changes are expected or allowed in this phase.

Manual smoke (optional, pre-handoff): run the dev server with
`DJANGO_SETTINGS_MODULE=kape_de_manubag.test_settings`, log in as an admin,
visit all three tabs at desktop and ~390px widths, submit a formula-shaped
customer name on a test order, export, reopen the workbook.

## 10. Regression risks

| Risk | Mitigation |
|---|---|
| Export header rename (H6) breaks an external consumer that parses column 5 by name | Column position and count unchanged; header change documented in decision record §10; grep repo for `Items`-column consumers before merge (none expected — export is user-facing only) |
| Range clamp (H2) changes results for admins who filter >92 days | Visible notice states the effective range; clamp is deterministic (most recent 92 days); no data deleted |
| Export cap (H3) blocks a legitimate giant export | 400 message names the remedy (narrow range); cap constant is one documented line to adjust later |
| Fixture substitution (H7) accidentally changes coverage | Rule in §1: only status strings change; §8.4 grep + full-suite pass proves it |
| Editing pre-modified files (`views.py`, `index.html`) | Additive edits only; before starting, record `git diff` hashes of those files and re-verify pre-existing hunks survive at the end (§11 tree check) |
| `data_type='s'` override vs future openpyxl upgrade | SH-1/SH-2/SH-3 round-trip tests fail loudly if the mechanism changes; openpyxl is pinned (3.1.5) |
| New views accidentally coupled to sales logic | Views share no queryset code with `_base_orders_qs`; IU/CD suites build independent fixtures |
| Tab strip regresses existing page JS/layout | SH-12 asserts chart data + cards intact; template edits are additive blocks |

## 11. Rollback considerations (no destructive Git operations)

- Everything ships as normal commits on the feature branch; rollback = `git revert`
  of the Phase 7.4 commit(s) — no resets, no force operations, working-tree
  preservation rules intact.
- Step isolation enables partial revert: Step 1 (fixtures) and Step 2 (hardening)
  are separable commits from Steps 3–4 (new reports), so the owner can keep
  hardening while deferring the new pages, or vice versa.
- New URL routes and templates are purely additive — reverting views/urls
  restores the previous reports app exactly; no data or schema artifacts exist
  (read-only feature, no migrations).
- The `data_type='s'` mechanism changes only newly generated workbooks; old
  exports are files on disk, unaffected.

## 12. Definition of done

1. All §8 tests pass locally and in CI (both jobs) with
   `kape_de_manubag.test_settings`.
2. `makemigrations --check --dry-run` clean; `git diff --check` clean.
3. Pre-existing working-tree modifications and untracked files preserved
   (verified by `git status --short` matching the pre-implementation snapshot
   plus only the §1 file set).
4. `grep -rn "status='pending'" apps/reports/` empty (H7).
5. Manual smoke checklist (§9) observed once on a dev server.
6. No file outside §1 changed; no staging/committing/pushing beyond what the
   owner explicitly authorizes at handoff.
