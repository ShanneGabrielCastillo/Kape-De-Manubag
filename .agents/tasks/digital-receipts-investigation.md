# Digital Receipts Investigation Report & Implementation Plan

## 1. Order Model Field Inventory

### Order fields (from `apps/orders/models.py`)

| Field | Type | Meaning |
|-------|------|---------|
| `order_number` | CharField(20) | Human-readable ID: `KDM-YYYYMMDD-NNNN` |
| `customer_name` | CharField(200) | Customer name at order time; default `'Walk-in Customer'` |
| `customer_phone` | CharField(15) | Optional phone number |
| `table_number` | CharField(10) | Table, blank for takeout |
| `order_type` | CharField choices: `dine_in`, `takeout` | Dine-in vs. takeout |
| `status` | CharField choices: `awaiting_payment`, `preparing`, `ready`, `completed`, `cancelled` | Current order status |
| `notes` | TextField | Special instructions |
| `subtotal` | DecimalField(10,2) | Sum of item line totals |
| `discount` | DecimalField(10,2) | Discount applied |
| `packaging_fee` | DecimalField(10,2) | Per-item takeout packaging fee total |
| `total` | DecimalField(10,2) | `subtotal + packaging_fee - discount` |
| `is_paid` | BooleanField | **Primary paid indicator** — True only after staff explicitly confirms payment |
| `payment_method` | CharField choices: `cash`, `gcash` | Payment method |
| `amount_paid` | DecimalField(10,2) | Amount tendered by customer |
| `change_amount` | DecimalField(10,2) | Change given to customer |
| `gcash_status` | CharField choices: `none`, `pending`, `verified`, `rejected` | GCash verification state |
| `gcash_reference` | CharField(50) | GCash reference number from customer |
| `gcash_proof` | ImageField | Screenshot uploaded by customer (Cloudinary) |
| `gcash_submitted_at` | DateTimeField | When customer submitted GCash info |
| `gcash_verified_at` | DateTimeField | When staff verified OR rejected |
| `gcash_verified_by` | FK to User | Staff who verified/rejected |
| `gcash_notes` | TextField | Staff rejection notes |
| `stock_deducted` | BooleanField | True after inventory deduction |
| `request_token` | CharField(64) | Idempotency key |
| `tracking_token` | CharField(64) | **Secure customer-facing access token** — URL-safe random, ~256 bits |
| `cashier` | FK to User | Staff who processed order (null for customer checkout orders) |
| `created_at` | DateTimeField | Order creation timestamp |
| `updated_at` | DateTimeField | Last update timestamp |
| `completed_at` | DateTimeField | When order reached `completed` status |
| `cancelled_at` | DateTimeField | When order was cancelled |
| `queue_number` | PositiveIntegerField | Sequential queue number for the day |
| `queued_at` | DateTimeField | When order entered preparation queue |
| `ready_at` | DateTimeField | When order reached `ready` status |

### OrderItem fields

| Field | Type | Meaning |
|-------|------|---------|
| `order` | FK to Order | Parent order |
| `product` | FK to Product (SET_NULL) | Product (may be null if deleted) |
| `product_name` | CharField(200) | **Snapshot** of product name at order time |
| `category_name` | CharField(200) | **Snapshot** of category name at order time |
| `packaging_eligible` | BooleanField | **Snapshot** of packaging requirement at order time |
| `size` | CharField choices: `none`, `small`, `medium`, `large`, `hot` | Size variant |
| `quantity` | PositiveIntegerField | Quantity ordered |
| `unit_price` | DecimalField(10,2) | **Snapshot** of price at order time |
| `subtotal` | DecimalField(10,2) | `unit_price × quantity` |
| `notes` | CharField(200) | Item-level notes |

---

## 2. Authoritative Payment-Confirmed Indicator

**`order.is_paid == True`** is the single authoritative gate for receipt access.

