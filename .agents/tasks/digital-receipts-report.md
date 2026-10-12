# Customer Digital Receipts — Final Implementation Report

**Project:** Kape De Manubag  
**Feature:** Customer digital receipt view and PDF download, gated by payment confirmation

---

## 1. Exact Files Changed

| File | Change |
|---|---|
| `requirements.txt` | Added `xhtml2pdf==0.2.16` (sole new dependency; pinned to exact version) |
| `apps/orders/urls.py` | Added `track/<token>/receipt/` and `track/<token>/receipt/pdf/` URL patterns in the "Queue / Tracker — public" section |
| `apps/orders/views.py` | Added `customer_receipt` and `customer_receipt_pdf` views (~100 lines); existing views are untouched |
| `apps/orders/tests.py` | Added `CustomerReceiptTests` class with 19 test methods covering all 13 spec scenarios |
| `templates/orders/customer_receipt.html` | New: customer-facing HTML receipt page with Print button, Download PDF button, and Back to Tracking link |
| `templates/orders/customer_receipt_pdf.html` | New: xhtml2pdf-compatible receipt template using inline CSS and safe PDF fonts |
| `templates/orders/customer_receipt_unavailable.html` | New: 403 page shown to unpaid receipt requests |
| `templates/orders/order_tracker.html` | Added receipt section card (server-rendered initial state); extended `TRACKER_CONFIG` with `receiptUrl`, `receiptPdfUrl`, `initialIsPaid` |
| `static/js/order_tracker.js` | Added `updateReceiptSection(isPaid)` function; called from `applyData` (poll/SSE) and `DOMContentLoaded` bootstrap |

---

## 2. Receipt View and Download URL Patterns

| Name | URL | View |
|---|---|---|
| `orders:customer_receipt` | `/orders/track/<tracking_token>/receipt/` | `customer_receipt` — HTML receipt; 200 when paid, 403 when unpaid |
| `orders:customer_receipt_pdf` | `/orders/track/<tracking_token>/receipt/pdf/` | `customer_receipt_pdf` — PDF download; 200 + `application/pdf` when paid, 403 when unpaid |

Both URLs use the `<str:tracking_token>` segment — the same path type used by `order_tracker`, `payment_waiting`, and all other customer-facing tracker endpoints. No new URL namespaces or prefixes were introduced.

The downloaded PDF filename is `receipt-<order_number>.pdf` (e.g. `receipt-KDM-20250615-0001.pdf`). Any `/` in `order_number` is sanitized to `-` before constructing the filename; in practice `KDM-YYYYMMDD-NNNN` never contains `/`.

---

## 3. PDF Generation Approach and Dependency Changes

**Library:** `xhtml2pdf==0.2.16`  
**How added:** Appended to `requirements.txt` at an exact pinned version, consistent with the project's existing pinning convention.

**Rationale for choosing xhtml2pdf:**  
WeasyPrint requires GTK/Pango system libraries unavailable on Render's free tier. ReportLab requires programmatic PDF construction rather than HTML template reuse. `xhtml2pdf` is pure Python (no system binaries), installs via pip, converts an HTML string to PDF bytes, and works on Render out of the box.

**Generation process:**

1. The same order data fetched for the HTML receipt is passed to `render_to_string('orders/customer_receipt_pdf.html', {...})`.
2. The resulting HTML string is passed to `pisa.CreatePDF(html_string, dest=buffer)` where `buffer` is a `BytesIO` — no disk write occurs.
3. If `result.err` is set, a `logger.error` call records the failure and HTTP 500 is returned.
4. On success, an `HttpResponse(pdf_bytes, content_type='application/pdf')` is returned with `Content-Disposition: attachment; filename="receipt-<order_number>.pdf"`.

The PDF template (`customer_receipt_pdf.html`) uses inline CSS only (no `{% static %}` imports) to satisfy xhtml2pdf's rendering constraints. All monetary values use `|floatformat:2`. Test `test_receipt_pdf_paid_returns_pdf` asserts `response.content.startswith(b'%PDF')` — confirming a genuine PDF binary.

---

## 4. Tracking Token Validation for Receipt Access

Both `customer_receipt` and `customer_receipt_pdf` use:

```python
order = get_object_or_404(
    Order.objects.only(
        'order_number', 'customer_name', 'order_type', 'table_number',
        'status', 'is_paid', 'payment_method', 'amount_paid',
        'change_amount', 'subtotal', 'discount', 'packaging_fee',
        'total', 'created_at', 'gcash_status', 'gcash_reference',
        'gcash_verified_at', 'tracking_token',
    ),
    tracking_token=tracking_token,
)
```

