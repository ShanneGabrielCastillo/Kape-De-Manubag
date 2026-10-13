# Technical Design: Repair Security Tests After Payment-First Ordering Flow

## Overview

The repair touches exactly one file: `apps/orders/tests_security.py`. No production code changes are needed — the application is correct. The checkout view redirects successful orders to `orders:payment_waiting` (path `/orders/waiting/<tracking_token>/` — note the original request erroneously mentions `/orders/wait/<token>/`; the actual URL confirmed in `urls.py` is `waiting/`, not `wait/`). The view requires `payment_method` as a mandatory `CheckoutForm` field (`ChoiceField` with choices `cash`/`gcash`), and the idempotency-replay early-exit path also redirects to `orders:payment_waiting` when the duplicate order's status is `awaiting_payment`. Three module-level and class-level helpers in `tests_security.py` predate these changes and must be updated to match the current contract, along with two inline test assertion sites.

---

## Technology Stack (locked)

- **Language / Framework**: Python 3 / Django (existing project version, no upgrades)
- **Test framework**: Django's built-in `TestCase` (already in use)
- **Test runner**: `python manage.py test`
- **Only file modified**: `apps/orders/tests_security.py`

---

## Root Cause Analysis

Two helpers and two inline assertion sites are stale. There is no third category of failure.

**Cause A — `_post_checkout()` (module-level helper, line 46)**
Submits only `customer_name` and `order_type`. The live `CheckoutForm` (`apps/orders/forms.py`) also requires `payment_method`, a mandatory `ChoiceField` (choices: `cash`, `gcash`, initial `cash`; no `required=False`). Without it, `form.is_valid()` returns `False`, the view re-renders the checkout page with HTTP 200, and every caller that asserts `status_code == 302` fails immediately.

The fix propagates automatically to the two class-level `_place_order()` wrappers that call this helper:
- `OrderRateLimitTests._place_order()` calls `_post_checkout(self.client)` — inherits the fix
- `TrackingTokenTests._place_order()` (line 246) calls `_post_checkout(self.client)` — inherits the fix

One additional call site does **not** go through `_post_checkout()` at all: `test_idempotency_replay_does_not_consume_quota` constructs a raw `payload` dict and calls `self.client.post(reverse('orders:checkout'), payload)` directly (twice). This call site must receive `payment_method: 'cash'` explicitly in Change 3.

**Cause B — `_place_order_success()` (class method in `OrderRateLimitTests`, line 71)**
Even after fixing Cause A, this method asserts `assertIn('/orders/success/', resp['Location'])`. The current checkout view redirects to `/orders/waiting/<tracking_token>/`, not `/orders/success/`. This URL does not exist in the route table at all. The assertion must be updated to check `/orders/waiting/`.

**Cause C — Inline assertions in `test_idempotency_replay_does_not_consume_quota` (lines ~178–197)**
The test constructs its own `payload` dict without `payment_method` and POSTs directly. Same form-validation failure → HTTP 200 → accessing `resp1['Location']` raises `KeyError` → one `ERROR` result in the baseline. It also asserts `assertIn('/orders/success/', resp1['Location'])` and `assertIn('/orders/success/', resp2['Location'])`. Both the missing field and the stale URLs must be fixed. The second POST (replay) hits the early-exit `posted_token` duplicate check in `checkout_view` (before the cart check and before the rate-limit check), which redirects a `status='awaiting_payment'` duplicate to `orders:payment_waiting` — not the `IntegrityError` handler, which is a separate fallback for concurrent INSERT races.

**Cause D — Inline assertion in `test_two_sessions_are_tracked_independently` (line ~211)**
`client_b` uses `_post_checkout(client_b)` which routes through the module-level helper and will inherit the Cause A fix. The stale `assertIn('/orders/success/', resp_b['Location'])` must still be updated to `/orders/waiting/`.

**Cause E — No genuine application regression found**
The production code is correct. `checkout_view` correctly redirects to `orders:payment_waiting` on success, requires `payment_method`, and handles idempotency replays. No changes to production files are required.

---

## Design Decisions

### 1. Where to add `payment_method`

`payment_method='cash'` is added at the `_post_checkout()` module-level helper (line 46) as a new optional keyword argument with a default of `'cash'`. This is the correct default because: (a) `CheckoutForm` uses `initial='cash'`; (b) all passing tests in `tests.py` use `'cash'`; (c) the failing security tests are payment-method-agnostic. Adding it at the helper level fixes all 21 affected tests at a single point.