Details:
- Set to `True` only by staff-side views (`process_payment`, `verify_gcash_payment`).
- Never set by customer actions. Submitting a GCash screenshot sets `gcash_status='pending'` but leaves `is_paid=False`.
- When a GCash order is rejected, `is_paid` stays `False`.
- For Cash orders: cashier processes payment via `process_payment` → `is_paid=True`.
- For GCash remote: customer submits → `gcash_status='pending'` → staff clicks Verify → `gcash_status='verified'`, `is_paid=True`.
- For GCash onsite: staff processes directly via `process_payment` (no screenshot required) → `is_paid=True`.

The `gcash_status` field provides additional context for GCash payment method display on the receipt, but is not used as the access gate.

**Payment confirmation timestamp limitation:** There is **no dedicated `paid_at` field**. For GCash orders, `gcash_verified_at` records when staff verified the payment, which serves as a proxy timestamp. For Cash orders, no timestamp is explicitly recorded. The `created_at` timestamp is always available. This limitation will be noted on the receipt.

---

## 3. Tracking Token Mechanism

- `tracking_token` is a `secrets.token_urlsafe(32)` — URL-safe Base64-encoded random bytes (~43 chars, ~256 bits of entropy).
- Generated in `Order.save()` when not present. Backfilled for legacy orders by migration `0017_order_tracking_token`.
- Used by all customer-facing endpoints (`order_tracker`, `api_track_order`, `payment_waiting`, `api_payment_waiting_status`, `submit_gcash_payment`) as the sole access credential.
- Token is stored in `Order.tracking_token` with `unique=True` and `db_index=True`.
- Invalid tokens: `get_object_or_404` returns 404 with no information leak.
- The tracker URL is `/orders/track/<tracking_token>/`.

The receipt endpoints must follow the same pattern: access via `tracking_token`, 404 on invalid token, no authentication required.

---

## 4. Existing Receipt/PDF Code

### Staff receipt (`templates/orders/receipt.html`)
- Thermal-printer-style template (Courier New monospace font, 380px wide).
- Displays: business name, order_number, date, time, customer, order_type, table, cashier name.
- Displays: item list with product_name, size, quantity × unit_price, subtotal.
- Displays: subtotal, packaging_fee, discount, TOTAL.
- Displays: payment method, amount_paid, change (when `order.is_paid`).
- Has: "Print Receipt" button with `window.print()`, a "Close" button, auto-prints after 500ms.
- Staff-only: served by `print_receipt` view which requires `@login_required` + `@cashier_or_admin_required`.
- **Reuse strategy**: The receipt template can be adapted (not reused directly) for the customer receipt. The customer receipt needs the same content but different styling (mobile-friendly, no auto-print, with Download PDF button, without cashier info, with "Back to Tracking" link). Creating a new `templates/orders/customer_receipt.html` is the right approach to avoid entangling staff and customer flows.

### Staff receipt view (`print_receipt` in `apps/orders/views.py`)
- Requires staff login. NOT reusable for customer access.
- Fetches order with `select_related('cashier')` and items with snapshot fields only.

### No existing PDF generation
- `requirements.txt` contains: Django, Pillow, python-dotenv, openpyxl, whitenoise, psycopg[binary], dj-database-url, gunicorn, cloudinary, django-cloudinary-storage, google-generativeai, resend.
- **No PDF library is installed** (no xhtml2pdf, weasyprint, reportlab, pdfkit).
- A lightweight library must be added. **Chosen: `xhtml2pdf==0.2.16`** — pure Python, no external binary dependencies (no wkhtmltopdf/Chromium), compatible with Django, renders HTML to PDF, works on Render's free tier. WeasyPrint requires GTK/Pango binaries that are hard to install on Render. ReportLab requires building PDFs programmatically rather than from templates. `xhtml2pdf` renders an HTML template to PDF, which means the receipt content stays in one template and the PDF is just a render of it — consistent with the existing template-driven approach.

---

## 5. PDF Library Decision

**Library chosen: `xhtml2pdf==0.2.16`**

Rationale:
- Pure Python — no system binaries required. Works on Render's free tier out of the box.
- Takes an HTML string or file and converts it to a PDF byte stream.
- Django integration is straightforward: render the receipt template to a string, pass it to xhtml2pdf, return the bytes as a `FileResponse`.
- Supports basic CSS (tables, fonts, colors). Sufficient for a receipt-style layout.
- Actively maintained, well-documented Django integration pattern.

