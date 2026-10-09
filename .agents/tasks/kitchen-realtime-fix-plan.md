# Implementation Plan: Fix Realtime Order Status Sync (Kitchen → Cashier)

## Findings from Code Inspection

### Root Cause 1 — `post_save` signal publishes INSIDE the transaction (confirmed)

File: `apps/realtime/signals.py`, function `order_saved` (line 17–27)

```python
@receiver(post_save, sender=Order)
def order_saved(sender, instance, created, **kwargs):
    ...
    else:
        publish('status_changed', { ... })   # fires synchronously, INSIDE atomic()
```

File: `apps/kitchen/views.py`, function `mark_order_ready` (lines 27–42)

```python
with transaction.atomic():
    order = get_object_or_404(Order.objects.select_for_update(), pk=pk)
    ...
    order.status = 'ready'
    order.ready_at = timezone.now()
    order.save()   # ← post_save fires HERE, inside the atomic block
    log_action(...)
# transaction commits only AFTER this point
```

The `post_save` signal fires synchronously at `order.save()`, before `transaction.atomic()` exits. The broker's `publish()` puts the event into every connected subscriber queue immediately. If the transaction later rolls back (e.g., `log_action` throws), connected cashiers receive a `status_changed: ready` for an order that is still `preparing` in the database. This is architecturally wrong, and it is also the reason the cashier sometimes sees nothing: the event was published, but the row lock was still held when the cashier's page query ran, which can cause ordering inconsistencies.

The **correct pattern**, already established in `checkout_view` (`apps/orders/views.py`, line 525) and `create_pos_order` (same file, ~line 1555), is:

```python
transaction.on_commit(lambda: publish('status_changed', {...}))
```

This defers the broadcast until after the transaction successfully commits. If the transaction rolls back, the callback is never called — no false event is published.

### Root Cause 2 — `status_changed` JS handler does NOT rebuild mobile cards (confirmed)

File: `templates/orders/order_list.html`, `status_changed` handler (lines ~688–754)

The handler:
1. Updates `row.cells[5]` — the status badge in the desktop table. ✓
2. Updates `row.cells[row.cells.length - 3]` — the payment cell. ✓
3. Updates the quick-advance button. ✓
4. Updates `row.dataset.isPaid` and `row.dataset.status`. ✓
5. **Missing: does NOT update `row.dataset.statusDisplay`.**
6. **Missing: does NOT call `buildOrderCards()` / `initResponsiveTables()`.**

The mobile card builder (`buildOrderCards`) reads `row.dataset.statusDisplay` to render the status badge text. The `status_changed` handler updates `row.dataset.status` but never updates `row.dataset.statusDisplay`, so after an SSE update the mobile card would still show "Preparing" even if `buildOrderCards` were called.

Additionally, the handler never calls `initResponsiveTables()` (which hooks into `buildOrderCards()`), so mobile cards are never rebuilt after a status change. Compare this to the `gcash_submitted` handler (lines ~771–790), which correctly calls `initResponsiveTables()` after updating the row data.

### Root Cause 3 — `status_changed` handler does NOT update `row.dataset.nextStatus` (confirmed)

The mobile card builder uses `row.dataset.nextStatus` to determine whether to render an advance button. The `status_changed` handler updates `row.dataset.status` but never updates `row.dataset.nextStatus`. After a `preparing → ready` transition:
- Desktop: the advance button is correctly hidden (handler step 3 does this).
- Mobile: `row.dataset.nextStatus` still holds `'ready'`, so `buildOrderCards()` would render a stale advance button pointing to `ready`.

### Cell Index Verification — `row.cells[5]` is CORRECT

The `<thead>` row (line 337) has exactly 9 columns in this order:

| Index | Column      |
|-------|-------------|
| 0     | Order #     |
| 1     | Customer    |
| 2     | Type/Table  |
| 3     | Items       |
| 4     | Total       |
| **5** | **Status**  |
| 6     | Payment     |
| 7     | Time        |
| 8     | Actions     |

