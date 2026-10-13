# Implementation Plan: Repair Security Tests After Payment-First Ordering Flow

**Only file modified:** `apps/orders/tests_security.py`

All four changes are isolated within the test file. Production code is correct — no production files are touched. Each item leaves the file in a buildable, runnable state.

---

- [x] 1. Add `payment_method` parameter to `_post_checkout()` module-level helper.

      The module-level `_post_checkout()` function (starting around line 46) builds a `payload`
      dict that omits `payment_method`. `CheckoutForm` treats `payment_method` as a required
      `ChoiceField` (choices: `cash`/`gcash`). Without it `form.is_valid()` returns `False`,
      the view re-renders with HTTP 200, and every caller that asserts `status_code == 302` fails.

      Add `payment_method='cash'` as a new optional keyword argument and include it in the
      `payload` dict. Update the docstring to document the parameter and why it defaults to
      `'cash'` (matches `CheckoutForm`'s `initial='cash'` and all passing tests in `tests.py`).

      The signature after the change:
      ```python
      def _post_checkout(client, customer_name='Test Customer', order_type='dine_in',
                         token=None, payment_method='cash'):
      ```
      The payload after the change must include:
      ```python
      payload = {
          'customer_name': customer_name,
          'order_type': order_type,
          'payment_method': payment_method,  # required by CheckoutForm (payment-first flow)
      }
      ```
      Do NOT modify either `_place_order()` wrapper (`OrderRateLimitTests._place_order()` or
      `TrackingTokenTests._place_order()`); both call `_post_checkout(self.client)` without
      specifying `payment_method` and will automatically inherit the corrected default.

      Files: `apps/orders/tests_security.py`

      Verify: `python manage.py test apps.orders.tests_security --verbosity=2`
      After this change alone, tests whose only failure cause was the missing field will begin
      passing. The `_place_order_success()` helper will still fail (stale URL assertion) — that
      is expected and fixed in item 2.

---

- [x] 2. Update `_place_order_success()` to assert the current payment-waiting redirect URL.

      `OrderRateLimitTests._place_order_success()` asserts
      `assertIn('/orders/success/', resp['Location'])`. The route `/orders/success/` does not
      exist in the URL table. The checkout view redirects successfully placed orders to
      `orders:payment_waiting` (`/orders/waiting/<tracking_token>/`). The stale assertion
      causes every test that calls `_place_order_success()` to fail.

      Replace both assertions inside the method:
      - Keep `self.assertEqual(resp.status_code, 302, "Expected redirect after order")` unchanged.
      - Replace `self.assertIn('/orders/success/', resp['Location'])` with two assertions:
        ```python
        self.assertIn('/orders/waiting/', resp['Location'])
        self.assertRegex(resp['Location'], r'/orders/waiting/[^/]+/',
                         "Redirect must include a non-empty tracking token")
        ```
      Replace the docstring to describe the current flow and document all three failure modes:
      - status 200: order was rate-limited (checkout page re-rendered)
      - 302 to `/orders/cart/`: stock adjustment triggered; customer must review cart
      - 302 to `/menu/`: cart was empty at POST time

      Files: `apps/orders/tests_security.py`

      Verify: `python manage.py test apps.orders.tests_security.OrderRateLimitTests --verbosity=2`
      All `OrderRateLimitTests` (except `test_two_sessions_are_tracked_independently` and
      `test_idempotency_replay_does_not_consume_quota`, fixed in items 3–4) must pass.

---

- [x] 3. Fix `test_idempotency_replay_does_not_consume_quota` — add `payment_method` to raw payload and add cart-prerequisite guard.

      This test bypasses `_post_checkout()` entirely and POSTs a raw `payload` dict directly via
      `self.client.post(reverse('orders:checkout'), payload)`. It therefore does not inherit the
      fix from item 1. Two problems:

      a) The `payload` dict lacks `'payment_method'`: `CheckoutForm` rejects it (HTTP 200),
         `resp1['Location']` raises `KeyError`, and the test records as an ERROR (not FAIL).
      b) Both `assertIn('/orders/success/', resp1['Location'])` and
         `assertIn('/orders/success/', resp2['Location'])` use the stale URL.

      Make these changes inside `test_idempotency_replay_does_not_consume_quota`:

      1. After `page = self.client.get(reverse('orders:checkout'))`, add:
         ```python
         self.assertEqual(page.status_code, 200,
                          "Checkout GET requires a non-empty cart to render context")
         ```
         This guard turns the opaque `TypeError` (when cart is empty and `page.context` is `None`)
         into a clear assertion failure.

      2. Add `'payment_method': 'cash'` to the `payload` dict:
         ```python
         payload = {'customer_name': 'Test', 'order_type': 'dine_in',
                    'request_token': token,
                    'payment_method': 'cash'}  # required by CheckoutForm (payment-first flow)
         ```

      3. Replace `self.assertIn('/orders/success/', resp1['Location'])` with:
         ```python
         self.assertIn('/orders/waiting/', resp1['Location'])  # payment-first: payment-waiting page
         ```

      4. Replace `self.assertIn('/orders/success/', resp2['Location'])` with:
         ```python
         self.assertIn('/orders/waiting/', resp2['Location'])  # idempotency replay also → waiting page
         ```

      All other assertions (order count, timestamp length) are unchanged.

      Note on the replay path: `resp2` (replay with same `request_token`) hits the early-exit
      `posted_token` duplicate check at the top of `checkout_view` — before the empty-cart
      check, before form validation, and before the rate-limit check. The duplicate order has
      `status='awaiting_payment'` so the view redirects to `orders:payment_waiting` with the
      same `tracking_token` as `resp1`. This is distinct from the `IntegrityError` fallback
      handler, which only fires on a concurrent INSERT race.

      Files: `apps/orders/tests_security.py`

      Verify: `python manage.py test apps.orders.tests_security.OrderRateLimitTests.test_idempotency_replay_does_not_consume_quota --verbosity=2`
      Must report 1 test passed, 0 failed, 0 errors.