Add to `requirements.txt`: `xhtml2pdf==0.2.16`

---

## 6. Realtime / Polling Mechanism

**Mechanism: Server-Sent Events (SSE) with HTTP polling fallback.**

- SSE endpoint: `GET /realtime/track/?tracking_token=<token>` → `customer_order_stream` in `apps/realtime/views.py`.
- Events dispatched to customer: `status_changed`, `payment_confirmed`, `order_accepted`, `gcash_rejected`, `heartbeat`.
- HTTP polling fallback: `GET /orders/api/track/<tracking_token>/` → `api_track_order` in `apps/orders/views.py`.
- The `order_tracker.js` uses SSE with a 30s safety-net poll when SSE is connected, or 5s fast poll when SSE is unavailable.
- `payment_confirmed` event is already fired by `process_payment` and `verify_gcash_payment` when `is_paid` becomes `True`.

**Receipt availability update strategy**: When `payment_confirmed` SSE event arrives (or when the poll returns `is_paid=True`), the tracker JS will show the receipt buttons. The `api_track_order` JSON response needs to include `is_paid` (it already does) and a `receipt_url` or `receipt_available` flag so the JS can reveal the buttons without a page reload. The tracker page will be updated to show/hide receipt buttons based on the live `is_paid` field from the API.

---

## 7. Tracking Template Path and Current Structure

**Template**: `templates/orders/order_tracker.html`

Current structure:
- Standalone HTML page (no base template, full `<!DOCTYPE html>`).
- Uses `css/main.css`, `css/mobile-design-system.css`, `css/responsive.css` and Lucide icons.
- CSS variables from the design system: `--brown-dark`, `--caramel`, `--cream-white`, `--cream`, `--gold`, `--radius`, `--shadow`, `--cream-light`, status-specific colors.
- Cards: `.tracker-card` — white/cream background, border-radius, shadow.
- Hero: `.tracker-hero` — colored by status class.
- Shows: queue number, status, progress steps, queue position, estimated wait, order items list.
- JS: `window.TRACKER_CONFIG` with `apiUrl`, `sseUrl`, `pollInterval`; loaded from `static/js/order_tracker.js`.

**Modification plan for tracker**: Add a receipt section below the order items card. Show "Receipt coming after payment confirmation" message when `!is_paid`. Show "View Receipt" and "Download Receipt" buttons when `is_paid`. The JS will update visibility based on live poll/SSE data. Server-renders with initial state; JS updates without page reload.

---

## 8. Limitations

1. **No `paid_at` timestamp for Cash orders**: The Order model has `gcash_verified_at` (set when staff verifies GCash) but no generic `paid_at` field. For Cash orders, there is no timestamp of when payment was confirmed. Workaround: display `gcash_verified_at` as "Payment Confirmed" timestamp for GCash orders; display a dash or "—" for Cash orders. **No new field will be added** to keep the scope minimal — `gcash_verified_at` covers GCash, and the task description says "if stored."

2. **No dedicated receipt number**: The `order_number` (e.g. `KDM-20250615-0001`) is used as the receipt identifier. This is the existing convention (used on the staff receipt template). No separate `receipt_number` field exists.

3. **GCash screenshot must not be exposed**: `gcash_proof` is stored in Cloudinary. The customer receipt must not include the screenshot URL. The receipt confirms payment method and GCash reference number only.

4. **No `amount_paid` / `change_amount` for all orders at receipt time**: These are set when payment is processed. For GCash verified orders: `amount_paid = order.total`, `change_amount = 0.00`. For Cash orders: whatever the cashier entered. Both are available when `is_paid=True`, so this is not a problem.

5. **Cashier info**: Customer receipts should not show the cashier's name (it's an internal field). The staff receipt shows it; the customer receipt will omit it.

---

## 9. Implementation Plan

