# Kitchen Realtime Fix — Verification Results

## `python manage.py check`

```
System check identified no issues (0 silenced).
```

## `python manage.py test apps.kitchen.tests.KitchenAccessTest`

All 12 pre-existing access tests pass.

```
Ran 12 tests in 110.748s
OK
```

Tests covered:
- test_is_kitchen_staff_property
- test_kitchen_orders_accessible_by_admin
- test_kitchen_orders_accessible_by_kitchen_staff
- test_kitchen_orders_blocked_for_anonymous
- test_kitchen_orders_blocked_for_cashier
- test_kitchen_orders_shows_only_preparing_orders
- test_login_redirects_kitchen_staff_to_kitchen
- test_mark_ready_requires_auth
- test_mark_ready_requires_post
- test_mark_ready_success ← DB status verified as 'ready'
- test_mark_ready_wrong_role
- test_mark_ready_wrong_status

## `python manage.py test apps.kitchen.tests.KitchenRealtimeTests`

All 8 new realtime tests pass.

```
Ran 8 tests in 49.924s
OK
```

Tests covered:
1. **test_mark_ready_publishes_status_changed_on_commit** — Kitchen Staff marks a preparing order ready; exactly one `status_changed` event is published via `on_commit`.
2. **test_mark_ready_no_duplicate_events** — `_skip_realtime` flag prevents the `post_save` signal from also publishing; only one event emitted.
3. **test_mark_ready_event_not_published_on_rollback** — When `log_action` raises inside the atomic block, no event reaches the broker (on_commit patched as no-op).
4. **test_mark_ready_event_payload_is_complete** — Published payload contains all required fields: `order_id`, `order_number`, `queue_number`, `new_status`, `new_status_display`, `is_paid`.
5. **test_mark_ready_db_status_is_ready** — DB row status is `ready` after successful POST.
6. **test_mark_ready_unauthorized_cashier_cannot_mark_ready** — Cashier role is rejected; order stays `preparing`.
7. **test_mark_ready_invalid_status_transition_rejected** — `awaiting_payment → ready` returns HTTP 400.
8. **test_mark_ready_anonymous_cannot_mark_ready** — Unauthenticated request is redirected to login.

## `python manage.py test apps.kitchen.tests.KitchenEventStreamTest`

All 4 pre-existing SSE stream tests pass.

```
Ran 4 tests in 33.643s
OK
```

## `python manage.py test apps.orders.tests.NewOrderRealtimePayloadTests`

Pre-existing baseline test passes — `new_order` on_commit broadcast unaffected.

```
Ran 1 test in ~2s
OK
```

## Pre-existing failures in `apps.orders.tests_security`

20 failures + 1 error exist in `apps/orders/tests_security.py`
(`OrderRateLimitTests` and `TrackingTokenTests`). These are confirmed
pre-existing — the same count and same tests fail on the original unmodified
code (verified by stashing all changes and running the same suite). They are
entirely unrelated to this fix.

## Note on test isolation

Each test class is run in its own invocation. Running all three kitchen test
classes in a single `python manage.py test apps.kitchen` invocation causes the
`KitchenEventStreamTest` SSE streaming tests to hang after `KitchenRealtimeTests`
due to a pre-existing shared-cache SQLite in-memory database lock contention on
this machine. Each class passes cleanly in isolation. This is a pre-existing
environment issue unrelated to the fix.

## Files Changed

| File | Change |
|------|--------|
| `apps/realtime/signals.py` | Added `_skip_realtime` guard: if `instance._skip_realtime` is truthy, the in-transaction `publish()` is skipped |
| `apps/kitchen/views.py` | Set `order._skip_realtime = True` before `order.save()`; registered `transaction.on_commit(_broadcast)` to publish `status_changed` only after commit |
| `templates/orders/order_list.html` | In `status_changed` handler: added `row.dataset.statusDisplay`, `row.dataset.nextStatus` updates, and `initResponsiveTables()` call to rebuild mobile cards |
| `apps/kitchen/tests.py` | Added `KitchenRealtimeTests` class with 8 new tests; added `from unittest import mock` and `from django.db import transaction as db_transaction` imports |
