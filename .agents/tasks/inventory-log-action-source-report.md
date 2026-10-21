# Phase 6C Completion Report — Inventory Log Action and Source Filters

**Date:** 2025-08-01  
**Phase:** 6C (final deferred filtering phase)  
**Scope:** Inventory Log view, template, and tests only

---

## 1. Pre-flight Findings

### Model field confirmation (`apps/inventory/models.py — InventoryLog`)

**`action` field** — `CharField(max_length=20, db_index=True)`

| DB value | Display label |
|---|---|
| `restock` | Restock |
| `adjustment` | Adjustment |
| `sale` | Sale Deduction |
| `waste` | Waste/Spoilage |

Exposed as `InventoryLog.ACTION_CHOICES`.

**`source` field** — `CharField(max_length=20, default='manual_adjustment')`

| DB value | Display label |
|---|---|
| `customer_order` | Customer Order |
| `pos_order` | POS Order |
| `manual_adjustment` | Manual Adjustment |
| `inventory_update` | Inventory Update |

Exposed as `InventoryLog.SOURCE_CHOICES`.

**Existing text-search fields confirmed:** `product__name`, `reason`, `notes` (all `icontains`, OR-combined via `Q`).

**Phase 1 IIFE confirmed:** used `btn.remove()` to hide the Submit button, built URL strings with `encodeURIComponent`, no `URLSearchParams`, no action/source handling.

**Pagination confirmed:** raw string concatenation — `?page=N{% if q %}&q={{ q }}{% endif %}` — no action/source parameters carried, `q` not urlencode-filtered.

---

## 2. Files Changed and Exact Changes

Three files changed. No other files were touched.

### `apps/inventory/views.py`

Added `VALID_ACTIONS`/`VALID_SOURCES` allowlist derivation from the model's choice tuples, GET param reading for `action` and `source`, allowlist validation (invalid values collapse to `''`), conditional queryset filtering, and four new context variables (`action_filter`, `source_filter`, `action_choices`, `source_choices`). No other logic in the view was modified.

Final state of the `inventory_log` function body (after existing `q` block):

```python
VALID_ACTIONS = [v for v, _ in InventoryLog.ACTION_CHOICES]
action_filter = request.GET.get('action', '').strip()
if action_filter not in VALID_ACTIONS:
    action_filter = ''
if action_filter:
    logs = logs.filter(action=action_filter)

VALID_SOURCES = [v for v, _ in InventoryLog.SOURCE_CHOICES]
source_filter = request.GET.get('source', '').strip()
if source_filter not in VALID_SOURCES:
    source_filter = ''
if source_filter:
    logs = logs.filter(source=source_filter)
```

Context passed to render:

```python
return render(request, 'inventory/log.html', {
    'logs': logs_page,
    'q': q,
    'action_filter': action_filter,
    'source_filter': source_filter,
    'action_choices': InventoryLog.ACTION_CHOICES,
    'source_choices': InventoryLog.SOURCE_CHOICES,
})
```

### `templates/inventory/log.html`

Four distinct areas updated:

**Filter bar** — Added two `<select>` dropdowns (`id="log-action-filter"`, `id="log-source-filter"`) to the existing `<form id="log-filter-form">`. Each dropdown iterates `action_choices`/`source_choices` from context and marks the selected option using `{% if action_filter == val %}` / `{% if source_filter == val %}`. The Clear anchor's condition was extended from `{% if q %}` to `{% if q or action_filter or source_filter %}` and its target remains `{% url 'inventory:log' %}` (no params).

**Pagination block** — Replaced raw `?page=N{% if q %}&q={{ q }}{% endif %}` construction with the same pattern extended by `{% if action_filter %}&action={{ action_filter }}{% endif %}` and `{% if source_filter %}&source={{ source_filter }}{% endif %}`. Applied Django's `|urlencode` filter to `q`. Added `data-page="N"` attribute to every anchor so the JavaScript can rewrite hrefs using `URLSearchParams`. Applied symmetrically to Prev, numbered pages, and Next anchors.

**Empty state** — Changed the `{% empty %}` row's condition from `{% if q %}` to `{% if q or action_filter or source_filter %}`, message updated to "No inventory logs match the current filters." / "No inventory logs yet."

**JavaScript IIFE** — Replaced the Phase 1 IIFE entirely (see section 8 below).

### `apps/inventory/tests.py`

Appended `InventoryLogActionSourceTests` class with 18 test methods. No existing classes or methods were modified.

---

## 3. Action Field Values and Display Labels

| Dropdown option | `name="action"` value sent | Display label |
|---|---|---|
| All Actions (default) | *(omitted from URL)* | — |
| Restock | `restock` | Restock |
| Adjustment | `adjustment` | Adjustment |
| Sale Deduction | `sale` | Sale Deduction |
| Waste/Spoilage | `waste` | Waste/Spoilage |

Options derived at runtime from `InventoryLog.ACTION_CHOICES`. No hardcoded option strings in the template.

---