### Phase overview
1. Add `xhtml2pdf` dependency  
2. Add two new URL patterns and two new views (`customer_receipt`, `customer_receipt_pdf`) in `apps/orders/`  
3. Create `templates/orders/customer_receipt.html` (HTML receipt for view)  
4. Create `templates/orders/customer_receipt_pdf.html` (PDF-optimized receipt template)  
5. Update `order_tracker.html` to show receipt section (server-rendered initial state + JS updates)  
6. Update `api_track_order` to include `receipt_url` in response  
7. Update `customer_order_stream` SSE to include `receipt_url` in `payment_confirmed` event  
8. Add tests in `apps/orders/tests.py`  
9. Run `python manage.py check` and tests

---

### Implementation Plan (ordered by dependency)

- [ ] 1. **Add `xhtml2pdf` to `requirements.txt`.**
  - Add `xhtml2pdf==0.2.16` to `requirements.txt`.
  - Files: `requirements.txt`
  - Verify: `pip install -r requirements.txt` succeeds; `python -c "import xhtml2pdf"` succeeds.

- [ ] 2. **Add two receipt URL patterns to `apps/orders/urls.py`.**
  - Add to the `urlpatterns` list (in the "Queue / Tracker — public" section):
    - `path('track/<str:tracking_token>/receipt/', views.customer_receipt, name='customer_receipt')`
    - `path('track/<str:tracking_token>/receipt/pdf/', views.customer_receipt_pdf, name='customer_receipt_pdf')`
  - Files: `apps/orders/urls.py`
  - Verify: `python manage.py check` passes; `python manage.py show_urls | findstr receipt` (or equivalent) shows the two new URLs.

- [ ] 3. **Add `customer_receipt` and `customer_receipt_pdf` views to `apps/orders/views.py`.**

  **`customer_receipt` view:**
  - Accepts `tracking_token` as URL parameter (no login required).
  - Uses `get_object_or_404(Order.objects.only(...), tracking_token=tracking_token)` — fetches only the fields needed for the receipt (same pattern as `order_tracker`).
  - Fields to fetch: `order_number`, `customer_name`, `order_type`, `table_number`, `status`, `is_paid`, `payment_method`, `amount_paid`, `change_amount`, `subtotal`, `discount`, `packaging_fee`, `total`, `created_at`, `gcash_status`, `gcash_reference`, `gcash_verified_at`, `tracking_token`.
  - **Access gate (server-enforced)**: if `not order.is_paid`, return `render(request, 'orders/customer_receipt_unavailable.html', {'order': order}, status=403)`. This is a server-side check, not just a hidden button.
  - Fetches items: `order.items.only('product_name', 'size', 'quantity', 'unit_price', 'subtotal')`.
  - Passes `pdf_url` (the PDF download URL) to the template context.
  - Does NOT expose `gcash_proof`, `cashier`, `request_token`, `tracking_token` details beyond what's needed.
  - Renders `templates/orders/customer_receipt.html`.

  **`customer_receipt_pdf` view:**
  - Same token lookup and `is_paid` gate as `customer_receipt`.
  - Renders `templates/orders/customer_receipt_pdf.html` to a string using `render_to_string`.
  - Uses `xhtml2pdf` (`from xhtml2pdf import pisa`) to convert the HTML string to PDF bytes.
  - Returns a `HttpResponse` with `Content-Type: application/pdf`, `Content-Disposition: attachment; filename="receipt-<order_number>.pdf"`.
  - Does NOT write to disk — PDF is generated in memory (`io.BytesIO`).
  - Does NOT change any order fields (read-only).
  - Logs any `xhtml2pdf` render errors with `logger.error` and returns HTTP 500 with a friendly message if PDF generation fails.

  Files: `apps/orders/views.py`
  Verify: `python manage.py check` passes.

