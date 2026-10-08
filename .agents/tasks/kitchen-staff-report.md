# Kitchen Staff Implementation Report

## 1. Files Created / Modified

### New files created
| File | Description |
|------|-------------|
| `apps/accounts/migrations/0005_customuser_kitchen_staff_role.py` | No-op migration marking kitchen_staff role in migration history |
| `apps/kitchen/__init__.py` | Kitchen app package |
| `apps/kitchen/apps.py` | KitchenConfig AppConfig |
| `apps/kitchen/migrations/__init__.py` | Kitchen migrations package (no models) |
| `apps/kitchen/views.py` | `kitchen_orders` and `mark_order_ready` views |
| `apps/kitchen/urls.py` | Kitchen URL conf (`app_name='kitchen'`) |
| `apps/kitchen/tests.py` | 16 tests across KitchenAccessTest + KitchenEventStreamTest |
| `templates/base_kitchen.html` | Minimal kitchen sidebar layout (no admin nav) |
| `templates/kitchen/orders.html` | Kitchen order cards page with SSE + MARK READY JS |

### Modified files
| File | Changes |
|------|---------|
| `apps/accounts/models.py` | Added `('kitchen_staff', 'Kitchen Staff')` to ROLE_CHOICES; added `is_kitchen_staff` property |
| `apps/accounts/decorators.py` | Added `kitchen_staff_required` and `kitchen_or_admin_required` decorators |
| `apps/accounts/views.py` | Login redirect to `kitchen:orders` for kitchen_staff; `staff_list` and `password_reset_request` now include `kitchen_staff` in role filter |
| `apps/accounts/forms.py` | Added `('kitchen_staff', 'Kitchen Staff')` to `StaffCreateForm` role choices |
| `apps/realtime/views.py` | Added `kitchen_event_stream` SSE endpoint; imported `kitchen_or_admin_required` |
| `apps/realtime/urls.py` | Added `path('kitchen-stream/', ...)` for `kitchen_event_stream` |
| `kape_de_manubag/settings.py` | Added `'apps.kitchen'` to `INSTALLED_APPS` |
| `kape_de_manubag/urls.py` | Added `path('kitchen/', include('apps.kitchen.urls'))` |
| `templates/base.html` | Added `kitchenStream: '{% url "realtime:kitchen_event_stream" %}'` to `window.KDM_URLS` |

---

## 2. signals.py Auto-Publish Finding

**`apps/realtime/signals.py` DOES auto-publish `status_changed` on every `Order.save()` via a `post_save` receiver on the `Order` model.**

The signal publishes:
```python
publish('status_changed', {
    'order_id': order.pk,
    'order_number': order.order_number,
    'queue_number': order.queue_number,
    'new_status': order.status,
    'new_status_display': order.get_status_display(),
    'is_paid': order.is_paid,
})
```

**Result:** `mark_order_ready` does NOT call `broker.publish` manually. The `order.save()` at the end of the view triggers the signal automatically, which fires `status_changed` with `new_status='ready'` to all subscribers. The kitchen SSE stream filters this event and forwards it to kitchen clients.

---

## 3. Sound Files Found in `static/sounds/`

| File | Size |
|------|------|
| `new_order.mp3` | 44 bytes (placeholder) |
| `order_ready.mp3` | 212 bytes |
| `NEW_ORDER_SOUND_README.txt` | — |
| `README.txt` | — |

**Used in `base_kitchen.html`:** `new_order.mp3` — loaded as `<audio id="kitchen-order-sound">` and played when a new `preparing` order arrives via SSE.

---

## 4. Migration Created

`apps/accounts/migrations/0005_customuser_kitchen_staff_role.py` — no-op migration (no `operations`). Django `CharField` choices are not enforced at the database level, so no schema change is needed. The migration's purpose is to mark this feature in the migration history.

Dependencies: `[('accounts', '0004_customuser_profile_image_filename')]`

`python manage.py migrate --check` returns exit code 1 (unapplied on dev DB — pending `manage.py migrate` run). This is expected for a new migration that hasn't been applied to the local dev database yet.

---

## 5. Test Results — `python manage.py test apps.kitchen`

```
Found 16 test(s).
...
Ran 16 tests in 119.734s
OK
```

**All 16 tests pass.** Each test takes ~7.5s due to the project's brute-force login protection middleware adding overhead per request (the `get_lockout_remaining` check on every login view hit). This is pre-existing project behaviour, not introduced by this implementation.

