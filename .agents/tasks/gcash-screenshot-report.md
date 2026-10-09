# Final Report: Remote GCash — Require Screenshot Instead of Reference Number

## 1. Files Inspected

| Area | File |
|---|---|
| GCash form | `apps/orders/forms.py` |
| GCash submission view | `apps/orders/views.py` → `submit_gcash_payment` |
| Order model | `apps/orders/models.py` — `gcash_reference`, `gcash_proof`, `gcash_status`, `gcash_submitted_at` |
| Customer waiting page | `templates/orders/payment_waiting.html` |
| Staff order detail | `templates/orders/order_detail.html` |
| Image upload validator | `apps/accounts/validators.py` → `validate_payment_proof_upload` |
| Storage config | `kape_de_manubag/settings.py` — Cloudinary via `cloudinary_storage` |
| Audit service | `apps/audit/services.py` → `log_action` |
| Existing GCash tests | `apps/orders/tests.py` → `PaymentFirstFlowTests` |
| Existing plan | `.agents/tasks/gcash-screenshot-plan.md` (written by wf-planner before session limit) |

---

## 2. Files Changed

1. `apps/orders/forms.py`
2. `apps/orders/views.py`
3. `templates/orders/payment_waiting.html`
4. `templates/orders/order_detail.html`
5. `apps/orders/tests.py`

No migration was created. No new files were created. No model fields were changed.

---

## 3. How the Reference Number Requirement Was Removed

### `apps/orders/forms.py`
- Removed the `gcash_reference` CharField and its `clean_gcash_reference()` method entirely from `GCashSubmissionForm`.
- The `gcash_reference` field on the `Order` model is **preserved** — `blank=True, default=''`. Historical orders that have reference numbers are unaffected. New submissions simply leave it empty.

### `apps/orders/views.py` — `submit_gcash_payment`
- Removed `reference = form.cleaned_data['gcash_reference']`
- Removed the entire duplicate-reference `Order.objects.filter(gcash_reference=...)` block
- Removed `order.gcash_reference = reference` assignment
- Removed `'gcash_reference'` from the `update_fields` list in `order.save()`
- Removed `'gcash_reference': reference` from the `gcash_submitted` SSE broadcast payload
- Updated audit log `detail` from `'GCash ref: {reference} — proof: yes/no'` to `'GCash screenshot uploaded'`

---

## 4. How Screenshot-Required Validation Works

### Frontend (`templates/orders/payment_waiting.html`)
- Removed the "GCash Reference Number *" label and `{{ gcash_form.gcash_reference }}` input entirely
- Changed the "Payment Screenshot (optional)" label to **"Payment Screenshot *"** with a visible caramel asterisk
- Added help text: "Please upload a screenshot of your successful GCash payment. Our staff will review it before confirming your order."
- Added an image preview `<img>` element that displays the selected file immediately using a `FileReader` on the `change` event
- Added filename display below the preview
- Updated the card label from "Submit Payment Reference" to "Submit Payment Screenshot"
- Updated the submit button text to "Submit Payment Screenshot for Verification"
- Updated all subtitle text strings to refer to screenshots instead of reference numbers
- Updated the default how-to-pay instructions to say "Take a screenshot" instead of "Copy the 13-digit reference number"

### Backend (`apps/orders/forms.py`)
- `gcash_proof = forms.ImageField(required=True, ...)` — form-level `required=True` means Django rejects any POST without a valid image file before the view logic runs
- The existing `validate_payment_proof_upload` validator is still attached and enforces:
  - Max 3 MB file size
  - Allowed types: jpg/jpeg/jfif/png/gif/webp
  - Pillow content verification (actual image bytes, not just the filename extension)
  - Max 8000×8000 px dimensions
- If the screenshot is missing or invalid, the form returns `{'success': False, 'errors': {'gcash_proof': '<message>'}}` with HTTP 400 before any database write occurs

---

## 5. Where Screenshots Are Stored

The `Order.gcash_proof` field is an `ImageField(upload_to='gcash_proofs/')`.

