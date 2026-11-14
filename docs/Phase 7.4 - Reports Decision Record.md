# Phase 7.4 — Reports Decision Record

**Status:** Approved for implementation (planning complete — implementation not started)
**Date:** 2026-11-13
**Baseline:** commit `956f22f` (`feat(dashboard): add ingredient movement summaries`), branch `fix/security-tests-and-tracking-api`
**Related architecture:** `docs/Kape De Manubag — Ingredient-Based Inventory Revised Architecture (Rev3).md` (§D.8, §D.10, §D.13, §F.4–F.5, §H.4, §H.8)
**Related record:** `docs/Phase 7.3 - Ingredient Movement Dashboard Decision Record.md` (companion activity-reporting contract)
**Companion document:** `docs/Phase 7.4 - Reports Implementation Plan.md`

This record does not modify the Rev3 architecture document. It is a reporting contract
for two read-only admin reports plus a batch of approved Sales Reports hardening items.
It is an activity-reporting contract, not an inventory reconciliation specification.

---

## 1. Purpose and scope

Phase 7.4 delivers, inside the existing `apps/reports` app:

1. **Report A — Ingredient Usage Report**: physically consumed ingredient and
   packaging quantities per period, separating normal order preparation
   consumption from consumption caused by cancelled-but-prepared orders.
2. **Report B — Cancellation Disposition Report**: product-unit quantities
   recorded in `OrderCancellationDisposition` at cancellation time, labeled by
   ledger (legacy vs ingredient) and disposition source.
3. **Sales Reports hardening** (existing page only, no behavior redesign):
   Excel formula-injection fix, maximum date-range, export row cap, label and
   footnote corrections, and obsolete test-fixture replacement.

Both reports are read-only, admin-only, additive, and independent of the existing
sales aggregation logic. No model, migration, service, or lifecycle behavior changes.

## 2. Approved owner decisions (summary)

| # | Decision |
|---|---|
| A1 | Two separate read-only report pages, admin-only, reusing `admin_required`. |
| A2 | Report A source of truth is `IngredientMovementLog`; Report B source of truth is `OrderCancellationDisposition`. No other tables are aggregated. |
| A3 | Report A includes only `movement_type='consumption'` rows. |
| A4 | Report A separates consumption with `reason_code='order_ready'` from `reason_code='cancelled_prepared'`. |
| A5 | Date basis Report A: `IngredientMovementLog.created_at`. |
| A6 | Date basis Report B: `OrderCancellationDisposition.decided_at`. |
| A7 | Ingredients and packaging are reported separately; never a combined total. |
| A8 | Quantities are grouped by item snapshot (`item_name`), `base_unit`, and `reason_code`; incompatible base units (g, ml, pcs) are never summed together. |
| A9 | `reserved_delta` is never used; reservation and reservation-release movements are never counted as consumption. |
| A10 | No stock reconciliation, no on-hand derivation, no "current balance" claims. |
| A11 | `cancelled_prepared` consumption is not wastage. It is labeled as its own category. |
| A12 | Disposition `discarded_units` is a disposition figure only; it is never added to Report A consumption or to manual wastage figures. |
| A13 | Report B includes all recorded dispositions, including rows with zero prepared and zero discarded units. |
| A14 | Report B shows `prepared_units`, `unprepared_units`, `discarded_units` as three separate figures. |
| A15 | Report B includes both ledgers (`legacy` and `ingredient`) and labels the ledger per row/group. |
| A16 | Report B quantities stay in product units; never merged with base-unit quantities. |
| A17 | Navigation via tabs on the existing Sales Reports page; existing page behavior and its two summary cards are preserved. Average Order stays removed. |
| A18 | Excel exports for the two new reports are **deferred** (not built in this phase). |
| A19 | Existing export hardening items H1–H7 (below) are included in this phase. |
| A20 | Sales Reports date basis stays `created_at` (consistent with Finance and Dashboard). |

## 3. Authoritative data sources (verified against current models)

### Report A — `apps.inventory.models.IngredientMovementLog`

Verified current definition (Rev3 §D.8; `apps/inventory/models.py:316-656`):

