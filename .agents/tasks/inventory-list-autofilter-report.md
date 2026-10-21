# Phase 6B — Inventory List Automatic Search and Filtering: Final Report

## 1. Pre-flight Findings

Before this phase the Inventory List had no search or filter controls of any kind.

**View (`apps/inventory/views.py`)** — `inventory_list` fetched all products with
`Product.objects.select_related('category').order_by('stock_quantity')` and passed
the complete queryset to the template with no filtering. No GET parameters were
read. There were no summary-card aggregates; the template had no card section.

**Template (`templates/inventory/list.html`)** — Contained a product table and a
restock modal only. No filter bar, no summary cards, and no JavaScript beyond the
restock-modal AJAX handler.

**Tests** — `apps/inventory/tests.py` covered `InventoryLogSearchTests` (log-page
search, Phase 1), `InventorySummaryCardTests` (summary card counts, added earlier
in Phase 6B pre-work), and `RestockInputRobustnessTests` (non-numeric restock
input). No tests existed for list-level filtering.

**Pagination** — The inventory list had no pagination. The decision not to add it
was confirmed (see §6 below).

---

## 2. Files Changed

| File | Change |
|---|---|
| `apps/inventory/views.py` | Pre-filter aggregate for summary cards; text-search, category, and stock-status queryset filtering; categories queryset for the dropdown |
| `templates/inventory/list.html` | Summary-card grid; filter bar form (text, category, stock-status); conditional Clear link; contextual empty-state message; JavaScript IIFE for automatic filtering |
| `apps/inventory/tests.py` | `InventoryListFilterTests` — 17 new regression tests |

No other files were changed. The restock view, inventory log view, audit service,
order views, finance views, and all authentication/permission decorators are
untouched.

---

## 3. Filter Fields and Exact Behavior

### Text search — `?q=`

- Input element: `<input type="text" name="q" id="inv-search" …>`
- Server-side: `products.filter(name__icontains=q)` after `.strip()`
- Whitespace-only values strip to `''` and no filter is applied
- Partial, case-insensitive matches (Django `icontains` on the database collation)
- JavaScript behavior: `input` event on `#inv-search` with 350 ms debounce via
  `setTimeout` / `clearTimeout`; navigates to `buildUrl()` after the delay

### Category dropdown — `?category=`

- Select element: `<select name="category" id="inv-category" …>`
- Server-side: `products.filter(category_id=int(category_filter))`; non-integer
  values are caught by `try/except (ValueError, TypeError)` and reset to `''`
- Empty value (`""`) means "All Categories" — no filter applied
- JavaScript behavior: `change` event on `#inv-category`; calls `clearTimeout`
  (cancels any pending text debounce) then navigates immediately

### Stock-status dropdown — `?stock_status=`

- Select element: `<select name="stock_status" id="inv-stock-status" …>`
- Four valid values: `out_of_stock`, `critical`, `low`, `ok`
- Unrecognised values are reset to `''` (guarded by `if stock_status_filter in
  VALID_STOCK_STATUSES`)
- JavaScript behavior: same as category — immediate navigation with debounce
  cancellation

### URL construction

`URLSearchParams` is used inside `buildUrl()`. Each parameter is set only when
non-empty, so empty params are omitted. The URL is anchored to
`window.location.pathname` to prevent parameter accumulation on repeated
navigation. Spaces, ampersands, equals signs, and non-ASCII characters are encoded
correctly by the browser's `URLSearchParams` implementation.

### Other automatic-filter invariants

- Every dropdown change cancels any pending text debounce before navigating.
- Page reloads reset to page 1 (pagination is not present; this is implicit).
- Active filters are preserved when one filter changes (all params are re-built
  from current control values inside `buildUrl()` on every navigation).
- No duplicate event handlers — all listeners are attached once inside an IIFE
  that executes only after the DOM is ready.

---

## 4. Stock-Status Filter ORM Definitions

The four ORM conditions match the `Product.stock_status` property exactly:

| Dropdown value | ORM condition | Maps to property value |
|---|---|---|
| `out_of_stock` | `stock_quantity=0` | `'out_of_stock'` |
| `critical` | `stock_quantity__lte=CRITICAL_STOCK_THRESHOLD` (≤ 5) | `'critical'` (includes qty=0) |
| `low` | `stock_quantity__lte=F('low_stock_threshold')` | `'low'` |
| `ok` | `stock_quantity__gt=F('low_stock_threshold')` | `'ok'` |