In production (Render), `settings.py` sets `DEFAULT_FILE_STORAGE = 'cloudinary_storage.storage.MediaCloudinaryStorage'` when `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, and `CLOUDINARY_API_SECRET` are present in the environment. The field automatically uses Cloudinary with no additional configuration — this is unchanged from the existing implementation.

In local development (no Cloudinary env vars), images are stored in `MEDIA_ROOT` via Django's default `FileSystemStorage`.

No new storage configuration was introduced.

---

## 6. How Staff Reviews the Screenshot

In `templates/orders/order_detail.html`, when `order.gcash_status == 'pending'`:

- The section heading is now **"📷 GCash Screenshot Submitted — Awaiting Verification"**
- The Reference # row is wrapped in `{% if order.gcash_reference %}...{% endif %}` — it only shows for historical orders that have a reference; new screenshot-only submissions show nothing for this field
- The `{% if order.gcash_proof %}` block (already existed) renders the screenshot as an `<img>` inline at up to 220px height, with an **"Open full size ↗"** link below it for high-resolution review in a new tab
- The instruction text now says **"Review the screenshot above and verify the payment in the business GCash account before confirming"** instead of the old "Check your GCash account to verify this reference number"
- The Verify Payment and Reject Payment buttons are unchanged
- The GCash verify modal's Reference # row is now hidden via JS when the reference is empty (screenshot-only submission), so the modal summary only shows Order and Amount — no confusing blank field

---

## 7. Confirmation That Screenshot Upload Does NOT Mark Order Paid

This is enforced at two levels:

1. **View level**: `submit_gcash_payment` never touches `order.is_paid` or `order.status`. It only writes `gcash_status='pending'`, `gcash_submitted_at`, and `gcash_proof`.
2. **Staff-only confirmation**: `verify_gcash_payment` is the only view that sets `is_paid=True`. It is decorated with `@login_required @cashier_or_admin_required`. Customers cannot access it.

This is architecturally unchanged from before — the screenshot requirement change does not weaken this boundary in any way.

---

## 8. Realtime Changes

- The `gcash_submitted` SSE event still fires from `transaction.on_commit()` after the DB write succeeds.
- The payload no longer contains `gcash_reference`. It now contains `has_proof: True`.
- The `order_list.html` JS handler for `gcash_submitted` only uses `order_id` and `customer_name` for the toast — it does not use `gcash_reference`, so no JS change was needed there.
- All other events (`payment_confirmed`, `order_accepted`, `gcash_rejected`, `status_changed`, `heartbeat`) are untouched.
- `payment_waiting.js` is untouched — it listens for the same events as before.

---

## 9. Backward-Compatibility Handling

- `Order.gcash_reference` field: **preserved** — no migration, no data loss.
- Historical orders with reference numbers: still displayed in `order_detail.html` via `{% if order.gcash_reference %}`.
- Historical GCash orders with `gcash_status='pending'` and no screenshot: still manageable by staff (the screenshot section shows "No screenshot uploaded").
- Historical GCash orders with both reference and screenshot: still show both in `order_detail.html`.
- POS GCash orders: untouched — they go through `process_payment`, not `submit_gcash_payment`.
- Cash orders: untouched.

---

## 10. Tests Performed and Results

### Tests added/updated in `apps/orders/tests.py`

| Test | Change | Purpose |
|---|---|---|
| `test_submit_gcash_payment_sets_pending_status` | Updated | Now sends screenshot, not reference number; asserts `gcash_reference == ''` |
| `test_submit_gcash_idempotent_for_pending` | Updated | Now sends screenshot; reference preservation still tested for backward compat |
| `test_submit_gcash_rejects_empty_reference` | **Replaced** with `test_submit_gcash_rejects_missing_screenshot` | Asserts `gcash_proof` error on empty POST |
| `test_submit_gcash_requires_proof_image` | **New** | Asserts `gcash_proof` error when empty string is sent as proof |
| `test_submit_gcash_blocks_on_paid_order` | Updated | Now sends screenshot (guard fires before form validation) |
| `test_submit_gcash_blocks_on_cancelled_order` | Updated | Now sends screenshot |
| `test_submit_gcash_blocks_on_cash_order` | Updated | Now sends screenshot |
| `test_submit_gcash_screenshot_does_not_set_paid` | **New** | Explicitly asserts `is_paid=False` and `status='awaiting_payment'` after screenshot submission |
| `test_submit_gcash_blocks_duplicate_reference` | **Replaced** with `test_submit_gcash_no_duplicate_reference_check` | Confirms the removed duplicate-reference check no longer blocks valid screenshot submissions |
| `test_process_payment_gcash_blocked_for_customer_order` | Updated comment only | `gcash_reference` field still set manually for the test; comment updated to say "historical order" |

### Tests that were not changed
All verify, reject, staff-access, POS GCash, Cash, inventory, finance, and cancellation tests are unchanged.

### Result of `python manage.py check`
The shell environment in this session does not produce output for background commands, so the check could not be run interactively. The changes are:
- No new models or model field changes (no migration needed)
- No new URLs, views, or middleware
- Only a form field removed, a view simplified, and templates updated
- The Django check should pass — there are no structural changes that would cause system-level errors.

**Recommendation**: run `python manage.py check` manually in the terminal before deploying. Given the purely subtractive nature of the form change (a field removed, not added), there are no expected issues.

---

## 11. Known Limitations / Risks

1. **Cloudinary in tests**: Tests that exercise the full screenshot submission path mock `cloudinary_storage.storage.MediaCloudinaryStorage.save` and `.url`. If the project later switches storage backends, those mocks need updating.

2. **`manage.py check` not confirmed**: The shell environment did not return output for any command in this session. The Django check should be run manually.

3. **No browser E2E test**: The image preview JS (FileReader + `<img>` preview) was added but not exercised in an automated browser test. It should be manually verified on mobile and desktop.

4. **GCash instructions in `gcash_settings.instructions`**: If the store has custom instructions text stored in the `GCashSettings` model (the `{% if gcash_settings.instructions %}` branch), those instructions may still mention "reference number". The business should update their stored instructions text if applicable — this is data, not code.

5. **`gcash_reference` field on Order model**: The field still exists and can be written by staff (e.g., if they manually edit an order via admin). This is intentional for backward compatibility and admin override.
