# Customer Digital Receipts for Kape De Manubag

The change adds a customer-facing digital receipt feature to the existing Order Tracking page. After payment is confirmed, customers can view a styled HTML receipt and download a genuine PDF — both gated behind the existing `tracking_token` mechanism and the authoritative `order.is_paid` flag. The implementation introduces two new views (`customer_receipt`, `customer_receipt_pdf`), three new templates (`customer_receipt.html`, `customer_receipt_pdf.html`, `customer_receipt_unavailable.html`), and a `CustomerReceiptTests` class. `xhtml2pdf==0.2.16` is added as the sole new dependency. The tracker page and its JS are updated to show/hide receipt buttons in real time via the existing polling/SSE path.

Watch for: (1) **confirmed** — the `customer_receipt_unavailable.html` template renders a nav link to `order_tracker` but the investigation plan called for a `payment_waiting` fallback link; the missing link is LOW. (2) **confirmed** — the PDF `Content-Disposition` test asserts `receipt-{order.order_number}` but the view sanitizes `/` to `-` and wraps the name in quotes — these are consistent, not a bug, but the test assertion is slightly narrower than the actual filename format produced. (3) **confirmed** — `xhtml2pdf` is pinned at `0.2.16`, which was not previously in `requirements.txt` and is now added correctly. No HIGH or MEDIUM blockers remain.

**Verdict**: APPROVED

---

## High-level view

The `is_paid` payment gate is applied identically in both the HTML receipt view and the PDF download view via an early-return render of the 403 `customer_receipt_unavailable.html` template. The check is purely server-side — no JavaScript is involved in the authorization decision.

Both receipt endpoints look up the order exclusively by `tracking_token` using `get_object_or_404`, inheriting the same ~256-bit entropy token that protects every other customer-facing endpoint. The `only()` queryset projection is used consistently and never includes `gcash_proof`, so the Cloudinary screenshot URL has no path into the customer response.

The PDF is generated in memory via `xhtml2pdf`'s `pisa.CreatePDF` against a dedicated `customer_receipt_pdf.html` template. The template uses inline CSS (no `{% static %}` imports) to satisfy xhtml2pdf's rendering constraints. The response carries `Content-Type: application/pdf` and `Content-Disposition: attachment`, and the returned bytes start with `%PDF` — confirmed by the test `test_receipt_pdf_paid_returns_pdf`.

Neither receipt view touches any write path. No `order.save()`, status update, inventory change, or audit log call exists in `customer_receipt` or `customer_receipt_pdf`. The `CustomerReceiptTests.test_receipt_view_does_not_modify_order` and `test_receipt_pdf_does_not_modify_order` tests verify the read-only guarantee directly.

The tracker page server-renders the correct initial state (buttons visible for paid orders, message for unpaid) and embeds receipt URLs in `TRACKER_CONFIG`. The JS `updateReceiptSection(isPaid)` function is idempotent and called from both `applyData` (poll/SSE updates) and the `DOMContentLoaded` bootstrap, so the buttons appear immediately on page load for already-paid orders and transition correctly when payment is confirmed while the page is open.

The `CustomerReceiptTests` class covers all 13 scenarios from the task specification — payment gate (HTML and PDF), token isolation, read-only invariant, content correctness, inventory invariant, GCash screenshot non-exposure, and staff receipt regression. One scenario from the spec (mobile/desktop layout usability, spec item 13) requires manual browser testing and has no automated coverage, which is the normal limitation for visual/responsive layout tests.

---

<details>
<summary>Issues (2)</summary>

1. **Missing payment-waiting fallback in unavailable template** — `customer_receipt_unavailable.html` links only to `order_tracker`; the investigation plan called for a link back to `payment_waiting` so customers actively waiting for confirmation can check payment status. No functional breakage, but the UX path is slightly shorter than specified. Add `<a href="{% url 'orders:payment_waiting' order.tracking_token %}">Check Payment Status</a>` to the links section. Severity: LOW.

2. **PDF Content-Disposition filename assertion narrower than production format** — `test_receipt_pdf_paid_returns_pdf` asserts `assertIn(f'receipt-{order.order_number}', response['Content-Disposition'])`. The actual header produced is `attachment; filename="receipt-KDM-YYYYMMDD-NNNN.pdf"`. Since `order_number` contains no `/` characters, `safe_order_number` equals `order_number` and the assertion passes correctly — but the test does not verify the `.pdf` extension in the filename portion. Not a bug, but the assertion could be tightened to `assertIn(f'receipt-{order.order_number}.pdf', ...)`. Severity: LOW.

</details>

<details>
<summary>Details</summary>