- `inventory_item` FK → `InventoryItem`, **PROTECT**, related name `movements`.
- Snapshots written at record time: `item_name` (CharField 120), `base_unit` (g/ml/pcs).
- `movement_type` ∈ {`purchase`, `reservation`, `reservation_release`,
  `consumption`, `wastage`, `adjustment`} — DB CHECK `mv_type_valid`; each type has
  a shape CHECK (`mv_shape_*`), notably **consumption ⇒ `on_hand_delta < 0` and
  `reserved_delta = on_hand_delta`**.
- `on_hand_delta`, `reserved_delta`, `on_hand_before/after`, `reserved_before/after`
  — DecimalField(14,3); arithmetic CHECKs guarantee internal consistency.
- `order` FK (SET_NULL, nullable) + `order_number` snapshot (CharField 30).
- `reservation_item` FK → `OrderIngredientReservationItem` (SET_NULL; set for
  consumption and release movements produced by `settle()`, null for aggregated
  placement-time reservation movements).
- `reason_code` CharField(50) — for consumption rows the service writes exactly
  `order_ready` or `cancelled_prepared` (verified write sites:
  `apps/orders/services.py:1671,1764` for `order_ready`;
  `apps/orders/services.py:1552-1559` for `cancelled_prepared`).
- `created_at` — `auto_now_add`, `db_index=True`.
- Indexes: `(inventory_item, -created_at)`, `(order)`, `(movement_type, -created_at)`.

Physical-shape facts the reports rely on (DB-enforced, so no code defensive checks needed):
- consumption rows always have `on_hand_delta < 0` and `reserved_delta = on_hand_delta`;
  physical quantity consumed per row = `|on_hand_delta|`.
- `reservation` / `reservation_release` rows always have `on_hand_delta = 0` — they
  move reserved stock, never physical stock.

### Report B — `apps.orders.models.OrderCancellationDisposition`

Verified current definition (Rev3 §D.13 + Q6d extension; `apps/orders/models.py:507-680`):

- `order` FK → `Order`, **PROTECT**, related name `cancellation_dispositions`.
- `order_item` FK → `OrderItem`, **PROTECT**, related name `cancellation_dispositions`;
  service-layer rule `order_item.order_id == order.pk`.
- `ledger` ∈ {`legacy`, `ingredient`} — snapshot copied from
  `OrderItem.inventory_mode` at cancellation time (DB CHECK `ocd_ledger_valid`).
- `order_item_quantity` — snapshot of `OrderItem.quantity` at cancellation.
- `prepared_units`, `unprepared_units`, `discarded_units` — non-negative ints;
  CHECKs: `prepared + unprepared = order_item_quantity`,
  `discarded_units <= prepared_units`.
- `source` ∈ {`staff_declared`, `system_ready`, `system_unpaid`}
  (cancel-from-`preparing` / cancel-from-`ready` / cancel-from-`awaiting_payment`).
- `decided_by` FK (SET_NULL), `decided_at` (`auto_now_add`), `note`.
- One row per `(order, order_item)` (`ocd_unique_item`). Append-only, never edited.
- **No `Meta.indexes`** — the only index is the unique constraint on
  `(order, order_item)`. See §9 (limitations) for the decided_at scan implication.
- `OrderItem.product_name` is the product-label source for Report B via the
  PROTECT `order_item` join (OrderItem rows cannot be deleted: orders cannot be
  deleted — hard-delete is blocked in `apps/orders/models.py:1059-1082`).

### Supporting facts verified (do not re-derive during implementation)

- `OrderItem.inventory_mode` ∈ {`legacy`, `ingredient`} — snapshot of
  `Product.inventory_tracking_mode` at placement (`apps/orders/models.py:319-335`);
  historical rows default `legacy`.
- `OrderItem` also carries `category_name` and `packaging_eligible` snapshots.
- `Order.status` vocabulary is `awaiting_payment / preparing / ready / completed /
  cancelled` — there is **no `pending` status**; existing test fixtures using
  `'pending'` are obsolete (see H7).
