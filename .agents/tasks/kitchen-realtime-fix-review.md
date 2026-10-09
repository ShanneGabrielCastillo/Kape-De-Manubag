# Kitchen-to-Cashier Realtime Status Sync Fix

The fix wires up the missing realtime path that lets a cashier's Order
Management page update automatically when Kitchen Staff marks an order
ready. Three independent gaps were closed: the backend was broadcasting
the SSE event inside an uncommitted transaction, the cashier's JavaScript
handler was not updating the data attributes that the mobile card builder
reads, and the handler was not triggering a mobile card rebuild at all.
The approach uses the `_skip_realtime` flag on the model instance to
suppress the `post_save` signal's in-transaction broadcast, then registers
an explicit `transaction.on_commit` callback — the same pattern already
established by `checkout_view` and `create_pos_order`.

Watch for: the `isTerminal` variable is declared in the JS handler but
never used (possible dead code, low severity). The Preparing filter does
not remove rows when their status advances to ready — the row stays in the
DOM with its badge updated (possible gap depending on intended UX). Payment
column addressing uses `row.cells[row.cells.length - 3]` rather than the
literal `cells[6]` used in the `gcash_submitted` handler — equivalent
today but fragile if the column count ever changes.

**Verdict**: APPROVED

---

## High-level view

The backend fix is structurally correct. `mark_order_ready` sets
`order._skip_realtime = True` before `order.save()` so the `post_save`
signal in `signals.py` short-circuits before calling `publish()`, then
registers `transaction.on_commit(_broadcast)` to emit `status_changed`
only after the database commit succeeds. The captured closure variables
(`_order_id`, `_order_num`, etc.) avoid a use-after-transaction-end
problem that would occur if the closure read `order.*` attributes after the
atomic block. The no-duplicate guarantee is tested directly.

The JS fix adds the two missing dataset attribute updates
(`statusDisplay`, `nextStatus`) and the `initResponsiveTables()` call that
rebuilds mobile cards. These were identified as separate confirmed root
causes; all three are now addressed. The `reinitLucide()` call correctly
follows the card rebuild to initialize any new icon elements.

The filter-aware behavior for the Preparing → Ready transition is not
explicitly handled: when a cashier is viewing the Preparing filter, the
row's badge updates in place but the row is not removed from the filtered
view. The `new_order` handler uses `location.reload()` when a filter is
active; the `status_changed` handler does not. Whether this constitutes a
gap depends on the intended UX — the task specification says "update the
UI according to the existing filtering behavior", which is ambiguous enough
that this is flagged as a possible concern rather than a hard block.

The test suite covers the critical behavioral contract: one event emitted
per commit, zero events on rollback, correct payload fields, role
enforcement, and invalid transition rejection. The 8 new tests all pass per
the verify artifact. The `on_commit` testing pattern — patching
`db_transaction.on_commit` to `side_effect=lambda fn: fn()` — is the same
technique used elsewhere in the project and is appropriate for Django's
`TestCase` wrapper.

---

<details>
<summary>Issues (3)</summary>

1. **`isTerminal` dead variable** — `const isTerminal = newStatus === 'completed' || newStatus === 'cancelled'` is declared in the `status_changed` handler but never read. Low severity — no behavioral impact, but it leaves an unused symbol in production code. Remove it or use it.

2. **Preparing-filter row not removed on status advance** — When the cashier is on the Preparing filter tab (`?status=preparing`) and an order advances to ready via SSE, the handler updates the badge in place but leaves the row visible in the Preparing filter view. The `new_order` handler takes the reload-on-filter approach for consistency; the `status_changed` handler has no equivalent. The task spec says to follow "existing filtering behavior", which is not defined for this case. If the intended behavior is to remove the row from the Preparing filter when it transitions to ready, add a check: `if (currentFilterStatus && currentFilterStatus !== newStatus) { row.remove(); initResponsiveTables(); }`.

3. **Payment column addressed by relative index** — `row.cells[row.cells.length - 3]` is equivalent to `row.cells[6]` for the current 9-column table, but the `gcash_submitted` handler uses the literal `cells[6]`. The asymmetry is not a bug today but is a latent maintenance hazard if columns are added or reordered. Low severity — no action required before shipping, but worth normalizing in a follow-up.

</details>

---

<details>
<summary>Details</summary>

### `on_commit` placement and duplicate-event prevention

`mark_order_ready` sets `order._skip_realtime = True` immediately before
`order.save()`. The guard in `signals.py` is:

```python
if getattr(order, '_skip_realtime', False):
    return
```