`row.cells[5]` is the Status column. This is **correct**. No fix needed here.

### `update_order_status` — also publishes inside transaction (OUT OF SCOPE, noted only)

`apps/orders/views.py`, `update_order_status` (~line 896): also calls `order.save()` inside `transaction.atomic()` without `on_commit`, so the `post_save` signal fires inside the transaction there too. Fixing this is **out of scope** for this task but should be a follow-up.

### `checkout_view` and `create_pos_order` use `on_commit` correctly (confirmed baseline)

Both use `transaction.on_commit(_broadcast_fn)` for `new_order` events. The fix for `mark_order_ready` must follow exactly this pattern.

### Existing realtime kitchen-to-cashier test coverage

`apps/kitchen/tests.py` has `test_mark_ready_success` which verifies `order.status == 'ready'` in the DB but does **not** assert that a `status_changed` event was published, nor that it was deferred via `on_commit`. No test currently covers the cashier-side JS update path.

---

## Plan

### Fix 1 — Backend: publish `status_changed` after commit in `mark_order_ready`

**What:** In `mark_order_ready`, replace the implicit signal-based publish with an explicit `transaction.on_commit` callback that fires after the atomic block exits successfully. Then suppress the automatic `post_save` signal for this save so no duplicate event is published.

**Decision:** The cleanest approach is to suppress the `post_save` signal for this specific save using Django's `update_fields` trick — passing `update_fields` does not prevent `post_save` from firing. Instead, use the established pattern from the codebase: disable the signal for the duration of this save by temporarily disconnecting it, or — simpler and already used elsewhere — pass a `skip_realtime=True` marker through `update_fields` sentinel. Looking at the codebase, the simplest safe approach is: **move the publish out of the signal and into an explicit `on_commit` in the view**, and suppress the `post_save` signal from firing on this save by using `Order.objects.filter(pk=order.pk).update(status='ready', ready_at=...)` (a queryset `.update()` does not trigger `post_save` signals). However, that would bypass the `order.save()` timestamp logic.

The most targeted fix that matches the existing pattern: call `order.save()` as today, then **immediately disconnect and reconnect** the signal — no, that is too fragile.

**Chosen approach:** Add a `_skip_realtime` attribute flag on the instance before saving. In `order_saved`, check for this flag and skip the publish if set. Then register an explicit `on_commit` in the view. This is a one-line guard in the signal and follows zero new infrastructure. The signal still fires (keeping future hooks possible) but the in-transaction broadcast is suppressed for this save.

Specifically:
1. In `mark_order_ready`, before `order.save()`: set `order._skip_realtime = True`.
2. After `order.save()`, register `transaction.on_commit(lambda: publish('status_changed', {...}))` with the full payload.
3. In `order_saved` signal: add `if getattr(instance, '_skip_realtime', False): return` before `publish(...)`.

This is minimal, targeted, cannot cause duplicates, and exactly mirrors the `checkout_view` on_commit pattern.

**Files to change:**
- `apps/kitchen/views.py`
- `apps/realtime/signals.py`

**Lines to change:**

`apps/realtime/signals.py` — add one guard line (around line 19):
```python
@receiver(post_save, sender=Order)
def order_saved(sender, instance, created, **kwargs):
    order = instance
    if created:
        pass
    else:
        if getattr(order, '_skip_realtime', False):   # ← ADD THIS
            return                                      # ← ADD THIS
        publish('status_changed', {
            ...
        })
```

`apps/kitchen/views.py` — set flag, add on_commit (around lines 35–42):
```python
with transaction.atomic():
    order = get_object_or_404(Order.objects.select_for_update(), pk=pk)
    try:
        validate_status_transition(order.status, 'ready', order=order)
    except ValueError as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

    order.status = 'ready'
    order.ready_at = timezone.now()
    order._skip_realtime = True          # ← ADD: suppress post_save broadcast
    order.save()

    log_action(request.user, 'order.mark_ready', order,
               detail='Kitchen staff marked order as ready.')

    def _broadcast():                    # ← ADD: on_commit broadcast
        from apps.realtime.broker import publish
        publish('status_changed', {
            'order_id':           order.pk,
            'order_number':       order.order_number,
            'queue_number':       order.queue_number,
            'new_status':         order.status,
            'new_status_display': order.get_status_display(),
            'is_paid':            order.is_paid,
        })
    transaction.on_commit(_broadcast)   # ← ADD
```