- Product-level ledger routing: `Product.inventory_tracking_mode`
  (`apps/menu/models.py:288`).
- `InventoryItem.item_type` ∈ {`ingredient`, `packaging`} with CHECK
  `item_type_valid`; `InventoryItem` deletion is blocked by the PROTECT FK from
  `IngredientMovementLog` and by deactivation rules, so the join is stable.
- Existing reports authorization: `@login_required @admin_required`
  (`apps/accounts/decorators.py:7-18`; admin check via `user.is_admin_user`).

## 4. Date-field decisions

| Report | Date basis | Filter form | Rationale |
|---|---|---|---|
| Report A | `IngredientMovementLog.created_at` (indexed; also rightmost column of `(movement_type, -created_at)`) | `created_at >= start_midnight` AND `created_at < end_midnight_plus_1` (aware datetime bounds, same pattern as the Phase 7.3 dashboard widgets) | Ledger recording time is when consumption physically happened. Range predicates on raw datetimes use the existing index directly. |
| Report B | `OrderCancellationDisposition.decided_at` | Same aware-datetime bounds on `decided_at` | `decided_at` is when the cancellation disposition was recorded — the business event being reported. Order `created_at` is deliberately NOT the basis. |
| Sales Reports (existing) | `Order.created_at` — unchanged (A20) | existing `created_at__date` predicates stay as-is (owner decision: no optimization without measured evidence) | Consistency with Finance and Dashboard. |

Default window for both new reports: same convention as Sales Reports — last 30
days ending today, with the same "invalid input falls back to default, inverted
range is swapped" parsing behavior (reusing the `_parse_date_range` pattern; the
new views get their own call sites so existing behavior cannot regress).

## 5. Usage classification rules (Report A)

### 5.1 What counts as physical consumption

Included: rows with `movement_type='consumption'` only. Physical quantity per row
is `|on_hand_delta|` (DB shape CHECK guarantees `on_hand_delta < 0`).

Excluded (each is an explicit rule, tested individually):

| Excluded | Why |
|---|---|
| `movement_type='reservation'` | Reservation-state event; `on_hand_delta = 0` by CHECK. Rule A9. |
| `movement_type='reservation_release'` | Releases reserved stock back to available; `on_hand_delta = 0` by CHECK. Rule A9. |
| `reserved_delta` in any aggregation | Never a physical quantity. Rule A9. |
| `movement_type='wastage'` | Separate physical reduction with its own reason; not order consumption. Report A is consumption-only (A3); wastage figures remain the domain of the inventory app. |
| `movement_type='purchase'` / `'adjustment'` | Not consumption. |
| Manual wastage `discarded_units` linkage | `OrderCancellationDisposition.discarded_units` (Report B) is never added into Report A figures (A12). |

### 5.2 Normal vs cancelled-prepared separation

Consumption rows carry `reason_code` written by the settlement service:

- `order_ready` — reservation settled when the order was marked ready (normal
  preparation).
- `cancelled_prepared` — reservation settled during cancellation of prepared units
  (the customer never received these).

These are two separate report dimensions. `cancelled_prepared` is displayed under
its own label ("Consumed — cancelled after preparation") and is **never** labeled
wastage, never merged into `order_ready`, and never subtracted or netted against
it (A4, A11). Any consumption row with an unexpected `reason_code` still appears,
grouped under its raw code — the report never silently drops or reclassifies rows.

### 5.3 Grouping

One grouped aggregation, grouped by:

- `item_name` (snapshot at movement time — survives item renames),
- `base_unit` (snapshot at movement time),
- `reason_code` (`order_ready` / `cancelled_prepared` / other),
- `inventory_item__item_type` (join to live `InventoryItem` — see §6).

Quantities are summed as `Sum('on_hand_delta')` and displayed as positive
magnitudes, following the Phase 7.3 dashboard convention (`-total` for consumption).

### 5.4 Explicit non-goals

- No derivation of current on-hand balances from movement totals (A10).
- No netting consumption against purchases or adjustments.
- No per-order drill-down in this phase (the `order_number` snapshot exists and is
  shown contextually if needed, but no drill-down view is built).
