# Implementation Plan: Require Screenshot Instead of Reference Number for Remote GCash Orders

## Exploration Findings

### Files Identified

| Area | File |
|---|---|
| Payment-waiting template | `templates/orders/payment_waiting.html` |
| GCash submission form | `apps/orders/forms.py` → `GCashSubmissionForm` |
| GCash submission view | `apps/orders/views.py` → `submit_gcash_payment` |
| Order model + GCash fields | `apps/orders/models.py` |
| Cloudinary/storage config | `kape_de_manubag/settings.py` |
| Image upload validator | `apps/accounts/validators.py` → `validate_payment_proof_upload` |
| Staff order detail (verify UI) | `templates/orders/order_detail.html` |
| Staff order list | `templates/orders/order_list.html` |
| GCash verification view | `apps/orders/views.py` → `verify_gcash_payment` |
| GCash rejection view | `apps/orders/views.py` → `reject_gcash_payment` |
| Realtime/SSE JS | `static/js/payment_waiting.js` |
| Audit service | `apps/audit/services.py` → `log_action` |
| Existing GCash tests | `apps/orders/tests.py` → `PaymentFirstFlowTests` |

---

## 1. Form Field Changes

### Remove from `GCashSubmissionForm` (apps/orders/forms.py)
- **Remove**: `gcash_reference` field (the `forms.CharField` with `min_length=3`) entirely.
- **Remove**: `clean_gcash_reference()` method entirely.
- **Change**: `gcash_proof` field — set `required=True` (currently `required=False`).
- **Update** `gcash_proof` widget: add a nicer `accept` attribute already present.
- **Update** `gcash_proof` help text: change from "Optional: upload a screenshot…" to "Upload a screenshot of your GCash payment receipt."

### No model field changes
- `gcash_reference` stays on `Order` — it is `blank=True, default=''` and will simply remain empty for new orders. Historical orders keep their data. **No migration needed.**
- `gcash_proof` is already an `ImageField(blank=True, null=True)` — no schema change needed. The form will now require it on submission, but the DB field stays nullable for backward compat with old orders.

---

## 2. Server-Side Validation Changes

### In `apps/orders/views.py` → `submit_gcash_payment`

**Remove** the duplicate-reference check (lines ~752–765):
```python
# REMOVE THIS ENTIRE BLOCK:
duplicate_qs = Order.objects.filter(
    gcash_reference=reference,
    gcash_status__in=('pending', 'verified'),
).exclude(pk=order.pk)
if duplicate_qs.exists():
    return JsonResponse({'success': False, 'errors': {'gcash_reference': ...}}, status=400)
```

**Change** the form extraction line (line ~748):
```python
# BEFORE:
reference = form.cleaned_data['gcash_reference']
proof_file = form.cleaned_data.get('gcash_proof')

# AFTER (reference becomes empty string — model field preserved):
reference = form.cleaned_data.get('gcash_reference', '')  # always '' now
proof_file = form.cleaned_data['gcash_proof']  # now required — always present
```

**Update** the `order.save()` block to no longer set `gcash_reference` from submitted form data, while still saving `gcash_proof`. Since `gcash_reference` is blank=True/default='' the field is simply left at its default when not submitted.

The `update_fields` list changes from:
```python
['gcash_reference', 'gcash_status', 'gcash_submitted_at', 'gcash_proof']
```
to:
```python
['gcash_proof', 'gcash_status', 'gcash_submitted_at']
```
(or keep `gcash_reference` in the list with empty string — either is fine, leaving it out is cleaner)

**Update** the audit log detail string:
```python
# BEFORE:
detail=f'GCash ref: {reference} — proof: {"yes" if proof_file else "no"}'
# AFTER (reference is always empty, proof is always yes):
detail='GCash screenshot uploaded'
```