Note: `publish` is already imported at module level in `signals.py`. In `views.py` it needs to be imported locally inside `_broadcast` (as done in `checkout_view`) or added to the module-level imports. Use the local import pattern to match `checkout_view`.

**Verify:**
```
python manage.py test apps.kitchen.tests -v 2
```
All existing kitchen tests must still pass. New tests (added in Fix 3) must pass.

---

### Fix 2 — Frontend: update `row.dataset.statusDisplay` and rebuild mobile cards in `status_changed` handler

**What:** In the `status_changed` JS handler in `templates/orders/order_list.html`, add three missing operations after the existing step 4 (around line 751):
1. Update `row.dataset.statusDisplay` with `data.new_status_display`.
2. Update `row.dataset.nextStatus` with the correct next status (using the same `forwardFlow` map already computed in step 3), or `''` if terminal.
3. Call `initResponsiveTables()` to trigger `buildOrderCards()` and rebuild mobile cards.

**Exact location:** In the `status_changed` handler, after the existing line:
```js
row.dataset.status = newStatus;
```
Add:
```js
row.dataset.statusDisplay = data.new_status_display || newStatus;
row.dataset.nextStatus    = nextStatus || '';          // nextStatus already computed above
if (typeof window.initResponsiveTables === 'function') {
  window.initResponsiveTables();
}
```

The variable `nextStatus` is already declared in step 3 of the handler (`const nextStatus = forwardFlow[newStatus]`), so no extra computation is needed.

**Files to change:**
- `templates/orders/order_list.html`

**Verify:** Manual browser test — Kitchen Staff marks an order ready; on the Cashier's Order Management page, the desktop status cell updates to "Ready" and (on mobile viewport) the card badge also updates. Automated JS test (added in Fix 3) covers this.

---

### Fix 3 — Tests: add/update tests for the kitchen-to-cashier realtime path

**What:** Add a new `KitchenRealtimeTests` class to `apps/kitchen/tests.py` covering:

1. **`test_mark_ready_publishes_status_changed_on_commit`** — Kitchen Staff marks a `preparing` order ready. With `transaction.on_commit` patched to fire immediately (same technique as `NewOrderRealtimePayloadTests` in `apps/orders/tests.py`), subscribe to the broker, POST to `mark_ready`, drain the queue, assert exactly one `status_changed` event was published with `new_status == 'ready'` and the correct `order_id`.

2. **`test_mark_ready_no_duplicate_events`** — Same setup; verify only one `status_changed` event was published (not two — confirming the `_skip_realtime` flag prevents the signal from also publishing).

3. **`test_mark_ready_event_not_published_on_rollback`** — Patch `log_action` to raise an exception inside the atomic block after `order.save()`. Assert that `on_commit` was registered but the broker received no event (because the transaction rolled back). This proves the fix is correct for the rollback case. Since Django's `TestCase` wraps in a transaction, patch `transaction.on_commit` to be a no-op; then assert the broker queue is empty after the (deliberately failed) POST.

4. **`test_mark_ready_event_payload_is_complete`** — Assert the published payload contains all required fields: `order_id`, `order_number`, `queue_number`, `new_status`, `new_status_display`, `is_paid`.

5. **`test_status_changed_js_handler_updates_dataset`** — Document this as a manual browser test note (Selenium/Playwright is out of scope). Include a comment explaining what to verify manually.

Also update the existing `test_mark_ready_success` to assert that the broker received a `status_changed` event (using the patched `on_commit` technique).

**Files to change:**
- `apps/kitchen/tests.py`