This fires on the `post_save` signal synchronously inside the
`transaction.atomic()` block and exits before reaching `publish()`. The
`transaction.on_commit(_broadcast)` call is registered after `order.save()`
but still inside the atomic block, which is correct — `on_commit` callbacks
registered inside an atomic block fire when the outermost transaction
commits. If `log_action` raises after the save, the transaction rolls back
and the callback is never called. `test_mark_ready_event_not_published_on_rollback`
confirms this via a no-op `on_commit` patch.

The closure variables (`_order_id`, `_order_num`, `_queue_num`, `_status`,
`_status_disp`, `_is_paid`) are captured from the order object's state
before the atomic block exits. This correctly handles the edge case where
Django might reuse the same request scope across the commit boundary.

### `status_changed` JS handler — dataset sync and mobile rebuild

Before this fix, the handler updated `row.dataset.status` but not
`row.dataset.statusDisplay` or `row.dataset.nextStatus`. The mobile card
builder (`buildOrderCards` via `initResponsiveTables`) reads both of these
attributes to render the badge text and the advance button label. The fix
adds:

```js
row.dataset.statusDisplay = data.new_status_display || newStatus;
row.dataset.nextStatus    = nextStatus || '';
if (typeof window.initResponsiveTables === 'function') {
  window.initResponsiveTables();
}
window.reinitLucide && window.reinitLucide();
```

`nextStatus` is already computed above in step 3
(`const nextStatus = forwardFlow[newStatus]`), so no duplication. The
`reinitLucide` call after `initResponsiveTables` is the correct ordering —
icons can only be initialized after their elements exist in the DOM.

The `isTerminal` variable declared at the top of the handler is never
referenced anywhere in the handler body. It is dead code (confirmed by
reading the entire handler — no branch conditions or assignments reference
it). This is a minor cleanup item, not a behavioral bug.

### Filter-aware row visibility gap (Preparing filter)

The status tabs (`All Orders`, `Preparing`, `Ready`, etc.) are server-side
URL filters — the page loads only the matching rows. When the cashier is on
the `?status=preparing` tab and an SSE `status_changed` event arrives for a
`preparing → ready` transition, the handler finds the row (it's in the DOM
because it was preparing when the page loaded), updates its badge from
"Preparing" to "Ready", and leaves it in place. The row now shows "Ready"
inside a page labeled "Preparing", creating a UI inconsistency.

The `new_order` handler takes the approach of reloading when any filter is
active (`if (hasFilter) { setTimeout(() => location.reload(), 2000); return; }`).
The `status_changed` handler has no equivalent. Whether the intended behavior
is to (a) update in place, (b) remove the row, or (c) reload the page when a
filter is active is not specified by the existing codebase pattern for this
event type. This is a possible UX gap — flagged but not blocking, because the
badge and dataset are correct after the update; the inconsistency is visual,
not a data integrity problem.

### Test coverage

The 8 new tests in `KitchenRealtimeTests` cover the full behavioral contract
for the backend path. The `on_commit` patch technique (replacing
`db_transaction.on_commit` with `side_effect=lambda fn: fn()`) correctly
causes the deferred callback to fire synchronously inside the test, making the
broker queue observable within the test's transaction wrapper.

`test_mark_ready_event_not_published_on_rollback` uses a no-op `on_commit`
patch combined with a `log_action` side effect to simulate a rollback. Because
Django's `TestCase` wraps the test in a non-committing savepoint, the no-op is
the correct simulation of what happens when `on_commit` callbacks are silently
dropped on rollback.

No automated tests cover the JS `status_changed` handler behavior — the verify
artifact notes this as a manual browser test. This is consistent with the rest
of the project (no Playwright/Selenium suite exists), so it is not a gap
relative to the existing standard.

</details>

---

<details>
<summary>Files changed</summary>

| File | What changed |
|------|-------------|
| `apps/realtime/signals.py` | Added `_skip_realtime` guard: early return before `publish()` if `instance._skip_realtime` is truthy |
| `apps/kitchen/views.py` | Set `order._skip_realtime = True` before `order.save()`; registered `transaction.on_commit(_broadcast)` to emit `status_changed` after commit; captured closure variables before atomic block exits |
| `templates/orders/order_list.html` | In `status_changed` handler: added `row.dataset.statusDisplay` and `row.dataset.nextStatus` updates; added `initResponsiveTables()` and `reinitLucide()` calls |
| `apps/kitchen/tests.py` | Added `KitchenRealtimeTests` class with 8 new tests covering event publication, no-duplicate guarantee, rollback safety, payload completeness, DB persistence, role enforcement, and invalid transitions |

Full diff: `git diff main` from `c:\Users\Shecile\kape_de_manubag_system`

</details>
