# GCash Modal HTTP 500 — Fix Report

## Root Cause and Exception

**Failing endpoint:** `GET /orders/manage/<pk>/gcash-details/`  
**View:** `gcash_payment_detail` in `apps/orders/views.py`

The view called `strftime` with `%-d` (day, no leading zero) and `%-I` (12-hour, no leading zero) format codes. These codes are **Linux-only**. On Windows they raise:

```
ValueError: Invalid format string
```

Reproduced directly:
```
py -c "import datetime; dt = datetime.datetime(2024, 1, 5, 9, 3); print(repr(dt.strftime('%B %-d, %Y %-I:%M %p')))"
# Output: ValueError: Invalid format string
```

This unhandled `ValueError` inside the view propagated to Django's error handler, which returned HTTP 500.

**Secondary fix (belt-and-suspenders):** The original `if order.gcash_proof:` guard evaluates `True` for an empty-string `ImageField` value, which can cause `ValueError` when `.url` is accessed on local storage. The guard was tightened to also check `.name`.

## Failing Endpoint

`GET /orders/manage/<int:pk>/gcash-details/` — `gcash_payment_detail`

The view was correct in all other respects: auth decorators intact, correct model fields, correct `.only()` fields, correct URL kwarg `pk`, correct JSON response shape. The sole failure was the `strftime` format string used to format `gcash_submitted_at`.

## Files Changed

### `apps/orders/views.py`

**Before:**
```python
gcash_submitted_at = None
if order.gcash_submitted_at:
    local_dt = timezone.localtime(order.gcash_submitted_at)
    gcash_submitted_at = local_dt.strftime('%B %-d, %Y %-I:%M %p')

gcash_proof_url = None
if order.gcash_proof:
    gcash_proof_url = order.gcash_proof.url
```

**After:**
```python
gcash_submitted_at = None
if order.gcash_submitted_at:
    local_dt = timezone.localtime(order.gcash_submitted_at)
    # %-d and %-I are Linux-only strftime codes (no leading zero) and
    # raise ValueError on Windows.  Build the string cross-platform.
    day_str    = str(local_dt.day)
    hour_12    = local_dt.hour % 12 or 12
    hour_str   = str(hour_12)
    minute_str = local_dt.strftime('%M')
    period     = local_dt.strftime('%p')
    gcash_submitted_at = (
        local_dt.strftime('%B ')
        + day_str
        + local_dt.strftime(', %Y ')
        + hour_str + ':' + minute_str + ' ' + period
    )

gcash_proof_url = None
if order.gcash_proof and order.gcash_proof.name:
    gcash_proof_url = order.gcash_proof.url
```

## Fix Implemented

Replaced the two Linux-only strftime platform codes with cross-platform string building:

- `%-d` (no-leading-zero day) → `str(local_dt.day)`
- `%-I` (no-leading-zero 12-hour) → `str(local_dt.hour % 12 or 12)`

The output format is identical: e.g. `January 5, 2024 9:03 AM` (no leading zero on day or hour).

Also tightened the `gcash_proof` guard from `if order.gcash_proof:` to `if order.gcash_proof and order.gcash_proof.name:` to prevent `.url` access on an empty-string `ImageField`.

No database migrations were needed. No other code was changed.

## Tests Performed

### Cross-platform datetime fix verified in shell

```
py -c "
import datetime, os, sys
sys.path.insert(0, '.')
os.environ['DEBUG'] = '1'
import django; django.setup()
from django.utils import timezone
dt = timezone.make_aware(datetime.datetime(2024, 1, 5, 9, 3))
day_str    = str(dt.day)
hour_12    = dt.hour % 12 or 12
hour_str   = str(hour_12)
minute_str = dt.strftime('%M')
period     = dt.strftime('%p')
result     = dt.strftime('%B ') + day_str + dt.strftime(', %Y ') + hour_str + ':' + minute_str + ' ' + period
print('OK:', result)
"
# Output: OK: January 5, 2024 9:03 AM
```

### Django system check

```
py manage.py check
# System check identified no issues (0 silenced).
```

### Full orders test suite

```
py manage.py test apps.orders.tests --verbosity=1
# Ran 88 tests in 227.411s
# OK
```

All 88 existing tests passed. The error-log output during tests (`RuntimeError: log write failed`) is a pre-existing mock side-effect in the inventory/status tests; it is not caused by this change and the tests themselves pass (the mock raises to verify the error-handling path, not to indicate a failure of the test runner).

## Invariants Confirmed

- **Every model field accessed in `gcash_payment_detail`** (`order_number`, `customer_name`, `total`, `payment_method`, `is_paid`, `gcash_status`, `gcash_reference`, `gcash_submitted_at`, `gcash_proof`) exists on the `Order` model with the exact names used in `.only()` and in the response body.
- **Null or empty `gcash_proof`** handled: `if order.gcash_proof and order.gcash_proof.name:` covers both `None` and `''`.
- **JSON serialization**: `total` → `float()`, `gcash_submitted_at` → `str`, all others are native `str`/`bool` — no `Decimal` or `datetime` objects leak into the response.
- **URL kwarg match**: URL pattern uses `<int:pk>`, template uses `{% url 'orders:gcash_payment_detail' order.pk %}` and JS uses `.replace('/0/', /${orderId}/)` — consistent end-to-end.
- **`@login_required` and `@cashier_or_admin_required`** decorators remain in place on `gcash_payment_detail`.
- **Returns 404** for missing orders (via `get_object_or_404`).
- **Returns 400** for non-pending GCash orders.
- **Returns 405** for non-GET requests (first check in the view).
- **Opening the modal or loading details never sets `is_paid=True`** — the view is GET-only and read-only.
- **Verify Payment and Reject Payment** actions untouched; their views (`verify_gcash_payment`, `reject_gcash_payment`) were not modified.
- **Existing Order Details page** (`order_detail`) was not modified.
- **No stack traces exposed** in error responses — the view returns structured JSON error objects.
- **No duplicate notifications or audit entries** — the read-only details endpoint creates no log entries.
- **Realtime updates** unaffected — no realtime publish calls in `gcash_payment_detail`.

## Commit

`fix: replace Windows-incompatible strftime codes in gcash_payment_detail`  
Commit hash: `f6ec6d4`