**Verify:**
```
python manage.py test apps.kitchen -v 2
python manage.py test apps.orders.tests.NewOrderRealtimePayloadTests -v 2
python manage.py check
```
All tests must pass. `python manage.py check` must produce `System check identified no issues (0 silenced)`.

---

### Fix 4 — Verify no existing tests regress

**What:** Run the full test suite for the affected apps to confirm no existing tests broke.

```
python manage.py test apps.kitchen apps.orders apps.realtime -v 2
python manage.py check
```

Expected: all tests pass, `manage.py check` is clean.

---

## Ordered Implementation Checklist

- [ ] 1. **`apps/realtime/signals.py`** — Add `_skip_realtime` guard.
      - Around line 19, inside `order_saved`, add two lines before `publish(...)`:
        ```python
        if getattr(order, '_skip_realtime', False):
            return
        ```
      - No other changes to this file.
      - Files: `apps/realtime/signals.py`
      - Verify: `python manage.py test apps.kitchen apps.orders -v 2` — all tests pass.

- [ ] 2. **`apps/kitchen/views.py`** — Set `_skip_realtime` flag and register `on_commit` broadcast.
      - Add `order._skip_realtime = True` before `order.save()` (around line 38).
      - After `order.save()`, define `_broadcast` closure and call `transaction.on_commit(_broadcast)`.
      - Remove the comment `# signals.py fires status_changed automatically — no manual publish needed`; replace with a comment explaining the on_commit approach.
      - Files: `apps/kitchen/views.py`
      - Verify: `python manage.py test apps.kitchen -v 2` — all existing tests pass.

- [ ] 3. **`templates/orders/order_list.html`** — Fix `status_changed` JS handler to update `statusDisplay`, `nextStatus`, and rebuild mobile cards.
      - In the `status_changed` handler, after `row.dataset.status = newStatus;` (around line 751), add:
        ```js
        row.dataset.statusDisplay = data.new_status_display || newStatus;
        row.dataset.nextStatus    = nextStatus || '';
        if (typeof window.initResponsiveTables === 'function') {
          window.initResponsiveTables();
        }
        ```
      - Files: `templates/orders/order_list.html`
      - Verify: Manual browser test — open Order Management page as cashier, mark an order ready from kitchen, confirm desktop badge changes to "Ready" and mobile card badge also updates without page reload.

- [ ] 4. **`apps/kitchen/tests.py`** — Add `KitchenRealtimeTests` class with the 4 new tests listed in Fix 3. Update `test_mark_ready_success` to also assert a `status_changed` event was published.
      - Files: `apps/kitchen/tests.py`
      - Verify: `python manage.py test apps.kitchen -v 2` — all tests including new ones pass.

- [ ] 5. **Final verification** — Run full check.
      - `python manage.py test apps.kitchen apps.orders apps.realtime -v 2`
      - `python manage.py check`
      - Confirm: all tests pass, no issues reported.

---

## Summary of Root Causes

| # | Root Cause | File | Fix |
|---|-----------|------|-----|
| 1 | `post_save` signal publishes inside `transaction.atomic()` — event fires before commit | `apps/realtime/signals.py`, `apps/kitchen/views.py` | Add `_skip_realtime` flag; move publish to `transaction.on_commit` |
| 2 | `status_changed` JS handler doesn't update `row.dataset.statusDisplay` | `templates/orders/order_list.html` ~line 751 | Add one dataset update line |
| 3 | `status_changed` JS handler doesn't update `row.dataset.nextStatus` | `templates/orders/order_list.html` ~line 751 | Add one dataset update line |
| 4 | `status_changed` JS handler doesn't rebuild mobile cards | `templates/orders/order_list.html` ~line 753 | Call `initResponsiveTables()` |

## Out of Scope (noted for follow-up)

- `update_order_status` in `apps/orders/views.py` (~line 896) has the same inside-transaction publish pattern. The fix is the same pattern but is explicitly excluded from this task's scope.