- No Excel export (A18).

## 6. Ingredient vs packaging separation rules

### 6.1 Classification mechanism (verified reliable)

Classification uses the **live `InventoryItem.item_type`** through the PROTECT FK:

```
IngredientMovementLog.inventory_item  (PROTECT, never null)
  → InventoryItem.item_type ∈ {ingredient, packaging}  (DB CHECK item_type_valid)
```

Reliability argument (verified in code):

1. `IngredientMovementLog.inventory_item` is `PROTECT` and non-nullable — the join
   can never be null or dangling.
2. `InventoryItem` rows are never hard-deleted while movements reference them
   (PROTECT), and deactivation (`is_active=False`) does not affect the join.
3. `item_type` has a DB CHECK permitting only the two values.

Cross-check available but **not** the primary mechanism: consumption rows written
by `settle()` also carry `reservation_item` (SET_NULL), whose
`OrderIngredientReservationItem.component_source` ∈ {`recipe`, `packaging`} is a
write-time snapshot. The report uses the FK join as primary and does not depend on
`reservation_item` (it is null for any consumption row not produced by `settle()`,
e.g. hypothetical future writers). This is documented as a deliberate choice:
one join, no nullable-dependency, no guessing.

### 6.2 Separation rules

- Ingredients and packaging are rendered as **two separate sections** with their
  own per-unit subtotals (A7). The templates never render a single combined
  "total consumed" figure across item types.
- Within each section, rows are grouped by item snapshot and `base_unit`; a row
  never mixes base units (A8). g, ml, and pcs are different dimensions — exactly
  the Phase 7.3 rule.
- Packaging consumption follows the same reason-code split (`order_ready` vs
  `cancelled_prepared`) because packaging components are reserved and settled
  through the same reservation items (`component_source='packaging'`).

## 7. Legacy vs ingredient disposition classification (Report B)

### 7.1 Classification mechanism (verified)

