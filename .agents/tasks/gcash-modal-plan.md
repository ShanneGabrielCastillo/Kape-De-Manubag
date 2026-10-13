# GCash In-Page Verification Modal — Implementation Plan

## Findings from Codebase Exploration

### Verify/Reject endpoints
- **Verify URL**: `orders:verify_gcash_payment` → `POST /manage/<pk>/gcash/verify/`
- **Reject URL**: `orders:reject_gcash_payment` → `POST /manage/<pk>/gcash/reject/`
- Both decorated with `@login_required` + `@cashier_or_admin_required`
- Both return JSON: `{"success": true/false, ...}`
- **Verify** returns `{"success":true, "order_number":..., "new_status":"preparing", "new_status_display":"Preparing"}`
- **Reject** returns `{"success":true, "order_number":..., "message":"..."}`
- **Reject reason field**: `rejection_note` (optional, sent as FormData)
- Both POST with `X-CSRFToken: getCookie('csrftoken')` header (no body content-type needed for verify; FormData for reject)

### Existing "Verify GCash" button
- Desktop table (server-rendered): `<a href="{% url 'orders:order_detail' order.pk %}">` in Payment column (td index 6)
- Mobile card builder (`buildOrderCard`): same anchor pattern, href=`d.detailUrl`
- `gcash_submitted` SSE handler: also sets an anchor to `detailUrl`

### Screenshot storage
- Field: `order.gcash_proof` (ImageField, `upload_to='gcash_proofs/'`)
- URL: `order.gcash_proof.url` — full Cloudinary URL in production, local media URL in dev
- The `order_list` view **defers** `gcash_proof`, `gcash_reference`, `gcash_submitted_at`, `gcash_verified_at`, `gcash_notes` — these are NOT available in the list page context.
- **Decision**: Add a new `GET /manage/<pk>/gcash-details/` JSON endpoint to load these on-demand when the modal opens.

### Endpoint response type
- Both verify and reject already return JSON. No change to views needed for the modal's POST calls.
- **Decision**: Modal uses `fetch()` + JSON, identical to how `order_detail.html` calls the same endpoints.

### Row update after verify/reject
- The `post_save` signal in `apps/realtime/signals.py` automatically publishes `status_changed` when any Order is saved. Since `verify_gcash_payment` saves the order, a `status_changed` SSE will arrive at the order list.
- The existing `status_changed` handler in `order_list.html` already handles row updates for status, payment column, and quick-advance button.
- **Decision**: After the modal's fetch succeeds, do a **direct DOM update** immediately (so there's no flicker waiting for SSE). The SSE arrival is idempotent. The direct update and the SSE update will produce the same final state.

### Toast and CSRF
- `showToast(message, type)` — defined in `static/js/main.js`, globally available
- `getCookie('csrftoken')` — defined in `static/js/main.js`, globally available

### CSS modal pattern
- `static/css/main.css`: `.modal-backdrop`, `.modal`, `.modal-header`, `.modal-body`, `.modal-footer`, `.modal-close`
- `static/css/responsive.css`: `.modal-backdrop` becomes bottom-sheet on mobile (`align-items: flex-end`)
- Button classes: `btn btn-primary`, `btn btn-outline`, `btn btn-block`, `btn btn-caramel`

---

## Files Changed

| File | Change |
|------|--------|
| `apps/orders/views.py` | Add `gcash_payment_detail` view |
| `apps/orders/urls.py` | Add `gcash_payment_detail` URL |
| `apps/orders/tests.py` | Add tests for `gcash_payment_detail` |
| `templates/orders/order_list.html` | Replace Verify GCash anchors with modal buttons; add modal HTML + JS |

**Not changed**: `order_detail.html`, `services.py`, `models.py`, `realtime.py`, `main.css`, `main.js`

---

## Implementation Plan

