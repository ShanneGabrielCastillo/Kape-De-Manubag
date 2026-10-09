# Implementation Plan — Kitchen Staff Role

## Exploration Findings

**Exact ROLE_CHOICES values:** `'admin'`, `'cashier'`, `'customer'` — add `'kitchen_staff'`  
**Role property naming convention:** `is_admin_user`, `is_cashier`, `is_customer` → new: `is_kitchen_staff`  
**Order status flow (NO 'accepted' status exists):**
```
awaiting_payment (is_paid=False)
    → cashier processes cash / verifies GCash: is_paid=True (status stays awaiting_payment)
    → cashier clicks Accept: status = 'preparing'   ← Kitchen receives order HERE
    → kitchen marks ready: status = 'ready'          ← Kitchen action
    → cashier completes: status = 'completed'
    cancelled (terminal)
```
**Kitchen receives orders at `status='preparing'`** — the cashier's "accept" moves `awaiting_payment → preparing`. No redundant `accepted` status needed.

**SSE broker:** `broker.publish(event_type: str, data: dict)` in `apps/realtime/broker.py`  
**status_changed signal:** fires automatically on every `Order.save()` via `apps/realtime/signals.py` — no manual publish needed in `mark_order_ready`  
**ready_at field:** already exists on `Order` model (DateTimeField, null=True)  
**Sounds:** `static/sounds/new_order.mp3` ✓ and `static/sounds/order_ready.mp3` ✓  
**Latest accounts migration:** `0004_customuser_profile_image_filename.py` → next is `0005`  
**Kitchen app:** does not exist yet  
**StaffCreateForm** has hardcoded `choices=[('cashier','Cashier'),('admin','Admin')]` — needs kitchen_staff added  
**staff_list** filters `role__in=['admin','cashier']` — needs kitchen_staff  
**password_reset_request** filters `role__in=['admin','cashier']` — needs kitchen_staff  
**base_admin.html:** existing staff sidebar template — Kitchen Staff gets a separate `base_kitchen.html`  
**Lucide version:** `lucide@0.469.0` — use `utensils` or `chef-hat` icon (both available at this version)

---

## Plan

- [ ] 1. **Add `kitchen_staff` to ROLE_CHOICES and add `is_kitchen_staff` property**  
      In `apps/accounts/models.py`: append `('kitchen_staff', 'Kitchen Staff')` to `ROLE_CHOICES` after `('cashier', 'Cashier')`. The `role` CharField's `max_length=20` comfortably fits `'kitchen_staff'` (13 chars). Add `is_kitchen_staff` property returning `self.role == 'kitchen_staff'`.  
      Files: `apps/accounts/models.py`  
      Verify: `python manage.py check` — no errors.

- [ ] 2. **Create no-op accounts migration 0005**  
      Create `apps/accounts/migrations/0005_customuser_kitchen_staff_role.py`. This migration has no `operations` (ROLE_CHOICES is not a DB constraint — Django CharField choices are not enforced at DB level). Dependencies: `[('accounts', '0004_customuser_profile_image_filename')]`. Its purpose is to mark this feature in migration history.  
      Files: `apps/accounts/migrations/0005_customuser_kitchen_staff_role.py`  
      Verify: `python manage.py migrate` — applies cleanly; `python manage.py showmigrations accounts` shows `0005` as applied.

- [ ] 3. **Add `kitchen_staff_required` and `kitchen_or_admin_required` decorators**  
      In `apps/accounts/decorators.py`: add two decorators following the exact `cashier_or_admin_required` pattern. `kitchen_staff_required`: unauthenticated → `redirect_to_login`, wrong role → `messages.error` + `redirect('menu:index')`. `kitchen_or_admin_required`: allows `is_kitchen_staff` OR `is_admin_user`; wrong role → `messages.error` + `redirect('menu:index')`.  
      Files: `apps/accounts/decorators.py`  
      Verify: `python manage.py test apps.accounts` — all pass.

- [ ] 4. **Update login redirect, staff list query, password reset query, and StaffCreateForm**  
      In `apps/accounts/views.py`: update `_default_login_redirect` to add `elif user.is_kitchen_staff: return 'kitchen:orders'` before `return 'menu:index'`. Update `staff_list` view filter to `role__in=['admin', 'cashier', 'kitchen_staff']`. Update `password_reset_request` filter to same.  
      In `apps/accounts/forms.py`: update `StaffCreateForm.role` choices to `[('cashier','Cashier'),('admin','Admin'),('kitchen_staff','Kitchen Staff')]`.  
      Files: `apps/accounts/views.py`, `apps/accounts/forms.py`  
      Verify: `python manage.py test apps.accounts` — all pass.