The two class-level `_place_order()` wrappers both call `_post_checkout()` without specifying `payment_method`. After Change 1, the updated default satisfies the form requirement. These two wrappers require no modification.

The inline `payload` dict in `test_idempotency_replay_does_not_consume_quota` bypasses `_post_checkout()` entirely via a raw `self.client.post()`. This call site must receive `'payment_method': 'cash'` added explicitly to the dict in Change 3.

### 2. What URL to assert in `_place_order_success()`

The checkout view redirects to `reverse('orders:payment_waiting', tracking_token=order.tracking_token)`. The path is `/orders/waiting/<43-char-token>/`. The tracking token varies per test run, so the assertion cannot be an exact match.

The chosen assertion is a two-part check:

```python
self.assertIn('/orders/waiting/', resp['Location'])
self.assertRegex(resp['Location'], r'/orders/waiting/[^/]+/')
```

The first assertion (`assertIn`) verifies the URL family. The second (`assertRegex`) ensures a non-empty token segment is present — ruling out a hypothetical malformed redirect to `/orders/waiting/` with no token. The regex `[^/]+` matches one or more non-slash characters (the tracking token), which is structurally correct regardless of token length.

Alternative considered and rejected: a single `assertIn('/orders/waiting/', resp['Location'])`. This is sufficient for the URL family check but would pass for a malformed empty-token redirect. The two-assertion approach closes this gap with minimal added complexity. `assertRegex` with `r'/orders/waiting/[^/]+/'` is not brittle to token-length changes — it matches any non-empty token — and matches the style of `tests.py`'s `assertRedirects` in spirit while avoiding the need to follow the redirect.

### 3. Idempotency replay test — raw POST and cart prerequisite

`test_idempotency_replay_does_not_consume_quota` calls `_make_cart(self.client, self.product)` before `self.client.get(reverse('orders:checkout'))`. The GET only renders context (including `request_token`) when the cart is non-empty; if the cart is empty the view redirects to `menu:index` and `page.context` is `None`, causing a `TypeError` on `page.context['request_token']`.

The fix (Change 3) adds `self.assertEqual(page.status_code, 200, "Checkout GET requires a non-empty cart")` immediately after the GET and before accessing `page.context['request_token']`. This surfaces the failure mode as a clear assertion error rather than an opaque `TypeError`.

The replay path (`resp2`) hits the early-exit `posted_token` duplicate check in `checkout_view` (before the cart check, before the rate-limit check, and before any form validation). The duplicate order has `status='awaiting_payment'` — never changed between the two POSTs — so the view redirects it to `orders:payment_waiting` with the same tracking token as `resp1`. Both `resp1` and `resp2` therefore return `/orders/waiting/<same_token>/`, which is correct and confirms the idempotency invariant.

### 4. `_place_order_success()` docstring and failure-mode commentary

The updated helper docstring will document the three meaningful failure modes to satisfy the NFR readability requirement:

- `status_code == 200`: the order was rate-limited (render with checkout page)
- 302 to `/orders/cart/`: a stock adjustment was triggered pre-checkout (cart review required)
- 302 to `/menu/`: the cart was empty at POST time

This makes test failure messages self-explanatory for future maintainers.

### 5. `_place_order_success()` return value

The method currently returns `resp`. This is preserved — some callers may inspect it, and no extra work is needed to keep it.

### 6. No class-level `_place_order()` changes

`OrderRateLimitTests._place_order()` and `TrackingTokenTests._place_order()` are left completely unmodified. Both call `_post_checkout()` without a `payment_method` argument; after Change 1 the helper's new default satisfies the form requirement automatically.

### 7. No `assertRedirects` upgrade

`assertRedirects()` follows the redirect and checks the target URL, which would require the `payment_waiting` view to render without error. Using it would add complexity (the view needs order data that may not be trivially available in every helper context). The two-assertion approach in Decision §2 is sufficient for all purposes and matches the existing assertion style in this file.

---

## Specific Changes

### File: `apps/orders/tests_security.py`

#### Change 1 — `_post_checkout()` module-level helper (line 46)