- [ ] 1. Add `gcash_payment_detail` JSON view in `apps/orders/views.py`.

      The existing `order_list` view defers `gcash_proof`, `gcash_reference`, `gcash_submitted_at` — they are not available on the list page. Add a new staff-only JSON endpoint that loads these fields on-demand for the modal.

      Implementation: Add `gcash_payment_detail(request, pk)` immediately after `reject_gcash_payment`. Decorate with `@login_required` + `@cashier_or_admin_required`. Only allow GET (return 405 for other methods). Fetch the order with `get_object_or_404`. Guard: if `order.payment_method != 'gcash'` or `order.gcash_status != 'pending'` or `order.is_paid`, return `JsonResponse({'error': 'Not a pending GCash order'}, status=400)`. Return:
      ```python
      return JsonResponse({
          'order_number':    order.order_number,
          'customer_name':   order.customer_name,
          'total':           float(order.total),
          'gcash_status':    order.gcash_status,
          'gcash_reference': order.gcash_reference or '',
          'gcash_submitted_at': order.gcash_submitted_at.strftime('%-m %d, %Y %-I:%M %p') if order.gcash_submitted_at else '',
          'gcash_proof_url': order.gcash_proof.url if order.gcash_proof else None,
      })
      ```

      Files: `apps/orders/views.py`
      Verify: `python manage.py check` — no errors.

- [ ] 2. Register the new URL in `apps/orders/urls.py`.

      Add `path('manage/<int:pk>/gcash-details/', views.gcash_payment_detail, name='gcash_payment_detail')` in the Staff/Cashier section, just before the receipt path. No other URL changes.

      Files: `apps/orders/urls.py`
      Verify: `python manage.py check` — no errors.

- [ ] 3. Add tests for `gcash_payment_detail` in `apps/orders/tests.py`.

      In the existing `GCashPaymentTest` class (or a new `GCashPaymentDetailTest` class following the same `setUp` pattern with `self.cashier`, `self.admin`, `self.kitchen`):
      - `test_gcash_detail_ok_for_cashier`: authenticated cashier, pending GCash order → 200 JSON with all expected fields including `gcash_proof_url`
      - `test_gcash_detail_400_not_pending`: authenticated cashier, order with `gcash_status='none'` → 400
      - `test_gcash_detail_400_already_paid`: authenticated cashier, `is_paid=True` → 400
      - `test_gcash_detail_requires_login`: unauthenticated → redirect to login (status 302)
      - `test_gcash_detail_kitchen_blocked`: kitchen staff (non-cashier) → redirect/403

      Files: `apps/orders/tests.py`
      Verify: `python manage.py test apps.orders --verbosity=2` — new tests pass, all existing tests pass.

- [ ] 4. Add GCash detail URL data-attributes to `<tr>` rows in `order_list.html`.

      In the server-rendered `<tr data-order-id="...">` block, add three new `data-*` attributes:
      ```html
      data-gcash-detail-url="{% url 'orders:gcash_payment_detail' order.pk %}"
      data-gcash-verify-url="{% url 'orders:verify_gcash_payment' order.pk %}"
      data-gcash-reject-url="{% url 'orders:reject_gcash_payment' order.pk %}"
      ```
      Also in the `new_order` SSE handler (where new `<tr>` rows are built in JS), add the three URL fields using the same `.replace('/0/', '/${order.order_id}/')` template pattern that already exists for `detailUrl`, `payUrl`, etc.:
      ```js
      const gcashDetailUrl = `{% url 'orders:gcash_payment_detail' 0 %}`.replace('/0/', `/${order.order_id}/`);
      const gcashVerifyUrl = `{% url 'orders:verify_gcash_payment' 0 %}`.replace('/0/', `/${order.order_id}/`);
      const gcashRejectUrl = `{% url 'orders:reject_gcash_payment' 0 %}`.replace('/0/', `/${order.order_id}/`);
      row.dataset.gcashDetailUrl = gcashDetailUrl;
      row.dataset.gcashVerifyUrl = gcashVerifyUrl;
      row.dataset.gcashRejectUrl = gcashRejectUrl;
      ```

      Files: `templates/orders/order_list.html`
      Verify: View page source — confirm data attributes present on rows.

- [ ] 5. Replace Verify GCash `<a>` with `<button>` in the server-rendered table.

      In the Payment `<td>` (column 6, inside the `{% if order.payment_method == 'gcash' and order.gcash_status == 'pending' %}` block), replace:
      ```html
      <a href="{% url 'orders:order_detail' order.pk %}" class="btn btn-sm btn-primary">
        <i data-lucide="badge-check" class="btn-icon" aria-hidden="true"></i> Verify GCash
      </a>
      ```
      with:
      ```html
      <button onclick="openGcashVerifyModal({{ order.pk }}, '{% url 'orders:gcash_payment_detail' order.pk %}', '{% url 'orders:verify_gcash_payment' order.pk %}', '{% url 'orders:reject_gcash_payment' order.pk %}')"
              class="btn btn-sm btn-primary">
        <i data-lucide="badge-check" class="btn-icon" aria-hidden="true"></i> Verify GCash
      </button>
      ```

      Files: `templates/orders/order_list.html`
      Verify: Page loads, button element present, no JS errors on load.