- [ ] 5. **Create the `apps/kitchen` Django app**  
      Create: `apps/kitchen/__init__.py`, `apps/kitchen/apps.py` (KitchenConfig, `name='apps.kitchen'`), `apps/kitchen/views.py`, `apps/kitchen/urls.py`, `apps/kitchen/migrations/__init__.py`. No models.  
      `apps/kitchen/views.py`:  
      - `kitchen_orders(request)`: `@login_required @kitchen_staff_required` — queries `Order.objects.filter(status='preparing').prefetch_related('items').order_by('queued_at','created_at')`, renders `'kitchen/orders.html'` with `{'orders': orders}`.  
      - `mark_order_ready(request, pk)`: `@login_required @kitchen_staff_required @require_POST` — `transaction.atomic()` + `select_for_update()`, validates `order.status == 'preparing'`, calls `validate_status_transition('preparing','ready')`, sets `order.ready_at = timezone.now()`, `order.status = 'ready'`, `order.save()`, calls `log_action(request.user, 'order.mark_ready', order, detail='Kitchen staff marked order as ready.')`, returns `JsonResponse({'success': True, 'order_number': order.order_number, 'new_status': 'ready'})`. On `ValueError`: `JsonResponse({'success': False, 'error': str(e)}, status=400)`.  
      `apps/kitchen/urls.py`: `app_name='kitchen'`, paths: `''` → `kitchen_orders` (name='orders'), `'<int:pk>/ready/'` → `mark_order_ready` (name='mark_ready').  
      Files: `apps/kitchen/__init__.py`, `apps/kitchen/apps.py`, `apps/kitchen/views.py`, `apps/kitchen/urls.py`, `apps/kitchen/migrations/__init__.py`  
      Verify: `python manage.py check` — no errors.

- [ ] 6. **Register `apps.kitchen` in INSTALLED_APPS and include kitchen URLs**  
      In `kape_de_manubag/settings.py`: add `'apps.kitchen'` to `INSTALLED_APPS` after `'apps.chatbot'`.  
      In `kape_de_manubag/urls.py`: add `path('kitchen/', include('apps.kitchen.urls'))` to `urlpatterns`.  
      Files: `kape_de_manubag/settings.py`, `kape_de_manubag/urls.py`  
      Verify: `python manage.py check` — no errors; `python manage.py showmigrations` — kitchen shows no migrations (no models).

- [ ] 7. **Add `kitchen_event_stream` SSE endpoint to realtime app**  
      In `apps/realtime/views.py`: add `kitchen_event_stream(request)` decorated with `@login_required @kitchen_or_admin_required`. It follows the same `subscribe()/unsubscribe()` pattern as `event_stream()`. The inner `stream()` generator filters broker events: forwards `status_changed` only when `event['data'].get('new_status') in ('preparing', 'ready')` (new order for kitchen, or order just marked ready); forwards `heartbeat` as-is. Drops all other event types (`new_order`, `payment_confirmed`, `order_accepted`, `inventory_changed`, `gcash_submitted`) — kitchen clients must never see financial data.  
      In `apps/realtime/urls.py`: add `path('kitchen-stream/', views.kitchen_event_stream, name='kitchen_event_stream')`.  
      Files: `apps/realtime/views.py`, `apps/realtime/urls.py`  
      Verify: `python manage.py test apps.realtime` — all pass; `python manage.py check` — no errors.

- [ ] 8. **Add `kitchenStream` URL to the `window.KDM_URLS` JS object in `templates/base.html`**  
      In `templates/base.html`, in the `<script>` block defining `window.KDM_URLS`, add `kitchenStream: '{% url "realtime:kitchen_event_stream" %}'`. This makes the URL available to the kitchen page JS without hardcoding.  
      Files: `templates/base.html`  
      Verify: `python manage.py check` — no template errors; rendered HTML includes the kitchenStream key.

- [ ] 9. **Create `templates/base_kitchen.html` — the kitchen sidebar layout**  
      Create `templates/base_kitchen.html` extending `base.html`. This is the kitchen equivalent of `base_admin.html`. It uses the same `.sidebar`, `.app-layout`, `.main-content`, `.topbar` structural CSS classes. The sidebar contains:  
      - Brand block (same `.sidebar-brand` with ☕ logo and "Kape De Manubag" text)  
      - A single nav section labeled "KITCHEN" with one link: `{% url 'kitchen:orders' %}` with Lucide icon `utensils` (available in lucide@0.469.0), label "Kitchen Orders", active class when `request.resolver_match.app_name == 'kitchen'`  
      - `.sidebar-user` block (same as `base_admin.html`: avatar, name, role, logout link)  
      No topbar order badge. No `data-realtime="true"` on body (kitchen uses its own SSE stream).  
      The `realtime_js` block loads: `<audio id="kitchen-order-sound" preload="auto"><source src="{% static 'sounds/order_ready.mp3' %}" type="audio/mpeg"></audio>` and `<script src="{% static 'js/realtime.js' %}"></script>` — realtime.js is still needed for `showToast` and `getCookie` helpers used by the kitchen JS. The kitchen JS itself lives in the child template's `extra_js` block.  
      Files: `templates/base_kitchen.html`  
      Verify: Template renders without `TemplateSyntaxError` (`python manage.py check --deploy` or load in dev server).