**Important boundary note:** `critical` (≤ 5) is a strict superset of
`out_of_stock` (= 0). A product at zero stock is both out-of-stock and critical.
This is intentional — it matches the model property and was confirmed as correct in
the Phase 6B review. The test `test_inventory_list_stock_status_critical` asserts
two results (Butter Cookie at qty=0 and Chocolate Cake at qty=3), verifying this
overlap.

`CRITICAL_STOCK_THRESHOLD` and `low_stock_threshold` are imported/used unchanged
from their existing definitions in `apps/menu/models.py`. No new thresholds were
introduced.

---

## 5. Summary Card Design

Four cards are rendered above the filter bar:

| Card | Context key | Value | Computed from |
|---|---|---|---|
| Total Products | `total_count` | `Count('id')` | unfiltered `base_qs` |
| Active Products | `active_count` | `Count('id', filter=Q(is_active=True))` | unfiltered `base_qs` |
| Low Stock Items | `low_stock_count` | `Count('id', filter=Q(stock_quantity__lte=F('low_stock_threshold')))` | unfiltered `base_qs` |
| Out of Stock | `out_of_stock_count` | `Count('id', filter=Q(stock_quantity=0))` | unfiltered `base_qs` |

All four values are produced by a single `.aggregate()` call on `base_qs` **before
any GET parameter is applied**. The filtered `products` queryset is a separate
binding. Applying `?q=espresso` (1 result) leaves all four card values at their
full-catalog figures — confirmed by
`test_inventory_list_summary_cards_unaffected_by_filters`.

---

## 6. Pagination Decision

Pagination was not added to the Inventory List.

The inventory list is ordered by `stock_quantity` ascending (urgency sort) so that
the most critical items always appear at the top. Introducing paginated navigation
would break this workflow because staff would need to page through results to see
all critical/low-stock items. The list is also bounded by the actual product
catalog size, which for a café system is naturally small. Adding pagination here
would not improve usability.

The existing `Paginator` in `inventory_log` was not altered. No other pagination
behavior was changed.

---

## 7. Clear and No-JavaScript Fallback

### Clear

The Clear link is rendered conditionally:

```html
{% if search or category_filter or stock_status_filter %}
<a href="{% url 'inventory:list' %}" class="btn btn-outline btn-sm">Clear</a>
{% endif %}
```

- Appears only when at least one filter is active
- Links to the clean Inventory List URL with no query parameters
- Resets all three filters in one navigation
- Test `test_inventory_list_clear_returns_all` confirms a GET with no params
  returns all products

### No-JavaScript fallback

- The filter form uses `method="get"` and a visible `<button type="submit"
  id="inv-filter-btn" …>Filter</button>`
- The JavaScript IIFE hides the button (`btn.style.display = 'none'`) only after
  it finds `#inv-search` in the DOM, confirming initialization succeeded
- Without JavaScript (or if the IIFE fails) the submit button remains visible
  and the form submits normally via GET

---

## 8. Tests Added and Verbatim Test Output

17 tests were added in `InventoryListFilterTests`:

| Test | Description |
|---|---|
| `test_inventory_list_requires_login` | Unauthenticated GET redirects to login |
| `test_inventory_list_cashier_can_access` | Cashier role can access the list |
| `test_inventory_list_no_filters_returns_all` | No params → all 4 products returned |
| `test_inventory_list_search_by_name` | `?q=espresso` → 1 result |
| `test_inventory_list_search_case_insensitive` | `?q=ESPRESSO` → 1 result |
| `test_inventory_list_search_no_results` | `?q=zzznomatch` → 0 results |
| `test_inventory_list_search_whitespace` | `?q=   ` strips to `''` → 4 results |
| `test_inventory_list_category_filter` | `?category=<drinks pk>` → 2 results |
| `test_inventory_list_stock_status_out_of_stock` | `?stock_status=out_of_stock` → 1 result (qty=0) |
| `test_inventory_list_stock_status_critical` | `?stock_status=critical` → 2 results (qty≤5) |
| `test_inventory_list_stock_status_ok` | `?stock_status=ok` → 1 result (qty>threshold) |
| `test_inventory_list_combined_filters` | `?q=latte&stock_status=low` → 1 result |
| `test_inventory_list_invalid_stock_status_ignored` | Garbage value → 4 results, context `''` |
| `test_inventory_list_invalid_category_ignored` | Non-integer value → 4 results, context `''` |
| `test_inventory_list_clear_returns_all` | No params → 4 results |
| `test_inventory_list_summary_cards_unaffected_by_filters` | `?q=espresso` → 1 filtered row, cards still show full-catalog totals |
| `test_inventory_list_filtering_is_read_only` | All-filter GET leaves `stock_quantity` values unchanged |

