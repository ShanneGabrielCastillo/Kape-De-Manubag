# Design Review: Repair Security Tests After Payment-First Ordering Flow

**Document reviewed:** `02-design.md`
**Requirements reviewed:** `01-requirements.md`
**Source verified against:** `apps/orders/tests_security.py`, `apps/orders/views.py`, `apps/orders/forms.py`, `apps/orders/urls.py`, `apps/orders/models.py`
**Verdict:** APPROVED

---

## Numbered Findings

### 1. NIT — Root Cause labeling is internally inconsistent

**Where:** Design §Root Cause Analysis, opening paragraph vs. the cause list below it.

**Problem:** The opening sentence says "Two helpers and two inline assertion sites are stale. There is no third category of failure." But the section then lists five labeled causes: A, B, C, D, and E. Cause E is "no regression," which is not a failure cause at all. A future reader will wonder why the opening says "two helpers and two inline sites" when five causes are labeled.

**Concrete fix:** Change the opening sentence to:

> "Four causes of failure are identified (A–D). Cause E confirms there is no application regression."

Or, equivalently, relabel E as a "Finding: No Regression" paragraph outside the lettered list.

---

### 2. NIT — Design Decision §1 describes FR-2 only transitively, not explicitly

**Where:** Design §Design Decisions §1, and §Specific Changes "No-change note" block.

**Problem:** Requirements FR-2 says "`OrderRateLimitTests._place_order()` must delegate to the corrected `_post_checkout()`." The design satisfies this correctly — the wrapper already calls `_post_checkout()` without a `payment_method` argument, and Change 1's new default satisfies the form. However, the design never explicitly states this. An implementer could mistakenly think FR-2 requires a direct code change to the wrapper. The "No-change note" block in Specific Changes partially addresses this but is buried after the Change 1 code snippet.

**Concrete fix:** In Design Decision §1, add one sentence before "The two class-level `_place_order()` wrappers…":

> "FR-2 is satisfied transitively: `OrderRateLimitTests._place_order()` already calls `_post_checkout(self.client)`, so the updated default in Change 1 propagates automatically with no modification to the wrapper itself."

---

### 3. NIT — `_make_cart()` call before `resp2` in idempotency test is described as a prerequisite when it is not

**Where:** Design §Specific Changes §Change 3, and §Edge Cases §Idempotency replay redirect path.

**Problem:** The design states "Seed a new cart item so cart is not empty, then replay old token" in the Change 3 comment. The comment implies the cart must be non-empty for the second POST to succeed. However, based on the verified `checkout_view` code, the `posted_token` duplicate-order check runs **before** the empty-cart check. The second POST hits the early-exit branch and returns immediately, so the cart state is irrelevant for `resp2`.

The existing test comment "Seed a new cart item so cart is not empty, then replay old token" is technically misleading. The design's Edge Cases section does acknowledge this ("The replay path...hits the early-exit `posted_token` duplicate check...before the empty-cart check") but the Change 3 comment in the code block still retains the misleading text verbatim from the original.

**Concrete fix:** Update the inline comment in the Change 3 "AFTER" code block to:

```python
# Replay (same token) — server returns original order via the early-exit
# posted_token duplicate check (runs BEFORE the empty-cart check, so cart
# state is irrelevant for the replay).  Rate counter must not increment.
_make_cart(self.client, self.product)
```

Or drop the `_make_cart` call entirely from the AFTER snippet and note it as "kept from original for minimal diff, harmless."

---

### 4. NIT — Validation Plan states "23 passed" but does not account for the possibility of skips or unexpected new failures in other test classes

**Where:** Design §Validation Plan, item 2.

**Problem:** The Validation Plan step 2 says "must keep all CustomerReceiptTests (19) and PaidAtTimestampTests (12) passing; zero regressions in any other test class." This is correct in intent, but item 1 only specifies `apps.orders.tests_security` and gives the exact expected count "23 passed, 0 failed, 0 errors." Item 2 does not give exact counts for CustomerReceiptTests and PaidAtTimestampTests, making it harder to verify mechanically.

This is consistent with the requirements (which list exact counts for those tests), but a minor gap between the design and the requirements AC.

**Concrete fix:** Update item 2 to:

> "`python manage.py test apps.orders --verbosity=2` — must report CustomerReceiptTests: **19 passed**, PaidAtTimestampTests: **12 passed**, zero failures or errors across all `apps.orders` test classes."

---

## Verified Assumptions

The following design claims were verified against actual source:

1. **`_post_checkout` is at line 46** — Confirmed. The function definition is at line 46 in `tests_security.py`.

2. **`_place_order_success` is at line 71** — Confirmed. The method definition is at line 71 in `tests_security.py`.

3. **`TrackingTokenTests._place_order()` is at line 246** — Confirmed.