`OrderCancellationDisposition.ledger` is a **write-time snapshot copied from
`OrderItem.inventory_mode`** (both fields verified; CHECK `ocd_ledger_valid`).
This is a historical fact frozen at cancellation time — no live join to
`Product.inventory_tracking_mode` is used, matching Rev3 §E.2 ("the line's mode
is a historical fact").

Historical identification: every `OrderItem` row created before Phase 4A carries
the column default `inventory_mode='legacy'` (per the model comment), and
dispositions write the ledger snapshot in the same transaction as the
cancellation. Therefore:

- `ledger='legacy'` ⇒ line was handled by the legacy `InventoryLog` ledger.
- `ledger='ingredient'` ⇒ line was reserved/settled through the ingredient ledger.

### 7.2 Reporting rules

- Both ledgers are included in the same report, grouped or labeled by `ledger`
  with human-readable labels ("Legacy stock ledger" / "Ingredient ledger") (A15).
- The report groups by `ledger`, `source`, and product name
  (`order_item__product_name` snapshot via the PROTECT join), and sums
  `prepared_units`, `unprepared_units`, `discarded_units` separately (A14).
- Rows with zero prepared and zero discarded units are **included** (A13) —
  `system_unpaid` cancellations legitimately produce all-zero-prepared rows and
  must be visible (they explain restored stock for legacy lines and released
  reservations for ingredient lines).
- Quantities stay in **product units** (A16). The report never shows base units
  and never joins its totals with Report A. `discarded_units` is displayed as a
  disposition figure only (A12) — the template copy states explicitly that
  discarded units are a cancellation disposition, not an ingredient consumption
  figure.
- No cancellation behavior or disposition record is modified (A-scope constraint).

## 8. Authorization and UI decisions

- **Admin-only.** Both views use the existing stack:
  `@login_required @admin_required` (same as `reports_index` / `export_excel`).
  No new permission system (A1). Cashiers and unauthenticated users are handled by
  the decorator's existing redirect behavior.
- **Separate read-only pages** (A1):
  - `/reports/ingredient-usage/` → `reports:ingredient_usage` →
    `templates/reports/ingredient_usage.html`
  - `/reports/cancellation-dispositions/` → `reports:cancellation_dispositions` →
    `templates/reports/cancellation_dispositions.html`
- **Navigation:** a small tab strip at the top of all three Reports pages
  (Sales Reports | Ingredient Usage | Cancellation Dispositions). The existing
  sidebar link already activates for the whole `reports` app
  (`base_admin.html:99` keys off `app_name == 'reports'`), so no sidebar change
  is required.
- **Existing page preserved:** Sales Reports content, filters, chart, tables, its
  two summary cards (Total Revenue / Total Orders) are unchanged except the
  approved hardening text changes (H4–H6) and the added tab strip (A17).
  Average Order remains removed (never restored).
- **Date filters** reuse the established From/To date-input convention and the
  parse/validate fallback behavior.
- **Empty states:** every section of both new pages has an explicit empty state
  ("No consumption recorded in this period" / "No cancellations recorded in this
  period"), mirroring the existing `empty-state` patterns in
  `templates/reports/index.html`.
- **Mobile:** both pages reuse the `reports-mobile-cards` / `report-card-item`
  card patterns and the responsive filter styles already used on the Sales
  Reports page; tables scroll horizontally inside `table-wrapper` on small
  screens.
- **Copy requirements (report pages):**
  - Report A footer/note: figures are period activity from the movement ledger —
    not a stock reconciliation and not current on-hand balances (A10).
  - Report A: `cancelled_prepared` labeled distinctly and explicitly **not** wastage (A11).
  - Report B: `discarded_units` labeled as a cancellation disposition, not an
    ingredient consumption figure (A12).

## 9. Known limitations and unresolved questions

### 9.1 Accepted limitations (documented, not blocking)

| Limitation | Impact | Disposition |
|---|---|---|
| `OrderCancellationDisposition` has no index on `decided_at` (verified: only `(order, order_item)` unique constraint). Date-range filtering is a table scan. | Bounded by number of cancelled orders × items — a small table in practice; scan is acceptable at this scale. | Accepted for this phase. **No migration is created** (explicit constraint). A future index `(decided_at)` is recorded here as a candidate for a later infrastructure phase. |
| `InventoryItem.item_type` is read via live join, not snapshotted on the movement row. If an item's `item_type` were ever changed after movements exist (admin correction), historical rows would reclassify. | `item_type` is a stable two-value category with a DB CHECK; no product requirement contemplates changing it. Cross-checkable against `reservation_item.component_source` snapshots if ever questioned. | Accepted; primary join is `inventory_item__item_type`. |
| Consumption `reason_code` values other than `order_ready`/`cancelled_prepared` would appear under their raw code. | No such writer exists today (only the two codes are written by `settle()` paths, verified). Displaying the raw code is fail-visible rather than silent. | Accepted. |
| Report B product label comes from `order_item__product_name` join (no snapshot on the disposition row itself). | `order_item` is PROTECT and orders cannot be deleted, so the join is permanent. | Accepted. |
| Excel exports for the two new reports are deferred (A18). | Admins use the pages; export comes in a later phase. | Deferred by owner decision. |

### 9.2 Unresolved questions (none blocking implementation)

1. **Exact wording of the two new report page titles/labels** — the plan fixes
   draft copy ("Ingredient Usage", "Cancellation Dispositions"); the owner may
   adjust strings at review without structural impact.
2. **Future wastage reporting** — if the owner later wants a wastage report, it
   is a third dimension (`movement_type='wastage'`) on the same pattern; out of
   scope here by A3/A12.

## 10. Sales Reports export-hardening requirements (H1–H7)

These apply to the **existing** Sales Reports page and its Excel export only.

| # | Requirement | Approved behavior |
|---|---|---|
| H1 | Excel formula injection fix | All user-controlled display columns (Customer, Cashier, and any future text) are written as **text cells**, never formula cells. Mechanism: write the string and force `cell.data_type = 's'` (verified against openpyxl 3.1.5 by round-trip probe: a value `'=cmd` written with `data_type='s'` reopens as `data_type='s'` with exact text preserved; the apostrophe-prefix alternative was rejected because openpyxl stores the `'` literally in the value). Character neutralization is thus achieved by type enforcement — formula-leading characters (`=`, `+`, `-`, `@`) in user text can never execute. Legitimate numeric cells (Total) remain numeric (`data_type='n'`). Regression tests must save + reopen the workbook and assert no text cell is a formula. |
| H2 | Maximum date range for Sales Reports | **92 days** (one quarter) maximum span for both the page and the export (shared parse path). A request with a larger span is clamped to `end_date - 91 days … end_date` (most recent 92 days of the requested window), and the page shows a visible notice with the effective range. No silent enormous daily arrays; `daily_sales` is capped at 92 entries by construction. Documented in code as `MAX_SALES_RANGE_DAYS = 92`. |
| H3 | Export result-size protection | **5,000 orders** maximum per export (`MAX_EXPORT_ROWS = 5000`). The view counts matching orders first; on exceed it returns an HTTP 400 response with a clear HTML message ("Export too large — narrow the date range") and **no workbook**. No silent truncation, ever. |
| H4 | Misleading label fix | Summary card sub-label `Paid orders` → **`Paid & completed orders`** (matches the actual `is_paid + status='completed'` filter). |
| H5 | Packaging-fee exclusion notes | Visible footnote on Top Products and Sales by Category sections: these exclude packaging fees (fees are not part of item subtotals). |
| H6 | Column rename | Export header `Items` → **`Line items`** (the value counts distinct `OrderItem` rows, not total sold units). |
| H7 | Obsolete `pending` fixtures | Replace `status='pending'` test fixtures with `awaiting_payment` (or `preparing` where the test means "in preparation") **only** in reports tests that intend to verify paid-but-not-completed exclusions: `tests_cancelled_orders.py`, `tests_category_sales.py`, `tests_daily_sales.py`, `tests_export.py`. Assertions themselves stay identical — only the invalid status strings change. (`Order.STATUS_CHOICES` has no `pending`; the ORM writes it without validation, so the tests pass today while testing a state that cannot exist.) |

H8/H9 (owner constraints): the Sales Reports date basis stays `created_at`, and
`created_at__date` predicates are **not** optimized in this phase without measured
evidence and a coordinated design decision.

## 11. Exclusions and anti-double-counting rules (cross-report)

1. Report A and Report B are independent query paths; neither feeds the other.
2. `discarded_units` (Report B) is never added to Report A consumption or to any
   wastage figure (A12). Data-model reality: for ingredient-ledger lines,
   prepared units consume via `settle()` movement rows (which Report A counts
   under `cancelled_prepared`); the disposition's `discarded_units` is a
   declaration about those prepared units, not an additional movement. For
   legacy-ledger lines, prepared units change no stock at all (Rev3 §H.8).
   Summing disposition figures into movement totals would double-count in the
   first case and fabricate a quantity in the second.
3. Report A never sums `reserved_delta`, never includes reservation/release
   movements, and never nets consumption against purchases.
4. Report B never sums across ledgers, never merges product units with base
   units, and never sums `prepared + unprepared + discarded` into one figure.
5. Reports never write to the database; no movement, disposition, or order
   mutation exists anywhere in the phase.

## 12. Explicitly out of scope

- Excel exports for the two new reports (A18).
- New models, migrations, fields, or indexes (including a `decided_at` index).
- Any change to movement types, reason codes, reservation/settlement/cancellation
  services, or `InventoryLog`.
- Stock reconciliation, on-hand derivation, valuation, or costing.
- Per-order drill-down views.
- Chart.js visualizations on the new pages (tables + cards only; charts can come
  later without contract changes).
- Optimization of `created_at__date` predicates in existing sales views (H9).
- Restoring Average Order (removal is intentional and permanent).
- Any modification to the authoritative Rev3 document.

## 13. Sign-off

Prepared as the Phase 7.4 decision record from the approved scope; all model
fields, relationships, indexes, write sites, and openpyxl behaviors cited above
were re-verified against the working tree at `956f22f` before writing
(investigation date 2026-11-13). Implementation must follow the companion
implementation plan and may not invent requirements beyond this record.
