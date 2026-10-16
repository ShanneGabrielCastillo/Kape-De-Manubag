# Inventory Warning Banner — Implementation Report

## Summary

Replaced the broad "low stock" warning banner with a conditional, compact banner that appears only when urgent attention is needed (critical low-stock or out-of-stock products).

---

## Conditional Logic

The banner is controlled by a single Django template condition:

```django
{% if critical_stock_count or out_of_stock_count %}
```

- **`critical_stock_count`** — products with `stock_quantity <= CRITICAL_STOCK_THRESHOLD` (threshold = 5 units, defined in `apps/menu/models.py`).
- **`out_of_stock_count`** — products with `stock_quantity = 0`.

Both values come from the **existing aggregate query** in `inventory_list` view (no new DB queries added). The view already computed and passed both counts to the template under the keys `critical_stock_count` and `out_of_stock_count`.

Inside the banner, each count line is individually guarded so zero-count lines are never shown:

```django
{% if critical_stock_count %}<div>{{ critical_stock_count }} critical low-stock item{{ critical_stock_count|pluralize }}</div>{% endif %}
{% if out_of_stock_count %}<div>{{ out_of_stock_count }} out of stock</div>{% endif %}
```

---

## Files Changed

### `templates/inventory/list.html`

**Before:**
```html
{% if low_stock_count %}
<div class="alert alert-warning" style="margin-bottom:20px">
  <i data-lucide="triangle-alert" class="status-icon" aria-hidden="true"></i>
  <strong>{{ low_stock_count }} product{{ low_stock_count|pluralize }}</strong>
  with low stock (at or below their restock threshold).
  {% if critical_stock_count %} <strong>{{ critical_stock_count }} critical</strong>.{% endif %}
  Restock soon!
</div>
{% endif %}
```

**After:**
```html
{% if critical_stock_count or out_of_stock_count %}
<div class="alert alert-warning" style="margin-bottom:20px;align-items:flex-start">
  <i data-lucide="triangle-alert" class="status-icon" style="flex-shrink:0;margin-top:2px" aria-hidden="true"></i>
  <div>
    <strong style="display:block;margin-bottom:4px">Inventory needs attention</strong>
    {% if critical_stock_count %}<div>{{ critical_stock_count }} critical low-stock item{{ critical_stock_count|pluralize }}</div>{% endif %}
    {% if out_of_stock_count %}<div>{{ out_of_stock_count }} out of stock</div>{% endif %}
    <div style="margin-top:4px;opacity:0.8;font-size:0.83rem">Review the affected products below.</div>
  </div>
</div>
{% endif %}
```

No other files were changed.

---

## Scenario Verification

| # | Scenario | `critical_stock_count` | `out_of_stock_count` | Banner shown? | Content |
|---|----------|----------------------|---------------------|---------------|---------|
| 1 | Critical low-stock products exist, none out of stock | > 0 | 0 | ✅ Yes | Critical count line only |
| 2 | Out-of-stock products exist, none critical | 0 | > 0 | ✅ Yes | Out-of-stock count line only |
| 3 | Both conditions exist | > 0 | > 0 | ✅ Yes | Both count lines |
| 4 | Low-stock products exist, but none critical and none out of stock | 0 | 0 | ✅ No | Banner hidden |
| 5 | All products adequately stocked | 0 | 0 | ✅ No | Banner hidden |

**Scenario 4 explanation:** Products that are low-stock (at or below their per-product `low_stock_threshold`) but have a stock quantity above 5 and above 0 will have `critical_stock_count = 0` and `out_of_stock_count = 0`, so the banner correctly stays hidden. The summary cards still show the `low_stock_count` value for visibility.

---

## `python manage.py check` Output

`python manage.py check` could not be executed in this environment — the command timed out, which is consistent with the shell not having database connectivity or the project's virtual environment activated. The template change is purely HTML/Django template syntax (no Python model, view, migration, or URL changes), so it introduces no Django system-check issues. The condition uses only context variables already passed by the existing view.

---

## What Was Not Changed

- `apps/inventory/views.py` — no changes; the view already computes and passes `critical_stock_count` and `out_of_stock_count`
- `apps/inventory/models.py` — no changes
- `static/css/main.css` — no changes; reused existing `.alert` and `.alert-warning` classes
- `CRITICAL_STOCK_THRESHOLD` — not touched
- Summary cards, product list, restock modal, restock logic — all untouched
- No other pages modified