- [ ] 10. **Create `templates/kitchen/orders.html` — the kitchen order card page**  
       Create `templates/kitchen/` directory and `templates/kitchen/orders.html` extending `base_kitchen.html`.  
       `{% block page_title %}Kitchen Orders{% endblock %}`  
       Content block: renders a `.kitchen-grid` div. For each order in `orders`: a `.card .kitchen-card` div with `data-order-id="{{ order.pk }}"`. Each card shows:  
       - Order number (prominent: `{{ order.order_number }}`)  
       - Customer name (`{{ order.customer_name }}`)  
       - Order type badge: `{{ order.get_order_type_display }}` using existing `.badge-dine-in` / `.badge-takeout` classes (or create these if not in main.css — check with grep first; if absent use inline styles)  
       - Order time: `{{ order.created_at|date:"g:i A" }}`  
       - Notes: `{% if order.notes %}<p class="order-notes">📝 {{ order.notes }}</p>{% endif %}`  
       - Items list: `{% for item in order.items.all %}<li>{{ item.quantity }} × {{ item.product_name }}{% if item.size != 'none' %} ({{ item.get_size_display }}){% endif %}{% if item.notes %} — {{ item.notes }}{% endif %}</li>{% endfor %}`  
       - MARK READY button: `<button class="btn btn-success mark-ready-btn" data-order-id="{{ order.pk }}" data-url="{% url 'kitchen:mark_ready' order.pk %}">MARK READY</button>`  
       If `orders` is empty: empty-state div with message "No orders to prepare right now ✅".  
       `{% block extra_css %}` with kitchen-specific styles: `.kitchen-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1rem}`, `.kitchen-card{border-left:4px solid var(--caramel)}`, `.order-items-list{list-style:none;padding:0;margin:0}`, `.btn-success{background:#198754;color:#fff;border:none}`, `.btn-success:hover{background:#157347}`, `@keyframes slideInCard{from{opacity:0;transform:translateY(-12px)}to{opacity:1;transform:none}}`, `.new-arrival{animation:slideInCard .4s ease}`.  
       `{% block extra_js %}` with inline `<script>` for realtime and MARK READY (see step 11).  
       Files: `templates/kitchen/orders.html`  
       Verify: `python manage.py check` — no errors. Load page in browser as kitchen_staff user — cards render correctly.

- [ ] 11. **Add kitchen realtime JavaScript in `templates/kitchen/orders.html` extra_js block**  
       Inline `<script>` in the `extra_js` block of `templates/kitchen/orders.html`:  
       ```js
       document.addEventListener('DOMContentLoaded', function() {
         // SSE connection to kitchen-only stream
         const ksUrl = window.KDM_URLS && window.KDM_URLS.kitchenStream;
         if (!ksUrl) return;
         const ks = new EventSource(ksUrl);
         ks.addEventListener('status_changed', function(e) {
           const data = JSON.parse(e.data);
           if (data.new_status === 'preparing') {
             // New order arrived for kitchen
             const audio = document.getElementById('kitchen-order-sound');
             if (audio) audio.play().catch(()=>{});
             if (typeof showToast === 'function')
               showToast('🍳 New order: #' + data.order_number, 'info', 5000);
             setTimeout(() => location.reload(), 800);
           } else if (data.new_status === 'ready') {
             // Order marked ready — remove its card from DOM
             const card = document.querySelector('[data-order-id="' + data.order_id + '"]');
             if (card) card.remove();
             if (typeof showToast === 'function')
               showToast('✅ Order #' + data.order_number + ' is Ready', 'success');
           }
         });
         ks.addEventListener('heartbeat', function() { /* keep-alive */ });
         ks.onerror = function() {
           console.warn('Kitchen SSE disconnected — browser will reconnect');
         };

         // MARK READY button handlers
         document.querySelectorAll('.mark-ready-btn').forEach(function(btn) {
           btn.addEventListener('click', function() {
             const url = this.dataset.url;
             const orderId = this.dataset.orderId;
             const card = document.querySelector('[data-order-id="' + orderId + '"]');
             this.disabled = true;
             this.textContent = 'Marking…';
             fetch(url, {
               method: 'POST',
               headers: { 'X-CSRFToken': getCookie('csrftoken') },
             })
             .then(r => r.json())
             .then(data => {
               if (data.success) {
                 if (card) card.remove();
                 if (typeof showToast === 'function')
                   showToast('✅ Order #' + data.order_number + ' marked Ready!', 'success');
               } else {
                 if (typeof showToast === 'function')
                   showToast(data.error || 'Could not mark ready', 'error');
                 this.disabled = false;
                 this.textContent = 'MARK READY';
               }
             })
             .catch(() => {
               if (typeof showToast === 'function')
                 showToast('Network error — try again', 'error');
               this.disabled = false;
               this.textContent = 'MARK READY';
             });
           });
         });
       });
       ```  
       Files: `templates/kitchen/orders.html` (modify the extra_js block added in step 10)  
       Verify: In browser as kitchen_staff, click MARK READY on a preparing order — card disappears, toast shown, order status updates to 'ready' in DB.

