# Implementation Plan: Add Optional Order Notes to POS

## Findings from Codebase Exploration

### Order Model (`apps/orders/models.py`, line ~48)
- Field: `notes = models.TextField(blank=True)` — no `max_length`, optional, already exists.
- **No new database field or migration needed.**

### Customer-Facing Checkout
- Form: `apps/orders/forms.py` — `CheckoutForm` has `notes = forms.CharField(required=False, widget=forms.Textarea(...))`
- View: `apps/orders/views.py` — `checkout_view` saves `notes=form.cleaned_data.get('notes', '')` on `Order.objects.create()`
- Submission: Standard HTML form POST to `orders:checkout`

### POS Interface
- Template: `templates/orders/pos.html`
- The `cashier_pos` view (line ~1555 in views.py) renders `templates/orders/pos.html`
- POS order creation endpoint: `create_pos_order` view (POST, JSON body)
  - **Already reads `notes=data.get('notes', '')`** at `Order.objects.create()` — **the backend already supports notes**
  - Submission: JavaScript `fetch()` POST with `Content-Type: application/json` — `body: JSON.stringify({...items, customer_name, order_type, payment_method, request_token})`
  - The `notes` field is **missing** from the JSON payload built in `static/js/main.js` `submitOrder()` (line ~1259)
- The `pos-checkout-controls` div in pos.html (line ~248–270) has: customer name input, order type radios, payment method radios — **no notes textarea**

### Kitchen Orders Display (`templates/kitchen/orders.html`)
- Already renders notes: `{% if order.notes %}<p class="order-notes">📝 {{ order.notes }}</p>{% endif %}` (lines ~104–106)
- **No changes needed here.**

### Order Management / Detail Display (`templates/orders/order_detail.html`)
- Already renders notes in the Order Info card: `{% if order.notes %}<div class="order-notes-highlight">...{{ order.notes }}</div>{% endif %}` (lines ~93–96)
- **No changes needed here.**

### JavaScript POS Object (`static/js/main.js`)
- `_draftPayload()` (line ~836): builds draft state — does NOT include notes
- `initDraftPersistence()` (line ~863): wires input listeners — does NOT wire notes textarea
- `restoreDraft()` (line ~880): restores draft state — does NOT restore notes
- `submitOrder()` (line ~1217): builds JSON body — does NOT include notes
- After `success`: clears customer name input (line ~1322) — notes field should also be cleared

### Tests (`apps/orders/tests.py`)
- POS tests: `PosOrderAtomicityTests` class — `_post_pos()` and `_post_pos_with_payment()` helpers post JSON to `create_pos_order`
- No existing test for `notes` field on POS orders

---

## Implementation Plan

- [ ] 1. Add the notes textarea to `templates/orders/pos.html`.

      Add a labeled `<textarea>` with `id="pos-notes"` inside the `.pos-checkout-controls`
      div, after the payment method row and before the closing `</div>`. Match the existing
      inline style (`font-size:0.82rem`, `padding:6px 10px`) of other POS controls. Add a
      mobile CSS rule for `#pos-notes` min-height (44px) alongside the existing
      `#pos-customer-name` rule (line ~133). Add `pos-notes` to the
      `.pos-layout--collapsed` hidden-controls block so it collapses with the drawer on mobile.

      Files: `templates/orders/pos.html`

      Verify: `python manage.py check` — no errors. Open the POS page in a browser and
      confirm the Notes textarea appears in the checkout controls area.

- [ ] 2. Wire the notes textarea into the JavaScript POS object in `static/js/main.js`.

      Four targeted edits, all within the `POS` object literal:

      a. **`_draftPayload()`** (line ~838): add `notes: document.getElementById('pos-notes')?.value || ''`
         to the returned object.

      b. **`initDraftPersistence()`** (line ~865): add `'pos-notes'` to the array of element
         IDs whose `input` event triggers `this.saveDraft()`.

      c. **`restoreDraft()`** (line ~880): after restoring the customer name, add:
         ```js
         const notesEl = document.getElementById('pos-notes');
         if (notesEl) notesEl.value = draft.notes || '';
         ```

      d. **`submitOrder()`** (line ~1224): add `notes: (document.getElementById('pos-notes')?.value || '').trim()`
         to the `JSON.stringify({...})` payload sent to the server.

      e. **After success** (line ~1322, where `pos-customer-name` value is cleared): also clear
         the notes textarea: `const notesEl = document.getElementById('pos-notes'); if (notesEl) notesEl.value = '';`

      Files: `static/js/main.js`

      Verify: `python manage.py check` — no errors. Place a POS order via the browser with
      notes text and confirm the notes field is cleared after successful order creation.