```python
# BEFORE
def _post_checkout(client, customer_name='Test Customer', order_type='dine_in', token=None):
    """Submit a checkout POST.  Does NOT follow redirects."""
    payload = {'customer_name': customer_name, 'order_type': order_type}
    if token:
        payload['request_token'] = token
    return client.post(reverse('orders:checkout'), payload)

# AFTER
def _post_checkout(client, customer_name='Test Customer', order_type='dine_in',
                   token=None, payment_method='cash'):
    """Submit a checkout POST.  Does NOT follow redirects.

    payment_method defaults to 'cash' to satisfy the mandatory CheckoutForm
    ChoiceField added with the payment-first ordering flow.  Individual tests
    that exercise GCash paths can pass payment_method='gcash' explicitly.
    """
    payload = {
        'customer_name': customer_name,
        'order_type': order_type,
        'payment_method': payment_method,  # required by CheckoutForm (payment-first flow)
    }
    if token:
        payload['request_token'] = token
    return client.post(reverse('orders:checkout'), payload)
```

#### No-change note — `OrderRateLimitTests._place_order()` and `TrackingTokenTests._place_order()`

Both wrappers call `_post_checkout(self.client)` without specifying `payment_method`. After Change 1 the updated default satisfies the form requirement. **No modification required to either wrapper.**

#### Change 2 — `OrderRateLimitTests._place_order_success()` (line 71)

```python
# BEFORE
def _place_order_success(self):
    """Place an order and assert it succeeded (redirects to success page)."""
    resp = self._place_order()
    # On success the view redirects to order_success
    self.assertEqual(resp.status_code, 302, "Expected redirect after order")
    self.assertIn('/orders/success/', resp['Location'])
    return resp

# AFTER
def _place_order_success(self):
    """Place an order and assert it succeeded (redirects to payment-waiting page).

    The payment-first checkout flow redirects to /orders/waiting/<token>/
    (orders:payment_waiting) on success.

    Failure modes:
      - status 200:              order was rate-limited (checkout page re-rendered)
      - 302 to /orders/cart/:    stock adjustment triggered; customer must review cart
      - 302 to /menu/:           cart was empty at POST time
    """
    resp = self._place_order()
    self.assertEqual(resp.status_code, 302, "Expected redirect after order")
    # Payment-first flow: successful checkout redirects to payment-waiting page.
    self.assertIn('/orders/waiting/', resp['Location'])
    self.assertRegex(resp['Location'], r'/orders/waiting/[^/]+/',
                     "Redirect must include a non-empty tracking token")
    return resp
```

#### Change 3 — `test_idempotency_replay_does_not_consume_quota` inline payload and assertions

This test bypasses `_post_checkout()` and constructs a raw `payload` dict. Changes needed: (a) add `'payment_method': 'cash'` to the payload, (b) add a `status_code == 200` assertion after the checkout GET (cart-prerequisite guard), (c) update both `assertIn('/orders/success/', ...)` calls to the current URL.

```python
# BEFORE
_make_cart(self.client, self.product)
page = self.client.get(reverse('orders:checkout'))
token = page.context['request_token']

payload = {'customer_name': 'Test', 'order_type': 'dine_in',
           'request_token': token}
# First real submission
resp1 = self.client.post(reverse('orders:checkout'), payload)
self.assertIn('/orders/success/', resp1['Location'])
self.assertEqual(Order.objects.count(), 1)

# Replay (same token) — server returns original order, rate counter
# must not increment again for the replay.
# Seed a new cart item so cart is not empty, then replay old token
_make_cart(self.client, self.product)
resp2 = self.client.post(reverse('orders:checkout'), payload)
self.assertIn('/orders/success/', resp2['Location'])
# Still only ONE order
self.assertEqual(Order.objects.count(), 1)

# Session timestamp list should have only 1 entry (not 2)
session = self.client.session
timestamps = session.get(_ORDER_RATE_SESSION_KEY, [])
self.assertEqual(len(timestamps), 1,
                 "Idempotency replay must not add a second timestamp")

# AFTER
_make_cart(self.client, self.product)
page = self.client.get(reverse('orders:checkout'))
self.assertEqual(page.status_code, 200,
                 "Checkout GET requires a non-empty cart to render context")
token = page.context['request_token']

payload = {'customer_name': 'Test', 'order_type': 'dine_in',
           'request_token': token,
           'payment_method': 'cash'}  # required by CheckoutForm (payment-first flow)
# First real submission
resp1 = self.client.post(reverse('orders:checkout'), payload)
self.assertIn('/orders/waiting/', resp1['Location'])  # payment-first: payment-waiting page
self.assertEqual(Order.objects.count(), 1)

# Replay (same token) — server returns original order via the early-exit
# posted_token duplicate check (before cart check and rate-limit check).
# Rate counter must not increment again for the replay.
# Seed a new cart item so cart is not empty, then replay old token
_make_cart(self.client, self.product)
resp2 = self.client.post(reverse('orders:checkout'), payload)
self.assertIn('/orders/waiting/', resp2['Location'])  # idempotency replay also → waiting page
# Still only ONE order
self.assertEqual(Order.objects.count(), 1)

# Session timestamp list should have only 1 entry (not 2)
session = self.client.session
timestamps = session.get(_ORDER_RATE_SESSION_KEY, [])
self.assertEqual(len(timestamps), 1,
                 "Idempotency replay must not add a second timestamp")
```