4. **Total test count is 23** — Confirmed. `grep "def test_"` finds exactly 23 test methods across both test classes.

5. **`CheckoutForm` has `payment_method` as a required `ChoiceField` with choices `cash`/`gcash`** — Confirmed in `apps/orders/forms.py`. The field has no `required=False`, so it is mandatory. Choices are `Order.PAYMENT_METHOD_CHOICES = [('cash', 'Cash'), ('gcash', 'GCash')]`.

6. **`CheckoutForm` initial value for `payment_method` is `'cash'`** — Confirmed (`initial='cash'` in the field definition).

7. **The checkout view redirects successful orders to `orders:payment_waiting` with URL path `/orders/waiting/<tracking_token>/`** — Confirmed in `apps/orders/urls.py` (`path('waiting/<str:tracking_token>/', views.payment_waiting, name='payment_waiting')`) and in `views.py` final success line (`return redirect('orders:payment_waiting', tracking_token=order.tracking_token)`).

8. **The URL is `/orders/waiting/` not `/orders/wait/`** — Confirmed. `urls.py` uses `waiting/`, not `wait/`. The design's overview note about the original request's erroneous `/orders/wait/` is accurate.

9. **`_ORDER_RATE_SESSION_KEY = 'order_rate_timestamps'` is defined in `views.py` and importable** — Confirmed at line 43 of `views.py`. `tests_security.py` already imports it at line 24 (`from apps.orders.views import _ORDER_RATE_SESSION_KEY`).

10. **Idempotency replay (second POST with same `request_token`) hits the early-exit `posted_token` duplicate check before the empty-cart check and before form validation** — Confirmed by reading `checkout_view` in `views.py`. The `posted_token` check (lines 303–315) is before `if not items: return redirect('menu:index')` (line 319). The duplicate's status is `awaiting_payment` at replay time (never changed between the two POSTs), so the view redirects to `orders:payment_waiting`.

11. **`TrackingTokenTests._place_order()` only asserts `resp.status_code == 302` and not a specific URL** — Confirmed at lines 246–251. No `assertIn` on `resp['Location']`. Change 1 alone fixes the 12 `TrackingTokenTests` failures.

12. **`test_two_sessions_are_tracked_independently` calls the module-level `_post_checkout(client_b)` directly (not through `_place_order_success`)** — Confirmed at line 212. After Change 1, `client_b`'s POST includes `payment_method='cash'` automatically. The stale URL assertion at line 213 still requires Change 4.

13. **`test_failed_checkout_does_not_consume_quota` is not affected by the missing `payment_method`** — Confirmed. The test posts to an empty cart, which redirects to `menu:index` before form validation is reached. The form's missing `payment_method` is never evaluated on this path.

14. **The two `_place_order()` class-level wrappers call `_post_checkout()` without specifying `payment_method`** — Confirmed. `OrderRateLimitTests._place_order()` (line 67–70) calls `_post_checkout(self.client)`, and `TrackingTokenTests._place_order()` (line 246–251) calls `_post_checkout(self.client)`. Both inherit the Change 1 default.

15. **Only `apps/orders/tests_security.py` needs modification** — Confirmed. The application code is correct: the checkout view properly requires `payment_method`, redirects to `payment_waiting`, and handles idempotency replays. No production file defects were found.

16. **`secrets.token_urlsafe(32)` produces 43 characters** — Consistent with design claim. The `assertRegex(r'/orders/waiting/[^/]+/')` pattern is length-agnostic.

17. **The `_ORDER_RATE_SESSION_KEY` stores a plain list of epoch floats** (not a nested dict) — Confirmed in `views.py`. `_record_order_created` appends a float to a list and stores it as `request.session[_ORDER_RATE_SESSION_KEY] = timestamps`. The design's description of "list of successful order creation times" is accurate. (An earlier design iteration erroneously described it as `{'timestamps': [...]}` — the current design correctly describes it as a plain list.)

---

## Unverified / Wrong Assumptions

None found. All design claims that could be checked against source code were verified and found to be accurate. The four root causes (A–D) are correctly identified, correctly scoped to `tests_security.py` only, and the proposed four changes are individually correct and complete.

---

## Summary

The design is technically sound and accurately reflects the codebase. All four changes are correctly targeted, the root cause analysis is accurate, the URL path `/orders/waiting/` is confirmed, the form field requirements are confirmed, and the idempotency replay behavior is confirmed. The only findings are NITs related to internal documentation clarity — a misleading code comment, an inconsistent cause-count claim, a missing explicit FR-2 traceability note, and a minor gap in the Validation Plan's expected counts. None of these block implementation.

**Verdict: APPROVED** (0 HIGH, 0 MEDIUM, 4 NIT findings)