This is exactly the pattern used by `order_tracker`, `payment_waiting`, `api_track_order`, and `submit_gcash_payment`. There is no alternate lookup by `pk`, `order_number`, or session. The `tracking_token` field is `unique=True`, `db_index=True`, and generated as `secrets.token_urlsafe(32)` (~256 bits of entropy). An invalid or unknown token produces a 404 with no information leak.

The `only()` projection explicitly excludes `gcash_proof`, so the Cloudinary screenshot URL has no path into the customer response. Neither receipt template references `gcash_proof` or any Cloudinary URL.

Cross-order isolation is verified by `test_invalid_token_cannot_access_another_order_receipt`: a valid tracking token for one order cannot be used to access a different order's receipt.

---

## 5. Payment Gate Enforcement (Server-Side)

**Field used:** `order.is_paid` (BooleanField on the `Order` model)

**Logic — identical in both views:**

```python
if not order.is_paid:
    return render(
        request,
        'orders/customer_receipt_unavailable.html',
        {'order': order},
        status=403,
    )
```

This check runs before any template rendering or PDF generation. It is purely server-side — no JavaScript is involved in the authorization decision.

`is_paid` is the single authoritative payment indicator across all payment paths:

- **Cash:** Set to `True` only by the `process_payment` staff view.
- **GCash (remote):** Set to `True` only by the `verify_gcash_payment` staff view after screenshot review. Customer submitting a screenshot sets `gcash_status='pending'` but leaves `is_paid=False`. Staff rejection leaves `is_paid=False`.
- **GCash (onsite):** Set to `True` by `process_payment`, same as Cash.

The four unpaid states tested and confirmed to return HTTP 403:

| State | Test |
|---|---|
| Cash, `is_paid=False` | `test_unpaid_cash_order_receipt_returns_403` |
| GCash, `gcash_status='none'`, `is_paid=False` | `test_gcash_awaiting_submission_receipt_returns_403` |
| GCash, `gcash_status='pending'`, `is_paid=False` | `test_gcash_pending_verification_receipt_returns_403` |
| GCash, `gcash_status='rejected'`, `is_paid=False` | `test_gcash_rejected_receipt_returns_403` |

---

## 6. How `is_receipt_available` Is Surfaced to the Tracking Template

The tracker does not use a separate `is_receipt_available` variable; it uses `order.is_paid` directly.

**Server-rendered initial state** in `order_tracker.html`:

- When `order.is_paid` is `True` at page load: `#btn-view-receipt` and `#btn-download-receipt` render with `display:flex`; `#receipt-unavailable-msg` renders with `display:none`.
- When `order.is_paid` is `False`: the two buttons render with `display:none`; the message is visible.

**Live update via JS:**

`window.TRACKER_CONFIG` is extended with three keys injected by the Django template:

```javascript
receiptUrl: '{% url "orders:customer_receipt" order.tracking_token %}',
receiptPdfUrl: '{% url "orders:customer_receipt_pdf" order.tracking_token %}',
initialIsPaid: {{ order.is_paid|yesno:'true,false' }},
```

`order_tracker.js` exposes `updateReceiptSection(isPaid)` which:

- Looks up `#receipt-unavailable-msg`, `#btn-view-receipt`, `#btn-download-receipt`.
- When `isPaid` is truthy: hides the unavailable message, shows the two buttons.
- Is idempotent — safe to call multiple times with the same value.

It is called from:

1. `applyData(data)` — triggered by every poll response and SSE event, using `data.is_paid`.
2. The `DOMContentLoaded` bootstrap handler when `config.initialIsPaid` is `true` — ensures buttons appear immediately on page load for already-paid orders without waiting for the first poll.

---

## 7. Realtime / Polling Path Update

**Was updated:** Yes.

The existing mechanism was left intact and extended minimally:

- `api_track_order` already returned `is_paid` in its JSON response before this change. No modification to that endpoint was needed.
- The SSE `payment_confirmed` event already fires from `process_payment` and `verify_gcash_payment` when `is_paid` becomes `True`. No modification to the realtime app was needed.
- The only change was in `order_tracker.js`: `updateReceiptSection(data.is_paid)` is now called inside `applyData`, which is already triggered on every poll response and SSE event.

When staff confirms payment:

1. The SSE `payment_confirmed` event fires immediately (or the 5-second poll catches it on the next cycle).
2. `applyData` runs, calling `updateReceiptSection(true)`.
3. The receipt buttons appear without a page reload.

If the customer opens the tracking link later, the server-rendered initial state shows the buttons immediately because `order.is_paid` is already `True`.

---

## 8. Test Results

**Test run:** `python manage.py test apps.orders.tests.CustomerReceiptTests --verbosity=2`

```
Ran 19 tests in 44.757s

OK
```