All other assertions in the test (order count, timestamp length) are unchanged.

#### Change 4 — `test_two_sessions_are_tracked_independently` inline assertion

`client_b` calls `_post_checkout(client_b)` which routes through the module-level helper. After Change 1 it will include `payment_method='cash'` automatically. Only the stale URL literal needs updating:

```python
# BEFORE
self.assertIn('/orders/success/', resp_b['Location'])

# AFTER
self.assertIn('/orders/waiting/', resp_b['Location'])  # payment-first flow
```

The `resp_b.status_code == 302` assertion immediately preceding this line is unchanged — it is the core security invariant (Session B must not be blocked by Session A's quota).

---

## Files Changed

| File | Change |
|------|--------|
| `apps/orders/tests_security.py` | Only file modified — 4 targeted changes as detailed above |

No other files are modified.

---

## Edge Cases

**Empty cart guard in idempotency test**: The checkout GET only renders context (including `request_token`) when the cart is non-empty. If `_make_cart()` fails silently, the view redirects to `menu:index` and `page.context` is `None`, producing a `TypeError` on `page.context['request_token']`. Change 3 adds `self.assertEqual(page.status_code, 200, ...)` before the context access. Any silent cart-seeding failure now surfaces as a clear assertion error.

**Empty cart guard in `_place_order_success()`**: The helper calls `_place_order()`, which calls `_make_cart()` before `_post_checkout()`. If the cart is somehow empty at POST time, the checkout view redirects to `menu:index` (not `/orders/waiting/`). The `assertEqual(302)` passes but `assertIn('/orders/waiting/', ...)` fails, clearly signalling the cart-empty condition. The updated docstring explains this failure mode.

**Idempotency replay redirect path**: `resp2` (the replay POST with the same `request_token`) hits the **early-exit `posted_token` duplicate check** at the top of `checkout_view` — before the empty-cart check, before form validation, and before the rate-limit check. The duplicate order has `status='awaiting_payment'` (it was just created by `resp1` and never paid), so the view returns `redirect('orders:payment_waiting', tracking_token=duplicate.tracking_token)`. Both `resp1` and `resp2` therefore redirect to the same `/orders/waiting/<token>/` URL. This is distinct from the `IntegrityError` fallback handler, which only fires on a concurrent INSERT race where two requests escape the duplicate check simultaneously.

**Rate-limit window in `test_order_allowed_after_window_expires`**: This test manually backdates session timestamps and then calls `_place_order_success()` a fourth time. After the helper fix, the fourth call POSTs with `payment_method='cash'`, gets a valid 302 to `/orders/waiting/`, and the `assertRegex` check passes. The test's final `assertEqual(Order.objects.count(), 4)` is unaffected.

**`_ORDER_RATE_SESSION_KEY` import**: Already imported at module level in `tests_security.py` (`from apps.orders.views import _ORDER_RATE_SESSION_KEY`). No change needed.

**Token-length assertion**: `test_tracking_token_is_url_safe_length` asserts `len(order.tracking_token) == 43`. `secrets.token_urlsafe(32)` produces exactly 43 URL-safe base64 characters. The `assertRegex(r'/orders/waiting/[^/]+/')` in Change 2 does not constrain token length — `[^/]+` matches any non-empty non-slash sequence, including 43-character tokens.

**Session isolation**: `_post_checkout()` is a module-level function that takes `client` as its first argument. `test_two_sessions_are_tracked_independently` passes `client_b` directly, so `client_b`'s POST is correctly isolated from `self.client`'s session and rate-limit counter. The `payment_method` addition does not affect this isolation.

---

## Error Handling

All changes are to test code only. The relevant failure modes in the tests are:

- **`_place_order_success()` with a rate-limited client**: `assertEqual(302)` fails with `200 != 302`, correctly surfacing the unexpected block. The updated docstring documents this.
- **`_place_order_success()` with an empty cart**: 302 to `/menu/index`. `assertIn('/orders/waiting/', ...)` fails, clearly surfacing the cart-empty condition. Documented in the helper docstring.
- **`_place_order_success()` with a stock-adjustment bounce**: 302 to `/orders/cart/`. Same failure path as above, documented in the helper docstring.
- **`_post_checkout()` with an invalid `payment_method` value**: `CheckoutForm` is a `ChoiceField`; an invalid value makes `form.is_valid()` return `False` and the view returns 200. Any test that passes a bad value will fail at the redirect assertion. The default `'cash'` is always valid.
- **`KeyError` on `resp['Location']`**: Occurred in the baseline because the form was invalid (no `payment_method`) and the response had no `Location` header. After the fix, the form is valid and `resp['Location']` is always set on the success path. The `KeyError` is eliminated.
- **`TypeError` on `page.context['request_token']`**: Occurred when the checkout GET returned a redirect (empty-cart case). The added `assertEqual(page.status_code, 200)` guard turns this into a clear assertion failure with an explanatory message.

---

## Testability

All changes are entirely within the test file. The production code interface is already clean (form POST, redirect response). The four changes are minimal, isolated, and each maps directly to one root cause identified above.

After the changes, `python manage.py test apps/orders.tests_security` exercises the real checkout view, real form validation, real session-based rate limiting, and real `Order` model creation. No mocking or patching is needed for any of the 21 affected tests.

---

## Validation Plan

Run in this order after implementing all four changes:

1. `python manage.py test apps.orders.tests_security --verbosity=2` — must report **23 passed, 0 failed, 0 errors**
2. `python manage.py test apps.orders --verbosity=2` — must keep all CustomerReceiptTests (19) and PaidAtTimestampTests (12) passing; zero regressions in any other test class
3. `python manage.py check` — must report 0 issues

---

## Responses to Design Review Findings

**Finding #1 (HIGH) — Design Decision §1 contradicts itself on inherited vs. explicit `payment_method` sites**
Addressed. Design Decision §1 now explicitly states that the two class-level wrappers inherit the fix, and separately identifies `test_idempotency_replay_does_not_consume_quota`'s raw `self.client.post()` as the one call site that requires an explicit addition (Change 3). The "No-change note" for both wrappers is added to the Specific Changes section.

**Finding #2 (HIGH) — No cart-prerequisite guard before `page.context['request_token']`**
Addressed. Change 3 now includes `self.assertEqual(page.status_code, 200, "Checkout GET requires a non-empty cart to render context")` before the `page.context['request_token']` access, and the Edge Cases section documents the empty-cart failure mode.

**Finding #3 (MEDIUM) — Line numbers off; early-exit path vs. IntegrityError handler not distinguished**
Addressed. Approximate line numbers have been removed from all root-cause descriptions. The Cause C analysis now explicitly names "the early-exit `posted_token` duplicate check" and distinguishes it from the `IntegrityError` fallback. The same distinction is made in Edge Cases §Idempotency replay redirect path.

**Finding #4 (MEDIUM) — `assertIn` alone would pass for a token-less redirect**
Addressed. `_place_order_success()` now uses a two-assertion approach: `assertIn('/orders/waiting/', resp['Location'])` for URL-family membership, followed by `assertRegex(resp['Location'], r'/orders/waiting/[^/]+/')` to require a non-empty token segment. Design Decision §2 documents the reasoning and the rejection of single-assertion approach.

**Finding #5 (MEDIUM) — Error handling does not explain non-rate-limited failure modes**
Addressed. The updated `_place_order_success()` docstring documents all three failure modes (rate-limited 200, cart-empty redirect, stock-adjustment redirect). The Error Handling section now covers all four failure modes.

**Finding #6 (NIT) — Original request's `/orders/wait/` vs. actual `/orders/waiting/` not flagged**
Addressed. The Overview paragraph now contains a parenthetical noting the discrepancy.

**Finding #7 (NIT) — Approximate line numbers cited for `_post_checkout()` and `_place_order_success()`**
Addressed. Approximate line numbers are now removed from narrative text and replaced with exact line numbers (46 and 71) only where they are confirmed by the source read. Line-number references in the Root Cause Analysis section have been removed where they could become stale.

**Finding #8 (NIT) — No explicit "No Change" blocks for the two `_place_order()` wrappers**
Addressed. An explicit "No-change note" block has been added to the Specific Changes section after Change 1.