- [ ] 4. **Create `templates/orders/customer_receipt.html` (HTML view).**

  Structure:
  - Standalone page matching the tracker/payment_waiting design system (same CSS imports, navbar, `.tracker-card` style).
  - Navbar: `☕ Kape De Manubag` left, `← Back to Tracking` right (links to `{% url 'orders:order_tracker' order.tracking_token %}`).
  - Page title: "Receipt — {{ order.order_number }} — Kape De Manubag"
  - Receipt header card: "☕ KAPE DE MANUBAG" centered, "Digital Receipt" subtitle.
  - Order info section: receipt/order number, order date/time (`created_at`), customer name, order type (Dine-In / Takeout), table number (if set), payment method.
  - Items table: product name, size, quantity × unit_price = subtotal (one row per item).
  - Totals section: subtotal, packaging fee (if > 0), discount (if > 0), **TOTAL**.
  - Payment section: payment method (`Cash` or `GCash`), amount paid, change (Cash), GCash reference (if `gcash_reference` is set), payment confirmation status (Confirmed ✓), payment confirmed timestamp (show `gcash_verified_at` for GCash, "—" for Cash).
  - Action buttons (`.no-print` div): "🖨 Print" (`onclick="window.print()"`), "⬇ Download PDF" (links to `pdf_url`), "← Back to Tracking" (links to `{% url 'orders:order_tracker' order.tracking_token %}`).
  - Print CSS: hide action buttons, adjust layout for printing.
  - Responsive: single-column, max-width 480px, uses existing CSS variables.
  - Does NOT include: cashier name, GCash screenshot, internal order IDs other than `order_number`, tracking_token.

  Files: `templates/orders/customer_receipt.html`
  Verify: Load `http://localhost:8000/orders/track/<token>/receipt/` with a paid order; confirm all fields render correctly.

- [ ] 5. **Create `templates/orders/customer_receipt_pdf.html` (PDF template).**

  Structure:
  - Minimal HTML optimized for `xhtml2pdf` rendering (subset CSS — no CSS variables, inline styles, absolute units).
  - Same data as the HTML receipt: header, order info, items, totals, payment section.
  - No navbar, no action buttons (PDF-only).
  - Font: Courier New or Arial (safe PDF fonts).
  - Page size: A4 or Letter. Receipt-style layout with margins.
  - All monetary values with two decimal places using `|floatformat:2`.
  - `xhtml2pdf` uses `{% load ... %}` but no `{% static %}` for external CSS — inline styles only.

  Files: `templates/orders/customer_receipt_pdf.html`
  Verify: Download PDF via `http://localhost:8000/orders/track/<token>/receipt/pdf/`; confirm PDF opens, shows correct order data.

- [ ] 6. **Create `templates/orders/customer_receipt_unavailable.html`.**

  A brief, friendly page shown when the receipt is requested but `is_paid=False`.
  - Same navbar/design system as the tracker.
  - Message: "Your receipt will be available after payment is confirmed."
  - Link back to the payment waiting page: `{% url 'orders:payment_waiting' order.tracking_token %}`.
  - Link back to order tracker: `{% url 'orders:order_tracker' order.tracking_token %}`.

  Files: `templates/orders/customer_receipt_unavailable.html`
  Verify: Visit receipt URL for an unpaid order; confirm 403 response with the "not yet available" message.

- [ ] 7. **Update `order_tracker.html` to display receipt section.**

  Changes to `templates/orders/order_tracker.html`:
  - Add a receipt card after the "Your Order" card:
    ```html
    <div class="tracker-card" id="receipt-section">
      <div class="tracker-card-label">Receipt</div>
      <!-- shown when is_paid=True (server-rendered initial state) -->
      {% if order.is_paid %}
      <a href="{% url 'orders:customer_receipt' order.tracking_token %}" class="btn btn-primary btn-block" id="btn-view-receipt">
        🧾 View Receipt
      </a>
      <a href="{% url 'orders:customer_receipt_pdf' order.tracking_token %}" class="btn btn-secondary btn-block" style="margin-top:8px" id="btn-download-receipt">
        ⬇ Download Receipt (PDF)
      </a>
      {% else %}
      <p style="font-size:0.82rem;color:#aaa;text-align:center;margin:0" id="receipt-unavailable-msg">
        🧾 Your receipt will be available after payment is confirmed.
      </p>
      {% endif %}
    </div>
    ```
  - Update `window.TRACKER_CONFIG` to include:
    - `receiptUrl: '{% url "orders:customer_receipt" order.tracking_token %}'`
    - `receiptPdfUrl: '{% url "orders:customer_receipt_pdf" order.tracking_token %}'`
    - `initialIsPaid: {{ order.is_paid|yesno:'true,false' }}`
  - The JS (`order_tracker.js`) `applyData` function currently sets status/queue info. It needs to also update receipt section visibility when `data.is_paid` becomes `true`. Add an `updateReceiptSection(isPaid)` function to `order_tracker.js` that:
    - Hides `#receipt-unavailable-msg` and shows `#btn-view-receipt` / `#btn-download-receipt` when `isPaid=true`.
    - Does nothing when already shown.
    - Called from `applyData()` when `data.is_paid` is observed (the `api_track_order` already returns `is_paid`).

  Files: `templates/orders/order_tracker.html`, `static/js/order_tracker.js`
  Verify: Unpaid order shows "Your receipt will be available…"; after confirming payment via staff UI, poll refreshes and buttons appear (test manually with a dev server, or verify via the JS unit-test-like check below).