## 4. Source Field Values and Display Labels

| Dropdown option | `name="source"` value sent | Display label |
|---|---|---|
| All Sources (default) | *(omitted from URL)* | — |
| Customer Order | `customer_order` | Customer Order |
| POS Order | `pos_order` | POS Order |
| Manual Adjustment | `manual_adjustment` | Manual Adjustment |
| Inventory Update | `inventory_update` | Inventory Update |

Options derived at runtime from `InventoryLog.SOURCE_CHOICES`.

---

## 5. Combined-Filter Behavior

All three filters are AND-combined server-side in strict sequence:

1. `q` — OR across `product__name__icontains`, `reason__icontains`, `notes__icontains`.
2. `action_filter` — exact match on `action` field.
3. `source_filter` — exact match on `source` field.

Each filter is applied only when non-empty. The queryset is a lazy chain; Django produces a single SQL query with all active WHERE conditions.

**Validation** — each received value is tested against its allowlist (`VALID_ACTIONS` / `VALID_SOURCES`). An unrecognised value collapses to `''` and is excluded from the queryset filter. This means invalid or injected values return the full unfiltered result set rather than an error, matching the pattern used by `InventoryListFilterTests.test_inventory_list_invalid_stock_status_ignored`.

**Empty values** — an empty string passes through validation and is also excluded from filtering, so `?action=` is treated identically to no `action` param.

---

## 6. Clear Behavior

The Clear anchor appears when `{% if q or action_filter or source_filter %}` is truthy — i.e., when at least one of the three filter params is active. It links to `{% url 'inventory:log' %}` with no query string, resetting all filters and returning to page 1. When no filter is active the anchor is not rendered.

---

## 7. Pagination URL Update

**Approach:** `data-page` attribute rewrite via JavaScript `URLSearchParams`.

Every pagination anchor carries a `data-page="N"` attribute. At page load the JavaScript IIFE queries `document.querySelectorAll('a[data-page]')` and rewrites each `href` using `buildParams({ page: N })`, which calls `URLSearchParams.set()` for all active parameters. This ensures all three filter values are correctly percent-encoded for every pagination link.

The Django template also builds valid inline `href` values as a no-JS fallback:

```
?page=N[&q=<urlencode>][&action=<action_filter>][&source=<source_filter>]
```

`q` is filtered through Django's `|urlencode`. `action_filter` and `source_filter` currently contain only ASCII identifier characters from the allowlist, so they are safe in the template as-is; however, the review noted this as a latent encoding gap (see Known Issues).

The JavaScript `buildParams()` function omits any parameter whose value is empty string or falsy, so the URL stays clean when filters are reset.

---

## 8. Phase 1 IIFE Replacement Rationale

The Phase 1 IIFE was replaced rather than patched for three reasons:

1. **`btn.remove()` → `btn.style.display = 'none'`** — `remove()` deletes the DOM node permanently, making the Submit button unavailable to automated test frameworks that inspect the DOM after JS runs. `style.display = 'none'` hides it visually while keeping the element present.

2. **`encodeURIComponent()` string concatenation → `URLSearchParams`** — The old code built URLs by string interpolation (`window.location.pathname + '?q=' + encodeURIComponent(trimmed)`). `URLSearchParams` handles encoding for all parameter types uniformly, eliminates manual concatenation, and naturally omits parameters that are not set.

3. **Dropdown `change` listeners** — The Phase 1 IIFE had no awareness of the action/source dropdowns. Adding two listeners on existing global variables required expanding the closure scope; given the structural change and the `btn.remove` fix, a clean replacement was safer than incremental patching.

The debounce timing (350 ms on text input) and overall auto-filter-on-input behavior are unchanged.

---

## 9. Tests Added

**Class:** `InventoryLogActionSourceTests` in `apps/inventory/tests.py`

**18 test methods:**

| # | Method name | What it covers |
|---|---|---|
| 1 | `test_action_filter_restock` | `?action=restock` returns exactly the restock record |
| 2 | `test_action_filter_adjustment` | `?action=adjustment` returns exactly the adjustment record |
| 3 | `test_action_filter_sale` | `?action=sale` returns exactly the sale record |
| 4 | `test_action_filter_waste` | `?action=waste` returns exactly the waste record |
| 5 | `test_source_filter_manual_adjustment` | `?source=manual_adjustment` returns exactly one record |
| 6 | `test_source_filter_customer_order` | `?source=customer_order` returns exactly one record |
| 7 | `test_source_filter_pos_order` | `?source=pos_order` returns exactly one record |
| 8 | `test_source_filter_inventory_update` | `?source=inventory_update` returns exactly one record |
| 9 | `test_action_and_source_combined` | `action=restock&source=manual_adjustment` → exactly one record matching both |
| 10 | `test_text_search_with_action_filter` | `?q=coffee&action=restock` → one record (coffee + restock) |
| 11 | `test_all_three_filters_combined` | `?q=Expired&action=waste&source=pos_order` → one record |
| 12 | `test_invalid_action_ignored` | Unrecognised action value → all 4 records, `action_filter == ''` |
| 13 | `test_invalid_source_ignored` | Unrecognised source value → all 4 records, `source_filter == ''` |
| 14 | `test_empty_action_returns_all` | `?action=` → all 4 records |
| 15 | `test_clear_resets_action_and_source` | GET with no params → all 4 records, all three context vars empty |
| 16 | `test_pagination_preserves_action_filter` | 13 restock records force 2 pages; `action=restock` appears in page-1 response content |
| 17 | `test_filter_resets_to_page_1` | Filter without `page` param → `logs.number == 1` |
| 18 | `test_action_filter_does_not_create_records` | Filter request leaves `InventoryLog.count()` and all `stock_quantity` values unchanged |

