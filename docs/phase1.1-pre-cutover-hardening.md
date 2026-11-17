# Phase 1.1 — Pre-Cutover Hardening and PostgreSQL Verification

Status: **BLOCKED — POSTGRESQL VERIFICATION REQUIRED** (all SQLite work
complete and green; the 5 new PostgreSQL concurrency tests are written,
collect cleanly, and skip on SQLite — execution pending a PostgreSQL
server, which is not installed locally).

## What was inspected

- `apps/menu/services.py` — Phase 1 readiness gates (10 checks) and
  `change_inventory_tracking_mode()` locking design.
- `apps/menu/tests_phase1_cutover.py` (30 tests) and
  `apps/menu/tests_phase8f2.py` (23 tests) — both suites passing before
  this phase; 65 combined.
- `docs/phase1-cutover-service.md` and the Rev3 architecture document
  (§H.1 lock hierarchy, §M.7 concurrency-test design).
- Existing PostgreSQL test infrastructure:
  `apps/inventory/tests_phase2a_pg.py`, `apps/orders/tests_phase5b7_pg.py`
  (TransactionTestCase + worker threads + threading.Barrier +
  `_require_postgresql` skip decorator).
- Inventory locking helper: `lock_inventory_items()`
  (apps/inventory/services.py:159) — single ordered
  `SELECT ... FOR UPDATE`, ascending pk.
- Order placement: `place_order_inventory()` (apps/orders/services.py:493+)
  — locks Order row, then Product rows (ascending), then InventoryItems
  (ascending); re-snapshots `inventory_mode` under the Product lock.

## What was changed

| File | Change |
|---|---|
| `apps/menu/services.py` | Added `cutover_stock_report()` (read-only §5 report: classification, per-item stock rows, limiting items, low-stock warnings, `few_units` flag, `client_confirmed=False` always) + `FEW_UNITS_THRESHOLD`. No existing gate or locking code was modified. |
| `apps/menu/tests_phase1_cutover.py` | +12 tests: released/consumed reservations do not block; reserved-blocks-even-with-completed-order; mixed-scope ambiguity (all + dine_in approved) blocks; per-status open-order blocking; 7 stock-report classification/read-only tests. |
| `apps/menu/tests_phase11_pg.py` | NEW — 5 PostgreSQL-only concurrency tests (scenarios A–E). Skip cleanly on SQLite. |
| `docs/phase1.1-pre-cutover-hardening.md` | This document. |

## Readiness-rule verification

All 10 gates were compared against the real workflow constants:

- `ACTIVE_ORDER_STATUSES = ('awaiting_payment', 'preparing', 'ready')`
  (services.py:22) — gate 9 blocks each of these individually
  (subTest regression test) and never blocks `completed`/`cancelled`
  (existing test retained).
- Reservation gate 10 keys on `OrderIngredientReservation.STATUS_RESERVED`
  via `order_item__product` **or** `source_recipe__product` — regression
  tests now cover reserved / released / consumed explicitly, including a
  reserved line whose order is already completed (still blocks).
- Mixed-scope ambiguity (approved `all` + approved `dine_in` for the same
  size) is counted as two applicable recipes and blocks the cutover.
- No gate was weakened. `resolve_plan()`-based gates delegate to the
  Phase 5B resolver.

## Lock-ordering analysis (source-verified)

| Operation | Lock sequence |
|---|---|
| Cutover | Product rows (ascending pk) → readiness pass 1 → InventoryItems (ascending, shared helper) → readiness pass 2 → write + strict audit |
| Placement | Order row → Product rows (ascending) → InventoryItems (ascending) |
| Settlement | Reservation/Order rows per existing lifecycle services |

Both writers acquire Product rows in ascending-pk order **before** any
InventoryItem lock, and both use single ordered statements for the item
level. A consistent global order (Order < Product < Item; ascending pk
within a level) means no wait-cycle can form between a cutover and a
placement or between two cutovers. Scenario D (overlapping item sets)
serialises on the Product level before either touches InventoryItems.
Scenario B (placement racing cutover) serialises on the Product row; the
placement re-snapshots `inventory_mode` under that lock
(apps/orders/services.py:610-636), so order lines can never carry a torn
legacy/ingredient mix. Scenario E relies on the outer
`transaction.atomic()` in `change_inventory_tracking_mode` — a failure
after some products were saved rolls the whole batch back (regression
tested on SQLite for the audit-failure path; PG atomicity test added).