- [ ] 8. **Update `api_track_order` to expose `receipt_url` and keep `is_paid`.**

  The view already returns `is_paid`. No additional field is strictly needed since the tracker template already embeds the receipt URLs in `TRACKER_CONFIG`. However, for the SSE `payment_confirmed` event to trigger the UI update correctly, the existing `applyData` function in `order_tracker.js` will call `updateReceiptSection(data.is_paid)` using the `is_paid` field already present.

  **No change to `api_track_order` is required** — `is_paid` is already in the response. The tracker JS just needs to call `updateReceiptSection` in `applyData`.

  Files: `apps/orders/views.py` (confirm `is_paid` already returned — it is, per line in `api_track_order`).
  Verify: `GET /orders/api/track/<token>/` returns `{"is_paid": true, ...}` for a paid order.

- [ ] 9. **Update `static/js/order_tracker.js` to handle receipt section visibility.**

  Add a `updateReceiptSection(isPaid)` function that:
  - Looks up `#receipt-unavailable-msg`, `#btn-view-receipt`, `#btn-download-receipt`.
  - If `isPaid` and `TRACKER_CONFIG.receiptUrl` are set: shows the two buttons, hides the unavailable message.
  - Does nothing if elements don't exist or if already shown (idempotent).
  - Call `updateReceiptSection(data.is_paid)` inside `applyData(data)`.
  - Also call `updateReceiptSection(config.initialIsPaid)` in the DOMContentLoaded bootstrap to handle the case where the page is loaded after payment is already confirmed.

  Files: `static/js/order_tracker.js`
  Verify: JS updates receipt section on poll when `is_paid` transitions from `false` to `true`.