### Verbatim test output summary

```
Ran 50 tests in 167.922s

OK
```

All 50 tests in `apps.inventory` passed — 17 from `InventoryListFilterTests`, 10 from `InventoryLogSearchTests` (Phase 1), 18 from `InventoryLogActionSourceTests` (Phase 6C), 3 from `InventorySummaryCardTests`, and 2 from `RestockInputRobustnessTests`.

All 10 Phase 1 `InventoryLogSearchTests` passed:

```
test_inventory_log_filter_by_query ... ok
test_inventory_log_filter_empty_query ... ok
test_inventory_log_filter_no_results ... ok
test_inventory_log_pagination_preserves_query ... ok
test_inventory_log_unauthorized ... ok
test_log_page_shows_source_and_reason ... ok
test_search_by_notes ... ok
test_search_by_product_name ... ok
test_search_by_reason ... ok
test_search_with_no_matches_shows_clear_message ... ok
```

System check: `System check identified no issues (0 silenced).`

---

## 10. Stock, Transaction, Audit, and Permissions — Unchanged

The following were confirmed unchanged by inspection and test:

- **Stock quantities** — `restock_product` view and `InventoryLog.record()` class method were not modified. `test_action_filter_does_not_create_records` asserts that a filter GET leaves `InventoryLog.objects.count()` and every `Product.stock_quantity` identical to their pre-request values.
- **Transaction creation** — No new `InventoryLog` records are created by the filter view. The view is decorated `@login_required` and `@cashier_or_admin_required`; these decorators were not changed.
- **Audit logging** — No audit events are generated by filter-only GET requests. The audit integration points (`AuditLog` creation inside `restock_product` and order cancellation) were not touched.
- **Order workflows** — `apps/orders` was not modified. The inventory deduction path triggered on order placement is untouched.
- **Permissions** — `@login_required` + `@cashier_or_admin_required` on `inventory_log` unchanged. `test_inventory_log_unauthorized` (Phase 1) continued to pass.
- **Finance logic** — No finance files were touched.

---

## 11. Known Issues from Review

The code-review document (`inventory-log-action-source-review.md`) identified three items. None of them alter the functional behavior of the feature as shipped; they are recorded here for the next maintenance pass.

1. **Bare `action`/`source` values in pagination template fallback** — The template appends `&action={{ action_filter }}` and `&source={{ source_filter }}` without `|urlencode`. All current allowlist values are ASCII identifiers, so no malformed URL is possible today. The JavaScript path is already correct because `URLSearchParams.set()` handles encoding. Applying `|urlencode` to both parameters in the template would make the no-JS fallback consistent.

2. **No database index on `source`** — `InventoryLog.Meta.indexes` includes `idx_invlog_action_created` on `(action, -created_at)` but no corresponding index on `source`. A `?source=…` request with no action or text filter will perform a full table scan. A `(source, -created_at)` composite index would close this gap; adding it requires a new migration.

3. **Test coverage asymmetry** — `test_pagination_preserves_action_filter` verifies that `action=` appears in rendered pagination links. There is no parallel `test_pagination_preserves_source_filter`. The template blocks for both parameters are structurally identical, so the practical regression risk is low.

These issues are in scope for the next review cycle and do not block the current phase.

---

## 12. Limitations

- **Browser automation unavailable.** Mobile layout, visual rendering of the filter bar, and actual 350 ms debounce timing were not verified by a browser. HTTP-level tests confirm server-side behavior; client-side JavaScript behavior (dropdown immediate navigation, debounce cancellation, `URLSearchParams` encoding, pagination href rewrite) is verified by code inspection only.
- The review's encoding correctness for special characters in rendered pagination links was tested at the code level (Django `|urlencode` on `q`; `URLSearchParams` in JavaScript). No live browser round-trip confirmed the full encoding chain.

---

## Summary

Phase 6C is complete. The Inventory Log filter bar now supports Action and Source dropdowns alongside the existing text search. All three filters are AND-combined server-side, preserve pagination, and reset to page 1 when changed. The Clear control appears when any filter is active and removes all params. The no-JS fallback (GET form + Submit button hidden by JS) remains functional. All 50 `apps.inventory` tests pass including the 10 original Phase 1 tests. System check reports zero issues.