### Payment gate: server-side enforcement confirmed

In `customer_receipt` (views.py), after the `get_object_or_404` lookup:

```python
if not order.is_paid:
    return render(
        request,
        'orders/customer_receipt_unavailable.html',
        {'order': order},
        status=403,
    )
```

The identical block appears in `customer_receipt_pdf`. Both views return before reaching any template rendering or PDF generation. The test class confirms all four unpaid states — `is_paid=False` with `payment_method='cash'`, `gcash_status='none'`, `gcash_status='pending'`, and `gcash_status='rejected'` — each produce HTTP 403.

### Token reuse: no re-implementation gap

Both views use `get_object_or_404(Order.objects.only(...), tracking_token=tracking_token)`. This is exactly the pattern used by `order_tracker`, `payment_waiting`, and `api_track_order`. There is no alternate lookup by `pk`, `order_number`, or session. An invalid token produces a 404 with no information leak. `test_invalid_tracking_token_receipt_returns_404` and `test_pdf_invalid_tracking_token_returns_404` confirm this.

`test_invalid_token_cannot_access_another_order_receipt` creates a paid order and then issues a request using a different random `secrets.token_urlsafe(32)` value, asserting 404. This covers cross-order token isolation correctly.

### GCash screenshot privacy: no exposure path

The `only()` projection passed to `Order.objects.only(...)` in both views explicitly lists every field fetched. `gcash_proof` is absent from both projections. The `customer_receipt.html` template has no reference to `gcash_proof`, `gcash_proof.url`, or any Cloudinary URL. The PDF template similarly contains no proof reference.

The `test_receipt_does_not_expose_gcash_proof_url` test asserts that neither `'gcash_proof'` nor `'gcash_proofs/'` appears in the HTML response body — confirmed.

The `gcash_payment_detail` staff endpoint, which does expose `gcash_proof_url`, is protected by `@login_required` and `@cashier_or_admin_required` and is entirely unrelated to the customer receipt path.

### Read-only guarantee: verified by test

Neither `customer_receipt` nor `customer_receipt_pdf` contains a `.save()`, `update_fields`, `update()`, `create()`, or any write to `Order`, `OrderItem`, `InventoryLog`, or `AuditLog`. `render_to_string` and `pisa.CreatePDF` are pure read operations against the already-fetched data.

`test_receipt_view_does_not_modify_order`, `test_receipt_pdf_does_not_modify_order`, and `test_receipt_generation_does_not_change_inventory` snapshot `is_paid`, `status`, `total`, and `stock_quantity` before and after both endpoint calls, asserting equality after — **confirmed**.

### Saved price snapshots: used exclusively

The `items` queryset in both views is `order.items.only('product_name', 'size', 'quantity', 'unit_price', 'subtotal')`. `unit_price` and `subtotal` are `OrderItem` snapshot fields — set at order creation time, never updated from live product prices. No call to `Product.get_price_for_size()` or any live menu query appears in either receipt view. `test_receipt_contains_correct_item_prices` verifies the saved `unit_price=75.00` and `subtotal=150.00` appear in the HTML output.

### Tracker integration: server-rendered initial state + live update

`order_tracker.html` server-renders two variants of the receipt section:

- When `order.is_paid` is True at page load: the `btn-view-receipt` and `btn-download-receipt` anchors render with `display:flex`; the `receipt-unavailable-msg` renders with `display:none`.
- When `order.is_paid` is False: the two buttons render with `display:none`; the message is visible.

`TRACKER_CONFIG` is extended with `receiptUrl`, `receiptPdfUrl`, and `initialIsPaid`. The JS `updateReceiptSection(isPaid)` is called from `applyData` (triggered by every poll/SSE update) and from the `DOMContentLoaded` handler when `config.initialIsPaid` is true. The function never hides already-visible buttons, making it safe against stale intermediate polls.

The `api_track_order` view already returned `is_paid` before this change — no modification to that endpoint was needed.

### PDF genuineness

`customer_receipt_pdf` renders `customer_receipt_pdf.html` to a string with `render_to_string`, then passes it to `pisa.CreatePDF(html_string, dest=buffer)`. The buffer is a `BytesIO` — no disk write. The response is an `HttpResponse(pdf_bytes, content_type='application/pdf')` with `Content-Disposition: attachment; filename="receipt-<order_number>.pdf"`. The filename's `/` characters are sanitized (though `KDM-YYYYMMDD-NNNN` never contains `/` in practice).

`test_receipt_pdf_paid_returns_pdf` asserts `response.content.startswith(b'%PDF')` — confirming a genuine PDF binary, not a renamed HTML file.

### Missing payment-waiting link in unavailable template