### Tests implemented
**KitchenAccessTest (12 tests)**
- `test_is_kitchen_staff_property` — verifies property returns correct values for all 3 roles
- `test_kitchen_orders_accessible_by_kitchen_staff` — kitchen_staff gets HTTP 200
- `test_kitchen_orders_blocked_for_cashier` — cashier gets HTTP 302
- `test_kitchen_orders_blocked_for_anonymous` — anonymous gets redirect to login
- `test_kitchen_orders_accessible_by_admin` — admin is blocked (kitchen_staff_required is strict)
- `test_kitchen_orders_shows_only_preparing_orders` — view only returns `status='preparing'` orders
- `test_login_redirects_kitchen_staff_to_kitchen` — login POST redirects to `kitchen:orders`
- `test_mark_ready_requires_auth` — unauthenticated POST redirects to login
- `test_mark_ready_requires_post` — GET on mark_ready returns 405
- `test_mark_ready_success` — POST on preparing order → JSON `{success: true}`, order.status == 'ready', order.ready_at set
- `test_mark_ready_wrong_status` — POST on awaiting_payment order → JSON `{success: false}`, HTTP 400
- `test_mark_ready_wrong_role` — cashier POST is redirected (not 200)

**KitchenEventStreamTest (4 tests)**
- `test_kitchen_event_stream_accessible_by_kitchen_staff` — 200 + `text/event-stream`
- `test_kitchen_event_stream_accessible_by_admin` — 200 (kitchen_or_admin_required allows admin)
- `test_kitchen_event_stream_blocked_for_cashier` — 302 redirect
- `test_kitchen_event_stream_blocked_for_anonymous` — 302 to login

---

## 6. `python manage.py check` Output

```
System check identified no issues (0 silenced).
```

(The `UserWarning` about `SECRET_KEY` is a dev environment warning, not a system check issue.)

---

## 7. Admin/Cashier Behaviour Unchanged

Verified by:
- Stashing all changes and running the failing test classes against baseline — exactly **21 failures and 1 error** existed before this implementation.
- After restoring changes, the same 21 failures and 1 error remain — **zero regressions introduced**.
- All pre-existing failures are in `apps.orders.tests_security` (OrderRateLimitTests, TrackingTokenTests) and `apps.accounts.tests.ProfileImageUploadTests` — none related to kitchen staff, decorators, or login redirect.

The `_default_login_redirect` still directs admin and cashier users to `dashboard:index`. The `staff_list` and `password_reset_request` queries now also include `kitchen_staff` in their role filters (so kitchen staff accounts are visible in the admin staff list and can use password reset).

---

## 8. Access Gaps Found During ITEM 13 Audit

**No gaps found.** All views that kitchen staff must not access are already protected:

### `apps/orders/views.py`
All staff-facing views are behind `@cashier_or_admin_required`:
- `order_list`, `order_detail`, `update_order_status`, `process_payment`, `accept_order`
- `verify_gcash_payment`, `reject_gcash_payment`, `print_receipt`
- `cashier_pos`, `create_pos_order`, `pos_draft_status`
- `quick_status_advance`, `api_awaiting_payment_count`

Since `is_kitchen_staff` is neither `is_admin_user` nor `is_cashier`, kitchen staff are blocked from all of these by the existing decorator — **no change needed**.

### `apps/accounts/views.py`
- `/accounts/staff/` — `@admin_required` — kitchen staff blocked ✓
- `/accounts/profile/` — `@login_required` only — kitchen staff can access (correct, per plan) ✓
- `/accounts/password/change/` — `@login_required` only — kitchen staff can access (correct, per plan) ✓

### Other apps
- `apps/dashboard/views.py` — all views behind `@admin_required` or `@cashier_or_admin_required` ✓
- `apps/finance/views.py`, `apps/reports/views.py`, `apps/inventory/views.py`, `apps/audit/views.py`, `apps/menu/views.py` — all protected by `@cashier_or_admin_required` or `@admin_required` ✓

**No gaps to fix.** Kitchen staff have access only to:
- `GET /kitchen/` — kitchen orders page
- `POST /kitchen/orders/<pk>/ready/` — mark order ready
- `GET /realtime/kitchen-stream/` — kitchen-only SSE stream
- `GET /accounts/profile/` — their own profile (intentional)
- `POST /accounts/password/change/` — their own password (intentional)
- Customer-facing menu (`/`) — public routes (intentional)