- [ ] 6. Replace the Verify GCash anchor in the `gcash_submitted` SSE handler.

      In `RealtimeConnection.on('gcash_submitted', ...)`, where `payCell.innerHTML` is set to an anchor, replace with a button that calls `openGcashVerifyModal` using `row.dataset` URLs:
      ```js
      payCell.innerHTML = `<button onclick="openGcashVerifyModal(${data.order_id}, '${escHtml(row.dataset.gcashDetailUrl||'')}', '${escHtml(row.dataset.gcashVerifyUrl||'')}', '${escHtml(row.dataset.gcashRejectUrl||'')}')" class="btn btn-sm btn-primary"><i data-lucide="badge-check" class="btn-icon" aria-hidden="true"></i> Verify GCash</button>`;
      ```

      Files: `templates/orders/order_list.html`
      Verify: No JS errors; after a gcash_submitted SSE, the row shows the button.

- [ ] 7. Replace the Verify GCash anchor in `buildOrderCard` (mobile card builder).

      In the `buildOrderCard()` function, replace the `payHtml` anchor for `gcash/pending` state with:
      ```js
      payHtml = `<button onclick="openGcashVerifyModal(${escHtml(d.orderId)}, '${escHtml(d.gcashDetailUrl||'')}', '${escHtml(d.gcashVerifyUrl||'')}', '${escHtml(d.gcashRejectUrl||'')}')" class="btn btn-sm btn-primary"><i data-lucide="badge-check" class="btn-icon" aria-hidden="true"></i> Verify GCash</button>`;
      ```

      Files: `templates/orders/order_list.html`
      Verify: On mobile viewport (<768px), the card shows a button (not an anchor).

- [ ] 8. Add the GCash verification modal HTML to `order_list.html`.

      Inside `{% block content %}`, just after `{% include "partials/payment_modal.html" %}`, add the modal markup. Use `id="gcash-om-backdrop"` as the root. Structure follows the existing modal pattern in `order_detail.html` (`.modal-backdrop > .modal > .modal-header + .modal-body + .modal-footer`).

      The modal body has three states: loading (spinner), error (red alert box), content (order summary card + screenshot + warning). The footer has two states: main (Cancel + Reject + Verify) and rejection-confirm (Back + Confirm Rejection). The rejection textarea (`id="gcash-om-reject-note"`) is hidden until Reject is clicked.

      Set `max-height: 90vh; display: flex; flex-direction: column` on `.modal` so the body scrolls internally on small screens. The existing `responsive.css` `.modal-backdrop` bottom-sheet rule handles mobile layout automatically.

      Key element IDs:
      - `gcash-om-backdrop` — root backdrop
      - `gcash-om-close`, `gcash-om-close-btn` — close/cancel buttons
      - `gcash-om-loading`, `gcash-om-error`, `gcash-om-content` — state containers
      - `gcash-om-order-number`, `gcash-om-customer`, `gcash-om-amount` — order info
      - `gcash-om-ref-row`, `gcash-om-reference` — reference row (hidden if no reference)
      - `gcash-om-date-row`, `gcash-om-submitted-at` — submission date
      - `gcash-om-screenshot-wrap`, `gcash-om-screenshot-link`, `gcash-om-screenshot-img`, `gcash-om-fullsize-link` — screenshot
      - `gcash-om-no-screenshot` — shown when no screenshot
      - `gcash-om-reject-wrap`, `gcash-om-reject-note` — rejection textarea
      - `gcash-om-action-error` — inline action error
      - `gcash-om-footer-main`, `gcash-om-footer-reject` — footer states
      - `gcash-om-verify-btn`, `gcash-om-reject-start`, `gcash-om-reject-back`, `gcash-om-reject-confirm` — action buttons

      Files: `templates/orders/order_list.html`
      Verify: Page loads with no visible modal, no JS errors in browser console.