---

- [x] 4. Fix stale URL assertion in `test_two_sessions_are_tracked_independently`.

      At the end of this test, `client_b` calls `_post_checkout(client_b)` (which routes through
      the module-level helper and inherits `payment_method='cash'` from item 1), then the test
      asserts `assertIn('/orders/success/', resp_b['Location'])`. This URL is stale.

      Replace only that one assertion:
      ```python
      # BEFORE
      self.assertIn('/orders/success/', resp_b['Location'])

      # AFTER
      self.assertIn('/orders/waiting/', resp_b['Location'])  # payment-first flow
      ```
      The `assertEqual(resp_b.status_code, 302)` immediately before it is the core security
      invariant (Session B must not be blocked by Session A's quota) and is unchanged.

      Files: `apps/orders/tests_security.py`

      Verify: `python manage.py test apps.orders.tests_security.OrderRateLimitTests.test_two_sessions_are_tracked_independently --verbosity=2`
      Must report 1 test passed, 0 failed, 0 errors.

---

- [x] 5. Run full validation suite and confirm baseline is restored.

      Run the three mandatory validation commands in order:

      1. Full security test class:
         ```
         python manage.py test apps.orders.tests_security --verbosity=2
         ```
         Expected: **23 passed, 0 failed, 0 errors**

      2. Full orders app tests (covers CustomerReceiptTests × 19, PaidAtTimestampTests × 12,
         and all other order/payment tests):
         ```
         python manage.py test apps.orders --verbosity=2
         ```
         Expected: All CustomerReceiptTests (19) and PaidAtTimestampTests (12) still passing;
         zero regressions across any other test class in the app.

      3. System check:
         ```
         python manage.py check
         ```
         Expected: **System check identified no issues (0 silenced).**

      Record the exact counts from the test runner output. If any test fails or errors that
      was not failing before these changes, diagnose and fix the regression before completing.

      Files: (read-only validation step — no file changes)

      Verify: Test runner output confirms the pass counts above.