## PostgreSQL concurrency tests

New file: `apps/menu/tests_phase11_pg.py` (5 tests, scenarios A–E).

Execution status: **PENDING — no PostgreSQL server is installed locally**
(port 5432 has no listener; verified this session). The tests skip
themselves on SQLite with an explanatory message; collecting them on
SQLite reports `Ran 5 tests, OK (skipped=5)`.

Commands (once PostgreSQL is available and the test settings point at it):

```
# 1. Ensure a PostgreSQL server is reachable and a test database user exists.
# 2. Point the test settings at PostgreSQL, e.g. set DATABASE_URL:
set DATABASE_URL=postgres://<user>:<password>@localhost:5432/kdm_test
# 3. Run the concurrency suites (isolated test database — the dev DB is
#    never touched; Django's test runner creates/destroys its own DB):
.\venv\Scripts\python.exe manage.py test apps.menu.tests_phase11_pg --verbosity=2
.\venv\Scripts\python.exe manage.py test apps.inventory.tests_phase2a_pg apps.orders.tests_phase5b7_pg --verbosity=2
```

## Stock-shortage findings (live-catalog dry run, read-only)

`cutover_readiness_report()` + `cutover_stock_report()` were executed
against the local development database; a full state snapshot taken
before and after is byte-identical (no audit rows, no timestamp changes,
no stock/reservation/order/movement/mode changes).

Actual results (differ from the previously reported figures because the
Phase 8F demonstration cutover already ran and consumed stock):

| Classification | Count |
|---|---:|
| Technically eligible | 36 |
| Blocked by configuration (Tuna Pastil) | 1 |
| Blocked by insufficient stock | 0 |
| Already ingredient-tracked | 4 (Mocha, Wintermelon, Okinawa, Special Pork Burger) |
| Inactive | 18 |
| Client-confirmed | **0** |

59 products total; product modes now 54 legacy / 5 ingredient
(DebugProd2 + the four demo products).

Low-stock / few-units findings among eligible products:

- **Burger Wrapper is the limiting item for all three burger products
  (p32, p33, p35): maximum 3 producible takeout units each** (dine-in is
  higher: 40/25/25). The wrapper is also at/below its own low-stock
  threshold — both warnings fire in the report.
- Matcha Powder limits p12/p13 to 10 units.
- Squid Roll / Tempura pieces limit p38/p39 to 8 units.
- Milk Base limits most coffee/milk-tea drinks to 26–38 units.
- No eligible product is at zero stock; `blocked_insufficient_stock` is
  empty — every technically eligible product can produce ≥ 3 units.

## Technical eligibility versus client approval

`cutover_stock_report()` reports `client_confirmed: False` for **every**
product and `client_confirmed: 0` in the summary. The sample recipes and
packaging are development fixtures; the report never infers approval from
configuration completeness. A future client-confirmation field must be
added deliberately (e.g. an explicit sign-off record), never derived.

## Tuna Pastil (p27) — remaining blockers

1. No approved recipe for `none/dine_in` or `none/takeout` (only the empty
   unapproved Phase 8C draft exists — retained deliberately).
2. No packaging rules for either order type.
3. Consequently `resolve_plan()` cannot resolve any slot.
4. Opening-stock gate cannot be evaluated (unresolved plans).

The product remains blocked; nothing was created for it.

## Remaining risks before an actual cutover

1. PostgreSQL concurrency suite has never been executed (no server) — the
   lock-ordering argument is source-verified but not runtime-verified.
2. All recipe/packaging data is SAMPLE/illustrative — `client_confirmed`
   is false everywhere by design.
3. The Phase 8F demonstration already consumed stock (e.g. Burger Wrapper
   at 3 pcs, Chocolate Powder reduced by the Mocha demo orders); re-run
   the stock report immediately before any real cutover.
4. No `ProductInventoryModeChange` audit model exists; cutover history
   lives in the generic audit log.
5. Migration metadata drift in `accounts/0006` and `orders/0026` is
   pre-existing and untouched by this phase.

## Final status

**BLOCKED — POSTGRESQL VERIFICATION REQUIRED.**

All SQLite-side work is complete and green (see the Phase 1.1 report for
exact counts). The 5 new PostgreSQL concurrency tests must be executed
against PostgreSQL before the cutover service can be considered
runtime-verified for production review. Nothing in this phase executed a
cutover or changed any tracking mode.