- [ ] 9. Add modal JavaScript to `{% block extra_js %}` in `order_list.html`.

      After the existing RealtimeConnection handlers and before `</script>`, add an IIFE that defines and exposes `window.openGcashVerifyModal`.

      **`openGcashVerifyModal(orderId, detailApiUrl, verifyUrl, rejectUrl)`:**
      - Store `_orderId`, `_verifyUrl`, `_rejectUrl`, `_triggerBtn` (document.activeElement)
      - Show backdrop (`display:flex`), `document.body.style.overflow='hidden'`
      - Show loading state, hide content/error, hide footer states
      - `document.addEventListener('keydown', _onKey)` for Escape
      - `fetch(detailApiUrl, {credentials:'same-origin'})` → on success populate fields:
        - `gcash-om-order-number`: JSON `order_number`
        - `gcash-om-customer`: JSON `customer_name`
        - `gcash-om-amount`: `'₱' + parseFloat(data.total).toFixed(2)`
        - `gcash-om-ref-row`: show/hide based on `data.gcash_reference`; set `gcash-om-reference` text
        - `gcash-om-submitted-at` / `gcash-om-date-row`: show/hide based on `data.gcash_submitted_at`
        - Screenshot: if `data.gcash_proof_url`, show `gcash-om-screenshot-wrap`, set img src and anchor hrefs; else show `gcash-om-no-screenshot`
        - Show `gcash-om-content`, hide `gcash-om-loading`
        - Show `gcash-om-footer-main` (set display to `flex` or `block`)
      - On fetch/JSON error: show `gcash-om-error` with message, hide loading

      **`_closeModal()`:**
      - `backdrop.style.display='none'`, restore `body.style.overflow`
      - `document.removeEventListener('keydown', _onKey)`
      - Reset state: `_submitting=false`, clear textarea, hide reject wrap, reset footer to main state, restore all button text/disabled states
      - If `_triggerBtn && _triggerBtn.focus`, call `_triggerBtn.focus()`

      **Backdrop click**: `if (e.target === backdrop && !_submitting) _closeModal()`

      **Verify click (`gcash-om-verify-btn`)**:
      - Guard: `if (_submitting) return; _submitting=true`
      - Disable verify + reject-start buttons, set verify button text to `'⏳ Verifying…'`
      - `fetch(_verifyUrl, {method:'POST', headers:{'X-CSRFToken':getCookie('csrftoken')}})`
      - On `data.success`: `_closeModal()`, `showToast('GCash payment verified! Order is now Preparing.', 'success')`, `updateRowAfterGcashVerify(_orderId, data)`
      - On `!data.success`: `_submitting=false`, re-enable buttons, restore text, show `gcash-om-action-error` with `data.error`
      - On network error: same recovery + `showToast('Network error — please try again', 'error')`

      **Reject-start click (`gcash-om-reject-start`)**:
      - Hide `gcash-om-footer-main`, show `gcash-om-reject-wrap` and `gcash-om-footer-reject`

      **Reject-back click (`gcash-om-reject-back`)**:
      - Show `gcash-om-footer-main`, hide `gcash-om-reject-wrap` and `gcash-om-footer-reject`, clear textarea

      **Reject-confirm click (`gcash-om-reject-confirm`)**:
      - Guard: `if (_submitting) return; _submitting=true`
      - Disable button, set text to `'⏳ Rejecting…'`
      - Build FormData: `fd.append('rejection_note', textarea.value)`
      - `fetch(_rejectUrl, {method:'POST', headers:{'X-CSRFToken':getCookie('csrftoken')}, body:fd})`
      - On `data.success`: `_closeModal()`, `showToast('Payment submission rejected. Customer has been notified.', 'info')`, `updateRowAfterGcashReject(_orderId)`
      - On failure: `_submitting=false`, re-enable button, show `gcash-om-action-error`

      Files: `templates/orders/order_list.html`
      Verify: No JS errors; clicking a Verify GCash button opens the modal and loads order data.

