# Phase 7.3 — Ingredient Movement Dashboard Decision Record

**Status:** Approved and implemented
**Scope:** Dashboard-only physical movement reporting for ingredients and packaging
**Related architecture:** `docs/Kape De Manubag — Ingredient-Based Inventory Revised Architecture (Rev3).md`

This addendum records the Phase 7.3 contract. It does not modify the Rev3 document.
It is an activity-reporting contract, not an inventory reconciliation specification.

## Approved decisions

| Decision | Approved requirement |
|---|---|
| D0 | Record Phase 7.3 in this separate architecture decision record. |
| D1 | The movement summary follows the active dashboard Week/Month period. |
| D2 | Display a compact per-unit table, showing only units with movements. |
| D3 | Include both ingredients and packaging. |
| D4 | Show signed net adjustments per unit. |
| D5 | Recent activity contains physical movements only. |
| D6 | Recent activity is limited to five entries. |
| D7 | Use `IngredientMovementLog.created_at` as the recording-period date. |
| D8 | Preserve existing refresh behavior; no new realtime events. |
| D9 | Place the two new cards in a two-column row after the Phase 7.2 card. |

## Source of truth

`IngredientMovementLog` is the sole source of movement data. The dashboard must not
sum purchase-line or reservation-line quantities separately, and must not derive
historical activity from current `InventoryItem` balances.

### Physical movement types

Only these types affect physical on-hand stock and may appear in Phase 7.3 totals
and the recent activity feed:

- `purchase`
- `consumption`
- `wastage`
- `adjustment`

### Excluded non-physical events

These are reservation-state events, not physical stock movements:

- `reservation`
- `reservation_release`

They are excluded from both widgets even though they are present in the movement
ledger. Their `on_hand_delta` is zero; the widgets must not display them as
physical quantities.

## Per-unit movement summary

The summary uses `on_hand_delta` only. `reserved_delta` is never used for physical
totals.

| Metric | Calculation |
|---|---|
| Consumed | Negative magnitude of summed consumption `on_hand_delta` |
| Purchased | Sum of purchase `on_hand_delta` |
| Wasted | Negative magnitude of summed wastage `on_hand_delta` |
| Net adjustment | Signed sum of adjustment `on_hand_delta` |

Adjustments remain separate from purchases and wastage. Different `base_unit`
values (`g`, `ml`, `pcs`) are never added together. The summary returns one row per
unit that has at least one physical movement in the selected period, in a stable
`g`, `ml`, `pcs` order.

Both ingredients and packaging are included. Historical movements for inactive
items remain visible because the ledger is historical.

### Summary JSON contract

`ingredient_movement_summary` is an additive dashboard summary key. It is either
`null` (widget failed), an empty list (no movements), or a list of objects:

```json
{
  "base_unit": "g",
  "consumed": "80",
  "purchased": "700",
  "wasted": "20",
  "net_adjustment": "6",
  "net_adjustment_display": "+6"
}
```

Quantity values are decimal strings. `net_adjustment` preserves its signed value;
`net_adjustment_display` adds an explicit `+` for positive values.

## Week/Month date semantics

The summary uses the same active period as the Sales Overview chart and Top
Products widget. `_period_start()` remains the shared source of truth.

- Week: Sunday-start calendar week, through the following Sunday (exclusive).
- Month: first day of the current calendar month, through the first day of the
  following month (exclusive).

Filtering uses local, timezone-aware boundaries and an inclusive start with an
exclusive end:

- `created_at__gte=start`
- `created_at__lt=end`

`created_at__date` is not used for this aggregate.

## Purchase recording-period semantics

Phase 7.3 uses `IngredientMovementLog.created_at`, not
`StockPurchase.purchase_date`. A purchase entered today for an earlier delivery
counts in today's recording period. The card is therefore described as movements
recorded in the active week/month, not as purchases by supplier receipt date.

## Recent activity feed

The recent activity feed is independent of the selected Week/Month period. It
shows the five most recent physical movements overall, ordered deterministically
by:

- `-created_at`
- `-pk`

Only `purchase`, `consumption`, `wastage`, and `adjustment` are included.

### Feed fields

- `pk`
- `item_name` snapshot
- `movement_type`
- human-readable movement type
- signed `on_hand_delta` string
- `quantity_display` with explicit positive sign
- `base_unit` snapshot
- `order_number` snapshot
- `reason_code`
- humanized `reason_display` when a reason exists
- `note`
- `created_at` ISO timestamp
- formatted `created_at_display`

No order join or item join is needed for the feed. Snapshot fields keep the query
at one SELECT and avoid N+1 queries.

### Display rules

- Purchase: positive, e.g. `+500 g`.
- Consumption: negative, e.g. `-50 g`.
- Wastage: negative, e.g. `-20 g`.
- Adjustment: preserve its actual signed direction.
- Empty reason codes remain empty; no reason is invented.
- Fixed internal badge classes map movement types to styling; payload text never
  becomes a CSS class.

## Opening-stock limitation

Item creation can set `on_hand_quantity` directly without writing an
`IngredientMovementLog` opening-balance row. Consequently:

- The ledger is not a complete all-time stock reconciliation from zero.
- Purchase minus consumption minus wastage minus adjustments does not necessarily
  equal current on-hand stock.
- Phase 7.3 reports period activity. It must not be labelled or treated as a stock
  balance or reconciliation.

## Error isolation

Both widgets are registered independently through the existing `_widget()` and
`_load_widgets()` mechanism.

- Successful empty widget: empty list.
- Failed widget on the page: friendly fallback inside only that card.
- Failed widget in summary JSON: `null` for that key only.
- A failure in either Phase 7.3 widget must not break the other widget or any
  existing dashboard dataset.
- Client rendering uses `safeWidgetUpdate()` and `escHtml()`.

## Query budgets

Each widget uses exactly one bounded database query.

- Dashboard page: **14** queries.
- Dashboard summary endpoint: **13** queries.

These replace the prior 12/11 budgets. No per-row queries or N+1 queries are
permitted.

## Migrations and indexes

Phase 7.3 adds no model fields, migration, or index. The existing
`(movement_type, -created_at)`, `created_at`, and ordering indexes support the
bounded queries. No migration or index change is authorized by this decision.

## Scope boundaries

Included:

- Per-unit physical ingredient and packaging movement summary.
- Five-row recent physical movement activity feed.
- Dashboard page and summary JSON integration, client rendering, and tests.
- This decision record.

Excluded:

- Inventory reconciliation or balance calculation.
- New movement types or reason codes.
- Reclassification of `cancelled_prepared` consumption as wastage.
- Changes to inventory services, order services, models, migrations, reports,
  finance, realtime events, authentication, or authorization.
- Polling, timers, or new realtime events.

## Final statement

Phase 7.3 provides **activity reporting only**. It summarizes recorded physical
movements for staff awareness and does not assert that the movement ledger
reconciles to current inventory balances.