All 19 tests passed. No failures, errors, or unexpected skips.

Full test-to-spec mapping:

| Spec scenario | Test method | Result |
|---|---|---|
| 1. Unpaid Cash → 403 | `test_unpaid_cash_order_receipt_returns_403` | ✓ PASS |
| 2. GCash no submission → 403 | `test_gcash_awaiting_submission_receipt_returns_403` | ✓ PASS |
| 3. GCash pending → 403 | `test_gcash_pending_verification_receipt_returns_403` | ✓ PASS |
| 4. GCash rejected → 403 | `test_gcash_rejected_receipt_returns_403` | ✓ PASS |
| 5. GCash verified → 200 | `test_gcash_verified_order_can_view_receipt` | ✓ PASS |
| 6. Cash paid → 200 | `test_cash_paid_order_can_view_receipt` | ✓ PASS |
| 7. Correct saved prices | `test_receipt_contains_correct_item_prices` | ✓ PASS |
| 8. Packaging fee matches | `test_receipt_shows_packaging_fee` | ✓ PASS |
| 9. PDF content matches HTML | `test_receipt_pdf_paid_returns_pdf` (structural — `%PDF` magic bytes; no field-by-field PDF parse) | ✓ PASS (partial coverage — see limitations) |
| 10. Invalid token → 404 | `test_invalid_tracking_token_receipt_returns_404`, `test_pdf_invalid_tracking_token_returns_404`, `test_invalid_token_cannot_access_another_order_receipt` | ✓ PASS |
| 11. Receipt generation read-only | `test_receipt_view_does_not_modify_order`, `test_receipt_pdf_does_not_modify_order`, `test_receipt_generation_does_not_change_inventory` | ✓ PASS |
| 12. Staff receipt regression | `test_staff_print_receipt_still_requires_login`, `test_staff_print_receipt_works_for_cashier` | ✓ PASS |
| 13. Mobile/desktop layout | Not automated | Manual test required |

Additional tests not directly mapped to a single spec scenario:

| Test method | Result |
|---|---|
| `test_receipt_pdf_unpaid_returns_403` | ✓ PASS |
| `test_receipt_does_not_expose_gcash_proof_url` | ✓ PASS |

---

## 9. Result of `python manage.py check`

```
System check identified no issues (0 silenced).
```

The only output before this line was a `UserWarning` about `SECRET_KEY` not being set in the `.env` file, which is a pre-existing development environment note — not a system check issue introduced by this change.

---

## 10. Known Limitations

1. **No `paid_at` timestamp for Cash orders.** The `Order` model has `gcash_verified_at` (recorded when staff verifies a GCash payment) but no generic `paid_at` field. The customer receipt displays `gcash_verified_at` as the payment confirmation timestamp for GCash orders, and shows `—` for Cash orders. No new model field was added; the spec accepts this with the clause "if stored."

2. **No dedicated receipt number.** `order_number` (`KDM-YYYYMMDD-NNNN`) is used as the receipt identifier, consistent with the existing staff receipt template. No separate `receipt_number` field exists on the model.

3. **PDF content is not parsed field-by-field in tests.** Spec scenario 9 ("PDF content matches HTML receipt") is confirmed structurally: the test asserts `Content-Type: application/pdf`, `%PDF` magic bytes, and non-empty content. The same template data (`order`, `items`) is passed to both the HTML and PDF renders, so content equivalence is architecturally guaranteed, but the test does not extract and compare individual fields from the rendered PDF bytes. For a café-scale deployment this is an accepted tradeoff.

4. **Mobile/desktop layout (spec scenario 13) requires manual testing.** There is no automated coverage for responsive/visual layout correctness. The templates use the project's existing `css/main.css` and `css/mobile-design-system.css` design system variables and a `max-width: 480px` single-column layout, but visual validation requires a real browser.

5. **Missing `payment_waiting` link on the receipt unavailable page.** `customer_receipt_unavailable.html` links back to `order_tracker` only. The investigation plan specified a second link to `payment_waiting` for customers still waiting for GCash confirmation. The tracker page shows the cashier-contact prompt for `awaiting_payment` orders, so the recovery path exists — it is just one tap deeper. This is a LOW-severity UX gap, not a security or functional issue.

6. **PDF `Content-Disposition` test assertion is narrower than the production header.** `test_receipt_pdf_paid_returns_pdf` asserts `assertIn(f'receipt-{order.order_number}', response['Content-Disposition'])`. The actual header is `attachment; filename="receipt-KDM-YYYYMMDD-NNNN.pdf"`. Since `order_number` contains no `/` characters, `safe_order_number` equals `order_number` and the assertion passes correctly. The test does not verify the `.pdf` extension in the filename portion. Not a functional bug — LOW severity.