- [ ] 10. Add `updateRowAfterGcashVerify` and `updateRowAfterGcashReject` helper functions.

       **`updateRowAfterGcashVerify(orderId, data)`**:
       - `const row = document.querySelector('tr[data-order-id="' + orderId + '"]'); if (!row) return;`
       - `row.dataset.isPaid = 'true'; row.dataset.status = 'preparing'; row.dataset.statusDisplay = data.new_status_display || 'Preparing'; row.dataset.nextStatus = 'ready'; row.dataset.gcashStatus = 'verified';`
       - Status cell (`row.cells[5]`): `<span class="badge badge-preparing" style="font-size:0.8rem;padding:5px 10px">${escHtml(data.new_status_display||'Preparing')}</span>`
       - Payment cell (`row.cells[6]`): `<span class="badge badge-paid">Paid</span>`
       - Actions cell (`row.cells[row.cells.length-1]`): rebuild with print button + advance-to-ready button (matching the pattern in the existing `status_changed` handler):
         ```js
         const advanceUrl = row.dataset.advanceUrl || '';
         const receiptUrl = row.dataset.receiptUrl || '';
         actionsCell.innerHTML = `<div style="display:flex;gap:4px;flex-wrap:wrap">
           <a href="${escHtml(receiptUrl)}" class="btn btn-sm btn-outline" target="_blank"><i data-lucide="printer" class="btn-icon" aria-hidden="true"></i></a>
           <button onclick="quickAdvance('${escHtml(advanceUrl)}', this)" class="btn btn-sm btn-caramel"
                   data-next-status="ready" data-is-paid="true">▶ Ready</button>
         </div>`;
         ```
       - `window.initResponsiveTables && window.initResponsiveTables(); window.reinitLucide && window.reinitLucide();`

       **`updateRowAfterGcashReject(orderId)`**:
       - `const row = document.querySelector(...); if (!row) return;`
       - `row.dataset.gcashStatus = 'rejected'; row.dataset.status = 'awaiting_payment';`
       - Status cell: `<span class="badge" style="background:#fee2e2;color:#991b1b;font-size:0.8rem;padding:5px 10px"><i data-lucide="circle-x" class="status-icon" aria-hidden="true"></i> GCash Rejected</span>`
       - Payment cell: Pay button (reuse the same `openPaymentModal` call already in the existing `status_changed` handler for awaiting_payment):
         ```js
         const payUrl  = row.dataset.payUrl  || '';
         const orderId = row.dataset.orderId || '';
         const total   = parseFloat(row.dataset.totalRaw || '0').toFixed(2);
         const payMethod = row.dataset.paymentMethod || 'gcash';
         payCell.innerHTML = `<button onclick="openPaymentModal(${orderId}, ${total}, '${escHtml(payUrl)}', '${escHtml(payMethod)}', '')" class="btn btn-sm btn-caramel"><i data-lucide="credit-card" class="btn-icon" aria-hidden="true"></i> Pay</button>`;
         ```
       - `window.initResponsiveTables && window.initResponsiveTables(); window.reinitLucide && window.reinitLucide();`

       Files: `templates/orders/order_list.html`
       Verify: After verify, row shows Paid + Preparing without reload. After reject, row shows GCash Rejected + Pay button.

- [ ] 11. Run full test suite and confirm no regressions.

       Files: no changes
       Verify: `python manage.py test apps.orders --verbosity=2` — all tests pass, including the new FEAT-001 tests.

---

## Key Decisions

**One new JSON endpoint (not a template fragment)**: The order_list view defers GCash fields to keep the list query lean. The modal JS fetches them on-demand. This is one cheap GET per modal open — acceptable and follows the existing `api_payment_waiting_status` pattern.

**Direct DOM update + let SSE be idempotent**: After verify/reject, immediately update the row via `updateRowAfterGcashVerify/Reject()`. The `status_changed` SSE will also arrive via `post_save` signal — that's fine, it produces the same result and the handler is already idempotent. No need to suppress or coordinate with SSE.

**Reject uses a textarea (not `window.prompt`)**: The order_detail.html uses `prompt()` for reject which is acceptable there. For the order_list modal, using a proper textarea inside the modal is cleaner (no browser dialog breaking the flow) and consistent with the modal pattern.

**No changes to backend verify/reject views**: They already return JSON. No redirect path to handle.

**CSS**: No new CSS needed. The existing `.modal-backdrop`, `.modal`, `.modal-header`, `.modal-body`, `.modal-footer` classes from `main.css` + `responsive.css` (bottom-sheet mobile) already handle all required layouts including the specified test widths.
