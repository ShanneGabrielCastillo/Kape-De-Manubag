# Implementation Plan — Kitchen Staff Sidebar Fix

## Findings from exploration

### Root cause

`templates/accounts/profile.html` (and `templates/accounts/change_password.html`)
extend `base_admin.html`, which renders the full Admin/Cashier sidebar with **no role
check**. The previously created `templates/base_kitchen.html` has the correct
restricted sidebar, but it is only used by `templates/kitchen/orders.html`.
Any page that extends `base_admin.html` therefore shows the full sidebar to every
authenticated user, including Kitchen Staff.

### Key facts confirmed from code

| Item | Value |
|---|---|
| Property on `CustomUser` for kitchen role check | `user.is_kitchen_staff` → `return self.role == 'kitchen_staff'` |
| URL name for kitchen orders | `kitchen:orders` (app_name `kitchen`, name `orders`) |
| Active-state logic used in `base_kitchen.html` | `{% if request.resolver_match.app_name == 'kitchen' %}active{% endif %}` |
| `brand-sub` text in `base_kitchen.html` | `Kitchen` |
| `brand-sub` text in `base_admin.html` | `Management System` |
| Kitchen nav icon used in `base_kitchen.html` | `utensils` (Lucide) |
| Templates that extend `base_admin.html` (full list) | see section below |

### All templates that extend `base_admin.html`

These are the **only** templates where a Kitchen Staff user can ever see a sidebar,
because every other authenticated page either extends `base.html` (no sidebar),
`base_kitchen.html` (already correct), or is blocked by server-side auth before
rendering:

```
templates/accounts/profile.html          ← Kitchen Staff visits this
templates/accounts/change_password.html  ← Kitchen Staff visits this
templates/dashboard/index.html
templates/dashboard/settings.html
templates/dashboard/gcash_settings.html
templates/orders/order_list.html
templates/orders/order_detail.html
templates/orders/pos.html
templates/finance/index.html
templates/finance/history.html
templates/inventory/list.html
templates/inventory/log.html
templates/reports/index.html
templates/audit/activity_log.html
templates/menu/product_list.html
templates/menu/product_form.html
templates/menu/category_list.html
templates/menu/category_form.html
templates/accounts/staff_list.html
templates/accounts/staff_form.html
```

Password-reset pages (`password_reset_request.html`, `password_reset_done.html`,
`password_reset_confirm.html`) extend `base.html` (not `base_admin.html`), so they
render no sidebar at all — they are unaffected by this fix.

---

## The fix — one item only

### Why a single change in `base_admin.html` is sufficient

The server-side auth decorators already block Kitchen Staff from every page except
`kitchen:orders`, `accounts:profile`, and `accounts:password_change`. Those three
pages all use the same sidebar layout mechanism:

- `kitchen:orders` → extends `base_kitchen.html` (already correct — no change needed)
- `accounts:profile` → extends `base_admin.html` (broken — needs fix)
- `accounts:change_password` → extends `base_admin.html` (broken — needs fix)

Patching `base_admin.html` once fixes both profile and change-password, and
transitively fixes any future page that extends `base_admin.html` and is someday made
accessible to Kitchen Staff, at no extra cost.

---