- [ ] 3. Add test cases for POS order notes to `apps/orders/tests.py`.

      In the `PosOrderAtomicityTests` class, add a `_post_pos_with_notes()` helper that sends
      `notes` in the JSON payload, then add the following test methods:

      a. `test_pos_order_with_notes_saves_correctly` — posts a POS order with
         `notes='Extra sugar please'`, asserts `order.notes == 'Extra sugar please'`.

      b. `test_pos_order_without_notes_succeeds` — posts a POS order without a `notes`
         key, asserts `order.notes == ''` (empty string default) and `data['success']` is True.

      c. `test_pos_order_notes_saved_for_cash_and_gcash` — posts two orders, one with
         `payment_method='cash'` and one with `payment_method='gcash'`, both with notes,
         asserts notes are persisted on both.

      d. `test_pos_order_notes_visible_in_kitchen_view` — creates a POS order with notes,
         advances it to `preparing` status (via `update_order_status` or direct model save),
         then GETs `kitchen:orders` as a kitchen staff user and asserts the notes text is
         present in the response.

      e. `test_pos_order_notes_trimmed_whitespace` — posts notes with leading/trailing
         whitespace (e.g. `'  hello  '`), asserts the stored value equals `'  hello  '`
         (server preserves what was sent; trimming is done client-side in JS before sending).
         *(This documents the actual behavior: the view uses `data.get('notes', '')` without
         extra stripping; the JS `.trim()` added in step 2 does the trimming before sending.)*

      f. `test_pos_order_very_long_notes_truncated_or_rejected` — posts notes of 10,001
         characters. Since `Order.notes` is a `TextField` (no `max_length`), assert the order
         is created successfully and the full text is stored. Document that the JS textarea
         uses `maxlength="1000"` (added in step 1) to enforce a practical limit client-side.

      g. `test_existing_pos_behavior_unchanged` — posts a POS order with the existing
         `_post_pos()` helper (no notes), asserts `data['success']` is True, stock is
         deducted, and `order.notes == ''`. Confirms no regression in the core POS flow.

      Files: `apps/orders/tests.py`

      Verify: `python manage.py test apps.orders.tests.PosOrderAtomicityTests --verbosity=2`
      — all tests pass including the new ones.

- [ ] 4. Run the full project check and targeted test suite.

      Run:
      1. `python manage.py check`
      2. `python manage.py test apps.orders --verbosity=2`
      3. `python manage.py test apps.kitchen --verbosity=2`

      Expected: `python manage.py check` exits 0. All tests pass. No regressions.

      Files: (none — verification only)

      Verify: All three commands exit with no errors.

---

## Summary of Changes

| File | Change |
|------|--------|
| `templates/orders/pos.html` | Add notes textarea in `.pos-checkout-controls`, CSS mobile rule, collapsed-drawer rule |
| `static/js/main.js` | Wire notes into `_draftPayload`, `initDraftPersistence`, `restoreDraft`, `submitOrder`, and post-success clear |
| `apps/orders/tests.py` | Add 7 test cases in `PosOrderAtomicityTests` |

**No changes needed to:**
- `apps/orders/models.py` — `notes = models.TextField(blank=True)` already exists
- `apps/orders/views.py` — `create_pos_order` already does `notes=data.get('notes', '')`
- `templates/kitchen/orders.html` — already renders `order.notes`
- `templates/orders/order_detail.html` — already renders `order.notes`
- `apps/orders/forms.py` — not used by POS
- No database migration required