- [ ] 12. **Write tests in `apps/kitchen/tests.py`**  
       Create `apps/kitchen/tests.py`. Tests:  
       - `kitchen_orders_requires_kitchen_staff_role`: GET /kitchen/ as anonymous → 302 to login; as cashier → redirect with error; as admin → redirect with error; as kitchen_staff → 200  
       - `kitchen_orders_shows_only_preparing_orders`: create orders in all statuses; GET /kitchen/ as kitchen_staff → only preparing orders appear  
       - `mark_order_ready_success`: POST /kitchen/<pk>/ready/ as kitchen_staff on preparing order → 200 JSON success, order.status=='ready', order.ready_at is set  
       - `mark_order_ready_wrong_status`: POST on awaiting_payment order → 400 error  
       - `mark_order_ready_requires_auth`: POST as anonymous → 302 to login  
       - `mark_order_ready_wrong_role`: POST as cashier → redirect (not 200)  
       - `login_redirects_kitchen_staff_to_kitchen`: login as kitchen_staff → redirect to kitchen:orders  
       Use `django.test.TestCase`, `self.client.login(...)`, `CustomUser.objects.create_user(..., role='kitchen_staff')`.  
       Files: `apps/kitchen/tests.py`  
       Verify: `python manage.py test apps.kitchen` — all tests pass; `python manage.py test` — full suite passes.

- [ ] 13. **Server-side access enforcement audit and verification**  
       Confirm that existing `@cashier_or_admin_required` and `@admin_required` decorators on the following views already block kitchen_staff (they check `is_admin_user` or `is_cashier`, which kitchen_staff has neither):  
       - `apps/orders/views.py`: `order_list`, `order_detail`, `update_order_status`, `process_payment`, `accept_order`, `verify_gcash_payment`, `reject_gcash_payment`, `print_receipt`, `cashier_pos`, `create_pos_order`, `quick_status_advance`, `api_awaiting_payment_count`  
       - `apps/dashboard/views.py` (all views behind `@admin_required` or `@cashier_or_admin_required`)  
       - `apps/finance/views.py`, `apps/reports/views.py`, `apps/inventory/views.py`, `apps/audit/views.py`, `apps/menu/views.py`  
       If any protected view is missing a decorator, add the appropriate one.  
       Files: audit only; fix any gaps found  
       Verify: `python manage.py test` — full suite passes.

---

## Architecture Decisions

**No separate auth system:** Kitchen Staff uses the same `CustomUser` model with a new `role` value. The existing Django session and `login_required` decorator protect all kitchen views.

**No redundant `accepted` status:** The existing flow `awaiting_payment → preparing` already represents "cashier accepted the order." Kitchen receives it at `preparing`. Adding a new `accepted` status would break the transition map and all existing tests.

**`mark_order_ready` does not call `broker.publish` directly:** The `apps/realtime/signals.py` `post_save` receiver fires on every `Order.save()` and publishes `status_changed` to all subscribers automatically. The kitchen stream SSE endpoint filters this event by `new_status`. No duplicate event publishing.

**Separate kitchen SSE stream (`/realtime/kitchen-stream/`):** The existing `/realtime/stream/` requires `@cashier_or_admin_required`. A new endpoint filtered to `status_changed(preparing/ready)` + `heartbeat` serves kitchen clients without exposing financial events (`new_order` with totals, `payment_confirmed`, `gcash_submitted`).

**`base_kitchen.html` not `base_admin.html`:** Kitchen Staff must never see the Admin/Cashier sidebar — not just have links hidden, but the entire nav structure must be different. A separate base template guarantees this.

**Order cards use `prefetch_related('items')` not `order.items.all()` in template:** Avoids N+1 queries when rendering multiple cards.

**Page reload on new order arrival (SSE `preparing` event):** Rather than injecting a new card via a partial render AJAX call, the kitchen page reloads on new order arrival. This avoids a full client-side rendering pipeline while still giving sub-second realtime notification. The SSE triggers sound + toast immediately, then reloads after 800ms so the user always sees a fresh, server-authoritative list.