**Update** the `gcash_submitted` SSE broadcast payload — remove `gcash_reference` key (it's empty). Add `has_proof: True` so the order_list JS can confirm evidence was submitted.

**Security invariants remain unchanged**:
- `is_paid` is never set to `True` in `submit_gcash_payment` — untouched.
- `verify_gcash_payment` still requires `@login_required @cashier_or_admin_required` — untouched.
- `gcash_status` only moves to `'pending'` in the customer view — untouched.

---

## 3. Where Reference Number Validation Currently Exists — Complete Removal Map

| Location | What to Remove/Change |
|---|---|
| `apps/orders/forms.py` | Remove `gcash_reference` field + `clean_gcash_reference()` |
| `apps/orders/forms.py` | Change `gcash_proof` `required=False` → `required=True` |
| `apps/orders/views.py` line ~748 | Remove `reference = form.cleaned_data['gcash_reference']` |
| `apps/orders/views.py` lines ~752–765 | Remove entire duplicate-reference `Order.objects.filter(gcash_reference=...)` block |
| `apps/orders/views.py` line ~782 | Remove `order.gcash_reference = reference` assignment |
| `apps/orders/views.py` line ~788 `update_fields` | Remove `'gcash_reference'` from the list |
| `apps/orders/views.py` line ~795 audit detail | Change audit detail to mention screenshot not reference |
| `apps/orders/views.py` line ~808 broadcast | Remove `'gcash_reference': reference` from the `gcash_submitted` event |

No other files reference the reference number in submission validation.

---

## 4. Cloudinary/Storage Integration

`settings.py` configures `cloudinary_storage.storage.MediaCloudinaryStorage` as the default storage backend when `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, and `CLOUDINARY_API_SECRET` are set in environment variables. When those are absent (local dev), it falls back to `django.core.files.storage.FileSystemStorage`.

`Order.gcash_proof` is an `ImageField(upload_to='gcash_proofs/')`. The field already uses the Django `default` storage backend, so it automatically uses Cloudinary in production with zero code changes. No new storage configuration is needed.

The `validate_payment_proof_upload` validator in `apps/accounts/validators.py` already enforces:
- 3 MB maximum size
- jpg/jpeg/jfif/png/gif/webp only
- Pillow content verification (not just extension)
- 8000×8000 px dimension cap

This validator is already attached to `gcash_proof` in `GCashSubmissionForm`. Making `gcash_proof` required does not change the validator — it continues to run on the uploaded file.

---

## 5. Model Field Changes — None Required

- `gcash_reference` stays on `Order` with `blank=True, default=''`. New orders leave it empty. Historical orders keep their data. **No migration.**
- `gcash_proof` stays `ImageField(blank=True, null=True)` — nullable for historical orders that have no screenshot. **No migration.**
- The form now requires the screenshot on submission, but the DB constraint does not change.

---

## 6. Realtime Events

### `gcash_submitted` event (in `submit_gcash_payment`)
Currently broadcasts: `{order_id, order_number, customer_name, total, gcash_reference}`

After the change, `gcash_reference` is always empty — remove it from the payload. The event itself still fires correctly after the DB commit. The `order_list.html` JS handler for `gcash_submitted` does not use `gcash_reference` — it only uses `order_id` to find the row and `customer_name` for the toast. **No JS change needed.**

### All other events unchanged:
- `payment_confirmed` — fired by `verify_gcash_payment` — no reference number involved
- `order_accepted` — fired by `verify_gcash_payment` — no reference number involved
- `gcash_rejected` — fired by `reject_gcash_payment` — only sends `rejection_note`
- SSE `payment_waiting.js` — listens for `payment_confirmed`, `order_accepted`, `status_changed`, `gcash_rejected`, `heartbeat`. None of these are affected.

---

## 7. Staff UI — What to Update

### `templates/orders/order_detail.html`

In the GCash `'pending'` state block (around line 110):
- **Change** the card label: "GCash Reference Submitted — Awaiting Verification" → "GCash Screenshot Submitted — Awaiting Verification"
- **Remove** the `<div>` that shows `Reference #: <code>{{ order.gcash_reference }}</code>` (it will be blank for new orders). Keep it conditionally: `{% if order.gcash_reference %}` for backward compat on historical orders.
- The screenshot preview is **already present**: `{% if order.gcash_proof %}` block renders `<img src="{{ order.gcash_proof.url }}">`. This already works — no change needed for displaying the screenshot.
- **Update** the GCash verify modal summary card to remove the reference row (or make it conditional on `order.gcash_reference` being non-empty).
- The "Payment Screenshot:" label, the `<img>`, and the "No screenshot uploaded" fallback are already there. The screenshot now becomes the primary evidence so the "No screenshot uploaded" message should indicate something is unexpected (though for historical orders it's fine).

### `templates/orders/order_list.html`

No structural change needed. The `gcash_status == 'pending'` badge "GCash — Verify" and the "Verify GCash" button already function correctly for screenshot-only submissions. The `gcash_submitted` real-time handler already updates the row. Only optional: update the toast text from "GCash reference submitted" to "GCash payment screenshot submitted" for clarity.

---

## 8. Migration Required

**None.** No model field is added, removed, or altered. The form-level `required=True` on `gcash_proof` is enforced by Django forms, not the database.

---

## 9. Exact List of Files to Change

### `apps/orders/forms.py`
- Remove `gcash_reference` CharField and `clean_gcash_reference()` method
- Change `gcash_proof` from `required=False` to `required=True`
- Update `gcash_proof` help_text to "Upload a screenshot of your GCash payment receipt."

### `apps/orders/views.py`
- In `submit_gcash_payment`:
  - Remove `reference = form.cleaned_data['gcash_reference']` line
  - Remove the entire duplicate-reference `Order.objects.filter(gcash_reference=...)` block
  - Remove `order.gcash_reference = reference` assignment
  - Update `update_fields` list to remove `'gcash_reference'`
  - Change `proof_file = form.cleaned_data.get('gcash_proof')` to `proof_file = form.cleaned_data['gcash_proof']`
  - Remove `if proof_file:` guard — proof is now always present
  - Update audit log `detail` string
  - Remove `'gcash_reference': reference` from `gcash_submitted` SSE broadcast

### `templates/orders/payment_waiting.html`
- In the `{% if order.payment_method == 'gcash' and not order.is_paid and order.gcash_status in 'none,rejected' %}` block:
  - Remove the `<div>` for "GCash Reference Number *" label and `{{ gcash_form.gcash_reference }}` input
  - Remove the `<div class="field-error" id="error-gcash_reference">` element
  - Remove the "The 13-digit number from your GCash transaction receipt." helper text
  - Change "Payment Screenshot (optional)" label to "Payment Screenshot *" (required)
  - Add helper text: "Please upload a screenshot of your successful GCash payment. Our staff will review it before confirming your order."
  - Add image preview `<img>` tag that shows selected file (set by JS)
  - Update card label from "Submit Payment Reference" to "Submit Payment Screenshot"
  - Update button text to "Submit Payment Screenshot for Verification"
  - Update "awaiting_payment" subtitle text in the `{% else %}` branch: from "submit your reference number" → "upload your payment screenshot"
  - Update `payment_method == 'gcash' and gcash_status == 'pending'` subtitle text (already fine: "submitted. Staff will verify shortly")
- In the form submit JS (inline `<script>`):
  - Add image preview logic on file input `change` event
  - Remove the `error-gcash_reference` from the "clear previous errors" loop (it won't exist)
  - Keep the `error-gcash_proof` error display

### `templates/orders/order_detail.html`
- In the `gcash_status == 'pending'` block:
  - Change the section heading from "GCash Reference Submitted — Awaiting Verification" to "GCash Screenshot Submitted — Awaiting Verification"
  - Wrap the `Reference #:` row in `{% if order.gcash_reference %}...{% endif %}` for backward compat
  - Update the note about "Check your GCash account" to mention the screenshot as primary evidence
  - Update the `gv-reference` modal read to handle empty string gracefully (already does via `|| '—'`)

### `apps/orders/tests.py`
- Update `test_submit_gcash_payment_sets_pending_status`: send a screenshot file instead of `gcash_reference`; remove `order.gcash_reference` assertion
- Update `test_submit_gcash_rejects_empty_reference`: replace with `test_submit_gcash_rejects_missing_screenshot` — send POST without a file and assert `'gcash_proof'` key in errors
- Update `test_submit_gcash_idempotent_for_pending`: no reference in the POST
- Update `test_submit_gcash_blocks_duplicate_reference`: this test is no longer applicable — replace with a new test `test_submit_gcash_requires_proof_image` that verifies the form rejects an empty screenshot field
- Remove `test_submit_gcash_blocks_duplicate_reference` (duplicate reference check is removed)
- Keep all other GCash tests unchanged (they test verify, reject, staff-only access — none of those touch reference submission)

---

## 10. Security Verification Checklist

| Check | Status after change |
|---|---|
| Customer submission never sets `is_paid=True` | ✅ `submit_gcash_payment` never touches `is_paid`; only `gcash_status='pending'` and saves `gcash_proof` |
| Only Admin/Cashier can verify GCash payment | ✅ `verify_gcash_payment` still has `@login_required @cashier_or_admin_required` — unchanged |
| Kitchen Staff cannot verify | ✅ `cashier_or_admin_required` decorator already blocks `role='kitchen'` — unchanged |
| Customer cannot directly set `gcash_status='verified'` | ✅ `submit_gcash_payment` only sets `'pending'`; `'verified'` is only set in `verify_gcash_payment` (staff-only) |
| Screenshot upload cannot bypass server validation | ✅ `validate_payment_proof_upload` runs server-side via Django form validators before any DB write |
| Failed upload does not mark order paid | ✅ Upload validation happens before the `transaction.atomic()` write block; a validation failure returns `400` immediately |
| Failed upload does not leave order in falsely verified state | ✅ `gcash_status` is only written inside the `transaction.atomic()` block, after the form validates successfully |
| Cloudinary upload failure is handled | ✅ Cloudinary errors propagate as exceptions during `order.save()`, which is inside `transaction.atomic()`. The transaction rolls back and the view should return a 500 (Django exception handler). The `gcash_status` is never committed as `'pending'` on upload failure. Optionally wrap `order.save()` in try/except to return a user-friendly JSON error. |
| POS GCash orders unaffected | ✅ POS orders go through `process_payment`, not `submit_gcash_payment` — completely separate code path |
| Onsite customer GCash (no screenshot) unaffected | ✅ The `process_payment` route for `gcash_status='none'` is unchanged |
| Historical orders with references remain readable | ✅ `gcash_reference` field preserved; `order_detail.html` shows it when non-empty |

---

## Implementation Order (Dependency-Sequenced)

1. **`apps/orders/forms.py`** — Remove reference field, make proof required. This is the foundation; all downstream changes depend on the form contract.

2. **`apps/orders/views.py`** → `submit_gcash_payment` — Remove reference handling, remove duplicate-reference check, update to require proof, update audit log + broadcast. Depends on the form change.

3. **`templates/orders/payment_waiting.html`** — Remove reference input UI, make screenshot required visually, add image preview, update help text. Depends on the form change (field names come from the form).

4. **`templates/orders/order_detail.html`** — Make reference display conditional, update section heading. Independent of steps 1–3 (backward compat only).

5. **`apps/orders/tests.py`** — Update existing GCash submission tests to match new behavior. Depends on steps 1–2.