- [ ] 1. Add a Kitchen Staff nav branch at the top of the sidebar `<nav>` in
         `templates/base_admin.html`.

  **What to do:**

  Immediately before the first `<div class="nav-section-title">Overview</div>` line
  (which is the very first child of `<nav class="sidebar-nav">`), insert an
  `{% if user.is_kitchen_staff %} … {% else %}` block that renders the Kitchen-only
  nav when the logged-in user is Kitchen Staff, and falls through to the existing full
  nav otherwise. Close with `{% endif %}` after the closing `</nav>` tag (before the
  closing `</div><!-- /.sidebar-nav-wrapper -->`).

  Also update the `brand-sub` span inside the same `{% if %}` branch so that Kitchen
  Staff see "Kitchen" rather than "Management System".

  **Exact insertion point:**

  ```html
  <!-- BEFORE (existing line, use as anchor): -->
      <div class="nav-section-title">Overview</div>
  ```

  **Exact template snippet to insert** (replace the entire `<nav class="sidebar-nav">
  … </nav>` block with this):

  ```django
  {# ── Role-aware sidebar nav ─────────────────────────────────────────────── #}
  {% if user.is_kitchen_staff %}
  {# ── KITCHEN STAFF: restricted nav — Kitchen Orders only ────────────────── #}
  <nav class="sidebar-nav">
    <div class="nav-section-title">Kitchen</div>
    <a href="{% url 'kitchen:orders' %}"
       class="sidebar-link {% if request.resolver_match.app_name == 'kitchen' %}active{% endif %}"
       aria-label="Kitchen Orders">
      <span class="nav-icon"><i data-lucide="utensils" class="nav-icon" aria-hidden="true"></i></span>
      <span class="nav-label">Kitchen Orders</span>
      <span class="sidebar-tooltip" aria-hidden="true">Kitchen Orders</span>
    </a>
  </nav>
  {% else %}
  {# ── ADMIN / CASHIER: full nav (unchanged) ───────────────────────────────── #}
  <nav class="sidebar-nav">
    <div class="nav-section-title">Overview</div>
    … (keep entire existing nav block verbatim) …
  </nav>
  {% endif %}
  ```

  **`brand-sub` update:**

  The `<span class="brand-sub">` must also reflect the role. Wrap it with the same
  condition inside the existing `.sidebar-brand` `<div class="brand-text">` block:

  ```django
  <div class="brand-text">
    <span class="brand-name">Kape De Manubag</span>
    {% if user.is_kitchen_staff %}
      <span class="brand-sub">Kitchen</span>
    {% else %}
      <span class="brand-sub">Management System</span>
    {% endif %}
  </div>
  ```

  **Files to modify:**
  - `templates/base_admin.html` — the only file changed by this plan

  **No other files are touched.** `base_kitchen.html`, all views, models, URLs,
  CSS, Admin nav, and Cashier nav are left exactly as they are.

  **Exact active-state condition for Kitchen Orders** (matching the pattern already
  used in `base_kitchen.html`):

  ```django
  {% if request.resolver_match.app_name == 'kitchen' %}active{% endif %}
  ```

  This evaluates to `active` on the kitchen orders page and on any future kitchen
  sub-page that shares the `kitchen` app namespace (e.g. a future kitchen detail
  page), which is the correct behavior.

  **Topbar orders-link (mobile badge):** The topbar inside `base_admin.html` contains
  an `<a href="{% url 'orders:order_list' %}">` with an awaiting-orders badge.
  Kitchen Staff must not see that link. Wrap it with the same role check:

  ```django
  {% if not user.is_kitchen_staff %}
  <a href="{% url 'orders:order_list' %}"
     id="topbar-orders-link"
     ...>
    ...
  </a>
  {% endif %}
  ```

  **Verify:**

  ```
  python manage.py check
  ```

  Expected: `System check identified no issues (0 silenced).`

  Then manually:
  1. Log in as Kitchen Staff → `/accounts/profile/` → sidebar shows only **Kitchen Orders** under section heading **KITCHEN**; brand subtitle reads **Kitchen**
  2. Log in as Kitchen Staff → `/kitchen/` → same restricted sidebar
  3. Log in as Kitchen Staff → `/accounts/password-change/` → same restricted sidebar
  4. Log in as Admin → any page → Admin sidebar is unchanged
  5. Log in as Cashier → any page → Cashier sidebar is unchanged
  6. Log in as Kitchen Staff → attempt `/dashboard/` → 403 / redirect (server-side auth, unaffected by this change)
  7. Mobile: at 375 px viewport, sidebar drawer shows only Kitchen Orders for Kitchen Staff

---

## What NOT to do (restatement for the implementer)

- Do not add `display:none` CSS to hide nav links from Kitchen Staff.
- Do not change `base_kitchen.html` — it is already correct.
- Do not create separate `profile_kitchen.html` or `change_password_kitchen.html` templates.
- Do not modify any view, model, URL, permission decorator, or business logic.
- Do not alter the Admin sidebar items.
- Do not alter the Cashier sidebar items.
- The `{% else %}` branch of the new condition must contain the **complete, verbatim**
  existing `<nav class="sidebar-nav"> … </nav>` block. Nothing in that branch changes.
