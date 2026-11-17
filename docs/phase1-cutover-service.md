# Phase 1 — Readiness-Gated Inventory Cutover

`apps.menu.services` owns the only supported path that changes
`Product.inventory_tracking_mode`.

## Dry run (read-only)

```python
from apps.menu.services import cutover_readiness_report

report = cutover_readiness_report([27, 1])
# Or omit the ids to assess every product:
# report = cutover_readiness_report()
```

`cutover_readiness_report()` returns JSON-friendly data:

```json
{
  "eligible_count": 1,
  "blocked_count": 1,
  "products": [
    {
      "product_id": 27,
      "product_name": "Tuna Pastil",
      "current_mode": "legacy",
      "eligible": false,
      "checks": [
        {"key": "approved_recipes", "label": "...", "passed": false, "detail": "..."}
      ],
      "reasons": ["..."],
      "size_availability": [
        {
          "size": "none",
          "order_type": "dine_in",
          "available_units": 0,
          "configuration_complete": false,
          "reason": "plan could not be resolved"
        }
      ],
      "missing_or_inactive_items": [],
      "open_orders": [],
      "outstanding_reservations": []
    }
  ]
}
```

For typed objects, use `assess_cutover_readiness(product)` or
`assess_products_for_cutover(ids)`. None of these functions write, lock, audit,
reserve, or change stock.

## Readiness gates

A product is eligible only when all of these pass:

1. The product exists and is active.
2. It is not already ingredient-tracked.
3. Every configured size has exactly one applicable approved recipe per order
   type (`all`, or the matching `dine_in` / `takeout` scope).
4. Every approved recipe has at least one line, and every line references an
   active `ingredient` item with a positive quantity.
5. Both dine-in and takeout rules exist for every configured size.
6. Every referenced profile is approved with active, correctly typed packaging
   items.
7. `resolve_plan()` succeeds for every configured size and order type.
8. Current available stock can produce at least one unit in every cell.
9. No `awaiting_payment`, `preparing`, or `ready` order references the product.
10. No `reserved` reservation line references the product.

## Cutover

```python
from apps.menu.services import change_inventory_tracking_mode

result = change_inventory_tracking_mode(
    [27], "ingredient", actor=request.user, reason="owner-approved cutover",
)
```

The service:

* locks Product rows first (ascending pk), then all plan-referenced
  `InventoryItem` rows through `lock_inventory_items()` (ascending pk);
* re-runs the complete assessment under those locks;
* writes only `inventory_tracking_mode` and `updated_at`, preserving the
  existing `Product` post-save realtime signal;
* writes one `strict=True` `AuditLog` row per product inside the same
  transaction, so an audit failure rolls the whole cutover back;
* changes only explicitly requested ids and never discovers products by itself.

`target_mode="legacy"` remains available for rollback. Its gate rejects products
with open ingredient order lines or outstanding reservations.

There is no hardcoded product allowlist; safety comes from the readiness gates.