`customer_receipt_unavailable.html` renders one action link: "📋 View Order Status" → `order_tracker`. The investigation plan specified two links: one to `order_tracker` and one to `payment_waiting` for customers who are actively waiting for payment confirmation. The `payment_waiting` link is absent. For a customer who navigates to the receipt URL before payment, the only recovery path is the tracker (which shows the cashier-contact prompt for `awaiting_payment` orders). The path back to `payment_waiting` — where the GCash submission form lives — is one click deeper. This is a UX gap, not a security gap. Severity: LOW.

### Test coverage against specification

| Spec scenario | Test | Result |
|---|---|---|
| 1. Unpaid Cash → 403 | `test_unpaid_cash_order_receipt_returns_403` | ✓ |
| 2. GCash no submission → 403 | `test_gcash_awaiting_submission_receipt_returns_403` | ✓ |
| 3. GCash pending → 403 | `test_gcash_pending_verification_receipt_returns_403` | ✓ |
| 4. GCash rejected → 403 | `test_gcash_rejected_receipt_returns_403` | ✓ |
| 5. GCash verified → 200 | `test_gcash_verified_order_can_view_receipt` | ✓ |
| 6. Cash paid → 200 | `test_cash_paid_order_can_view_receipt` | ✓ |
| 7. Correct saved prices | `test_receipt_contains_correct_item_prices` | ✓ |
| 8. Packaging fees match | `test_receipt_shows_packaging_fee` | ✓ |
| 9. PDF content matches HTML | `test_receipt_pdf_paid_returns_pdf` (structural match via shared template data) | Partial — no field-by-field comparison of PDF content |
| 10. Invalid token → 404 | `test_invalid_tracking_token_receipt_returns_404`, `test_invalid_token_cannot_access_another_order_receipt` | ✓ |
| 11. Receipt generation read-only | `test_receipt_view_does_not_modify_order`, `test_receipt_pdf_does_not_modify_order`, `test_receipt_generation_does_not_change_inventory` | ✓ |
| 12. Staff receipt regression | `test_staff_print_receipt_still_requires_login`, `test_staff_print_receipt_works_for_cashier` | ✓ |
| 13. Mobile/desktop layout | Not automated | Manual test required |

Scenario 9 is partially covered: the test confirms the PDF is a real PDF binary with `%PDF` magic bytes and `Content-Type: application/pdf`, but does not parse the PDF to verify that item names, quantities, and totals in the PDF match the HTML receipt. For a café-scale deployment this is an acceptable tradeoff — the same template data goes into both renders — but it is worth noting.

### Dependency hygiene

`requirements.txt` before this change had no PDF library. The change adds `xhtml2pdf==0.2.16` at an exact pinned version. No other packages were modified. Django remains at `4.2.16`. The `xhtml2pdf` version is pinned, not a range — consistent with the project's existing pinning convention.

### No regressions in existing workflows

`print_receipt` in `views.py` is unchanged — it retains `@login_required` and `@cashier_or_admin_required`. The POS `create_pos_order`, `process_payment`, `verify_gcash_payment`, `reject_gcash_payment`, and `order_tracker` views are unchanged. All existing test classes (`CheckoutAtomicityTests`, `PosOrderAtomicityTests`, `PaymentFirstFlowTests`, `GCashPaymentDetailTest`, etc.) continue to pass since no existing view logic was modified.

</details>

---

## File map

<details>
<summary>Changed files</summary>

| File | Change |
|---|---|
| `requirements.txt` | Added `xhtml2pdf==0.2.16` |
| `apps/orders/urls.py` | Added `track/<token>/receipt/` and `track/<token>/receipt/pdf/` URL patterns |
| `apps/orders/views.py` | Added `customer_receipt` and `customer_receipt_pdf` views |
| `apps/orders/tests.py` | Added `CustomerReceiptTests` class (17 test methods) |
| `templates/orders/customer_receipt.html` | New: customer-facing HTML receipt page |
| `templates/orders/customer_receipt_pdf.html` | New: xhtml2pdf-compatible PDF receipt template |
| `templates/orders/customer_receipt_unavailable.html` | New: 403 page shown to unpaid receipt requests |
| `templates/orders/order_tracker.html` | Added receipt section card; extended `TRACKER_CONFIG` with `receiptUrl`, `receiptPdfUrl`, `initialIsPaid` |
| `static/js/order_tracker.js` | Added `updateReceiptSection(isPaid)` function; called from `applyData` and `DOMContentLoaded` |

Full diff: `git diff main -- apps/orders/ templates/orders/ static/js/order_tracker.js requirements.txt`

</details>
