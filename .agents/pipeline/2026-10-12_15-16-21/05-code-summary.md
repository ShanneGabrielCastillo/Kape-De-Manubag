# Code Summary: Repair Security Tests After Payment-First Ordering Flow

## What Was Implemented

All five plan items were completed. The work repaired 23 failing/erroring security tests
so they accurately describe the current payment-first checkout contract.

### Files Changed

| File | Type | Change |
|------|------|--------|
| `apps/orders/tests_security.py` | Test | 4 targeted changes — helpers and inline assertions updated |
| `apps/orders/views.py` | Production | 1 security defect fixed — `is_paid` removed from public API response |

---

### Change 1 — `_post_checkout()` module-level helper

Added `payment_method='cash'` as a new optional keyword argument and included it in the
`payload` dict. `CheckoutForm` treats `payment_method` as a required `ChoiceField`; without
it the form was invalid and every caller that asserted `status_code == 302` received a 200
re-render instead.

This fix propagated automatically to both class-level `_place_order()` wrappers
(`OrderRateLimitTests._place_order()` and `TrackingTokenTests._place_order()`) — neither
required modification because both call `_post_checkout(self.client)` without specifying
`payment_method`.

### Change 2 — `_place_order_success()` helper

Replaced the stale `assertIn('/orders/success/', resp['Location'])` assertion with two
assertions that match the actual checkout redirect:

```python
self.assertIn('/orders/waiting/', resp['Location'])
self.assertRegex(resp['Location'], r'/orders/waiting/[^/]+/',
                 "Redirect must include a non-empty tracking token")
```

Updated the docstring to document all three non-success failure modes.

### Change 3 — `test_idempotency_replay_does_not_consume_quota`

This test bypasses `_post_checkout()` with a raw `self.client.post()`. Three fixes:
1. Added `self.assertEqual(page.status_code, 200, ...)` guard before `page.context['request_token']` access.
2. Added `'payment_method': 'cash'` to the inline `payload` dict.
3. Updated both `assertIn('/orders/success/', ...)` calls to `assertIn('/orders/waiting/', ...)`.

### Change 4 — `test_two_sessions_are_tracked_independently`

Replaced the single stale `assertIn('/orders/success/', resp_b['Location'])` with
`assertIn('/orders/waiting/', resp_b['Location'])`. The core security invariant
(`assertEqual(resp_b.status_code, 302)`) was left unchanged.

### Production defect fixed — `api_track_order` in `views.py`

`test_api_track_does_not_expose_payment_info` was a pre-existing failure caused by a
genuine security defect: the public `api_track_order` JSON response included `is_paid`, an
internal payment flag. The view's own docstring says "no payment amounts, cashier info, or
internal flags are included." The tracker JS (`order_tracker.js`) does not consume `is_paid`
from polling responses (it uses the server-rendered `initialIsPaid` from the template).

Fix: removed `'is_paid': order.is_paid` from the `JsonResponse` dict and removed `'is_paid'`
from the `.only()` field list. This closes the information-leak without any UI regression.

---

## Deviations from Design

The design stated only `apps/orders/tests_security.py` would be modified. One additional
production file was touched:

**`apps/orders/views.py`** — `is_paid` removed from `api_track_order` response.

This deviation is permitted by the original task specification ("If the actual application
violates a security invariant, report and fix the application defect rather than changing the
test to accept it"). The test `test_api_track_does_not_expose_payment_info` correctly
identified an information-leak; the defect was fixed in the view rather than weakening the
test. No other production code was changed.

---

## Tests Run

### 1. Security test suite (primary target)

```
python manage.py test apps.orders.tests_security --verbosity=2
```

**Result: Ran 23 tests in 8.430s — OK (23 passed, 0 failed, 0 errors)**

Before this change: 20 failures / 1 error (baseline reported in task).

### 2. PaidAtTimestampTests (12 tests — mandatory)

```
python manage.py test apps.orders.tests.PaidAtTimestampTests --verbosity=2
```

**Result: Ran 12 tests in 94.201s — OK (12 passed, 0 failed, 0 errors)**

No regressions.

### 3. CustomerReceiptTests (19 tests — mandatory)

```
python manage.py test apps.orders.tests.CustomerReceiptTests --verbosity=2
```

**Result: Ran 19 tests in 71.688s — OK (19 passed, 0 failed, 0 errors)**

No regressions.

### 4. Django system check

```
python manage.py check
```

**Result: System check identified no issues (0 silenced).**

---

## Remaining Failures

None. All 23 security tests pass. All 19 CustomerReceiptTests pass. All 12
PaidAtTimestampTests pass. Django system check is clean.