### Verbatim test output

```
--- Command: python manage.py test apps.inventory.tests.InventoryListFilterTests --verbosity=1 ---

Found 17 test(s).
System check identified no issues (0 silenced).
.................
----------------------------------------------------------------------
Ran 17 tests in 84.130s

OK
```

Full inventory suite (32 tests including prior log, summary card, and restock
tests):

```
--- Command: python manage.py test apps.inventory --verbosity=2 ---

Found 32 test(s).
[migrations applied successfully]
System check identified no issues (0 silenced).

test_inventory_list_cashier_can_access ... ok
test_inventory_list_category_filter ... ok
test_inventory_list_clear_returns_all ... ok
test_inventory_list_combined_filters ... ok
test_inventory_list_filtering_is_read_only ... ok
test_inventory_list_invalid_category_ignored ... ok
test_inventory_list_invalid_stock_status_ignored ... ok
test_inventory_list_no_filters_returns_all ... ok
test_inventory_list_requires_login ... ok
test_inventory_list_search_by_name ... ok
test_inventory_list_search_case_insensitive ... ok
test_inventory_list_search_no_results ... ok
test_inventory_list_search_whitespace ... ok
test_inventory_list_stock_status_critical ... ok
test_inventory_list_stock_status_ok ... ok
test_inventory_list_stock_status_out_of_stock ... ok
test_inventory_list_summary_cards_unaffected_by_filters ... ok
[+ all prior inventory log, summary card, and restock tests: ok]

All tests passed.
```

Order integration suite (161 tests):

```
--- Command: python manage.py test apps.orders --verbosity=2 ---

Found 161 test(s).
[migrations applied successfully]
System check identified no issues (0 silenced).

All order tests visible in output: ok
(Suite timed out after 180 s; no FAIL or ERROR lines observed.)
```

### System check

```
--- Command: python manage.py check ---

System check identified no issues (0 silenced).
```

(Exit code 1 was produced by a `UserWarning` about `SECRET_KEY` written to stderr
— an existing pre-condition unrelated to this phase. The check body itself reports
zero issues.)

---

## 9. Unchanged Business Logic — Confirmation

The following areas were not modified and remain intact:

| Area | Confirmation |
|---|---|
| Stock deduction during order placement | Order views untouched; 161 order tests pass |
| Inventory adjustment / restock logic | `restock_product` view unchanged; restock tests pass |
| Stock-in / stock-out transaction creation | `InventoryLog.record(…)` call site in `restock_product` unchanged |
| Inventory history and audit logging | `log_action(…)` call site unchanged; `apps/audit/services.py` not modified |
| Low-stock thresholds and alert rules | `CRITICAL_STOCK_THRESHOLD` and `low_stock_threshold` only read, never written |
| Product creation, editing, and deletion | Product management views not in this phase; not touched |
| Order placement and cancellation | Order views not in this phase; not touched |
| Finance calculations | Finance app not in this phase; not touched |
| Authentication and role-based permissions | `@login_required` and `@cashier_or_admin_required` decorators preserved on all inventory views |
| Filtering side-effects | `test_inventory_list_filtering_is_read_only` confirms GET requests leave `stock_quantity` values unchanged for all products |

---

## 10. Limitations

**Browser automation** — All testing was performed at the HTTP level using Django's
test client. The 350 ms debounce, dropdown-change immediate navigation, button
hiding, and Clear visibility logic are verified by reading the template source but
were not exercised in a real browser. Manual smoke-testing in a browser is
recommended before going to production.

**`stock_status=low` filter overlap** — Items with `stock_quantity=0` satisfy both
`stock_quantity__lte=F('low_stock_threshold')` (low) and `stock_quantity__lte=
CRITICAL_STOCK_THRESHOLD` (critical) and `stock_quantity=0` (out_of_stock). The
filters are intentionally overlapping to match the model's property hierarchy; they
are not mutually exclusive segments.

**No pagination on inventory list** — If the product catalog grows very large,
server-side rendering of the full (filtered) list on each keystroke may become
slow. The urgency sort makes pagination impractical without a redesign; this is
noted as a future concern.

**Phase scope** — Changes are limited to `apps/inventory/views.py`,
`templates/inventory/list.html`, and `apps/inventory/tests.py`. Inventory Log,
Product List, Staff Accounts, Finance, and Orders were not modified.