- [ ] 10. **Add tests in `apps/orders/tests.py` (new `CustomerReceiptTests` class).**

  Following the existing test patterns (Django `TestCase`, `setUp` with `Category`/`Product`, `CustomUser`, helper methods for creating orders, `reverse()` for URLs).

  Tests to add:

  1. `test_unpaid_cash_order_receipt_returns_403` — Create unpaid Cash order; GET `customer_receipt` → 403.
  2. `test_gcash_awaiting_submission_receipt_returns_403` — GCash order, `gcash_status='none'`, `is_paid=False`; GET `customer_receipt` → 403.
  3. `test_gcash_pending_verification_receipt_returns_403` — GCash order, `gcash_status='pending'`, `is_paid=False`; GET `customer_receipt` → 403.
  4. `test_gcash_rejected_receipt_returns_403` — GCash order, `gcash_status='rejected'`, `is_paid=False`; GET `customer_receipt` → 403.
  5. `test_gcash_verified_order_can_view_receipt` — GCash order, `gcash_status='verified'`, `is_paid=True`; GET `customer_receipt` → 200; confirm order_number, customer_name, item names in response content.
  6. `test_cash_paid_order_can_view_receipt` — Cash order, `is_paid=True`; GET `customer_receipt` → 200; confirm order_number in content.
  7. `test_receipt_contains_correct_item_prices` — Paid order with 2 items at specific prices; GET `customer_receipt`; assert prices appear in content.
  8. `test_receipt_shows_packaging_fee` — Paid takeout order with packaging_fee > 0; GET `customer_receipt`; assert packaging fee appears.
  9. `test_receipt_pdf_unpaid_returns_403` — Unpaid order; GET `customer_receipt_pdf` → 403.
  10. `test_receipt_pdf_paid_returns_pdf` — Paid order; GET `customer_receipt_pdf` → 200 with `Content-Type: application/pdf`; assert content is non-empty bytes starting with `%PDF`.
  11. `test_invalid_tracking_token_receipt_returns_404` — GET `customer_receipt` with random token → 404.
  12. `test_receipt_does_not_modify_order` — GET `customer_receipt` and `customer_receipt_pdf` for paid order; assert `Order.objects.get(pk=order.pk)` is_paid, status, total unchanged after both requests.
  13. `test_invalid_token_cannot_access_another_order_receipt` — Create two paid orders; try to access order1's receipt with order2's token → 404.
  14. `test_receipt_generation_does_not_change_inventory` — Confirm stock count before and after receipt view/download requests matches.

  For the staff receipt tests (requirement #12), the existing `print_receipt` view already requires login; add a quick sanity test:
  15. `test_staff_print_receipt_still_requires_login` — GET `orders:print_receipt` without login → redirect to login.
  16. `test_staff_print_receipt_still_works_for_cashier` — Cashier login; GET `orders:print_receipt` for any order → 200.

  Files: `apps/orders/tests.py`
  Verify: `python manage.py test apps.orders.tests.CustomerReceiptTests --verbosity=2` — all tests pass.

- [ ] 11. **Run full verification.**

  - `python manage.py check` — no system errors.
  - `python manage.py test apps.orders` — all existing tests plus new `CustomerReceiptTests` pass.
  - Manual spot check: create a test order through checkout, confirm payment via staff UI (`/orders/manage/<pk>/payment/`), visit `/orders/track/<token>/` — receipt section shows buttons; click "View Receipt" — receipt page renders with correct data; click "Download PDF" — PDF downloads, contains matching data.

  Files: none (verification only)
  Verify: `python manage.py check` exits 0; `python manage.py test apps.orders` passes.

---

## Design Decisions

### Why `xhtml2pdf` over alternatives
WeasyPrint requires system-level GTK/Pango/Cairo libraries that are not available on Render's free tier without a custom buildpack. ReportLab requires programmatic PDF construction (no HTML template reuse). `xhtml2pdf` is pure Python, installs via pip, and directly converts HTML templates to PDF — minimal new code, consistent with the existing template-driven approach.

### Why a new `customer_receipt.html` instead of reusing `receipt.html`
The staff receipt auto-prints (`setTimeout(() => window.print(), 500)`), has a "Close" window button, and is designed for an 80mm thermal printer. The customer receipt needs a "Back to Tracking" link, download PDF button, mobile layout, no auto-print, no cashier name, and Kape De Manubag branding consistent with the customer-facing pages. Entangling both use cases in one template would add fragile conditionals and risk breaking the staff receipt.

### Why not a separate `receipt_number` field
The `order_number` (`KDM-YYYYMMDD-NNNN`) already uniquely identifies every order, is human-readable, and is used on the existing staff receipt. No migration is needed; no new field risk.

### Why `is_paid` is the sole gate (not `gcash_status == 'verified'`)
`is_paid` is the single field consistently set to `True` across all payment paths (Cash, GCash onsite, GCash remote after verification). Using `gcash_status` would break Cash receipt access. `is_paid` is the established authoritative indicator throughout the codebase.

### Realtime update approach
The tracker's `order_tracker.js` already polls `api_track_order` which returns `is_paid`. Adding `updateReceiptSection(data.is_paid)` to the existing `applyData` function requires minimal JS change and reuses the established polling/SSE pattern. No new API endpoint needed.

### Encoding the receipt URL in `TRACKER_CONFIG`
Rather than building the receipt URL in JS from the tracking token, the Django template renders the exact receipt URLs into `TRACKER_CONFIG.receiptUrl` and `receiptPdfUrl`. This avoids duplicating URL construction logic in JavaScript and keeps all URL routing in Django.
