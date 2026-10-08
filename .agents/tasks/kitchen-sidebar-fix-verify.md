# Verification — Kitchen Staff Sidebar Fix

## `python manage.py check`

```
System check identified no issues (0 silenced).
```

Result: **PASS**

---

## `python manage.py test apps.kitchen`

```
Found 16 test(s).
Ran 16 tests in 132.272s
OK
```

Result: **PASS — 16/16**

---

## `python manage.py test` (full suite)

1699 tests discovered. Timed out at 10 minutes in this environment before
the run completed. No failures or errors were printed before the timeout.
The system check and kitchen-specific tests both pass cleanly.

---

## What was changed

Single file modified: `templates/base_admin.html`

### Change 1 — brand-sub role branch

`<span class="brand-sub">Management System</span>` was wrapped with a
role check so Kitchen Staff see "Kitchen" and all other roles see the
original "Management System".

### Change 2 — role-aware sidebar nav

The entire `<nav class="sidebar-nav"> … </nav>` block was wrapped:

```django
{% if user.is_kitchen_staff %}
  <nav> … Kitchen Orders only … </nav>
{% else %}
  <nav> … full Admin/Cashier nav (verbatim, unchanged) … </nav>
{% endif %}
```

Kitchen Staff see one section ("Kitchen") containing a single link
("Kitchen Orders") using the `utensils` Lucide icon, linking to
`{% url 'kitchen:orders' %}` with the same active-state logic pattern
(`request.resolver_match.app_name == 'kitchen'`) already used in
`base_kitchen.html`.

### Change 3 — topbar orders link guard

The topbar clipboard-list link (which navigates to `orders:order_list`
and holds the mobile awaiting-orders badge) was wrapped with
`{% if not user.is_kitchen_staff %}` so Kitchen Staff never see a link
to a page they are not authorized to access.

---

## Pages affected

| Page | Before | After |
|---|---|---|
| `/accounts/profile/` (Kitchen Staff) | Full Admin/Cashier sidebar | Kitchen-only sidebar |
| `/accounts/password-change/` (Kitchen Staff) | Full Admin/Cashier sidebar | Kitchen-only sidebar |
| `/kitchen/` (Kitchen Staff) | Already used `base_kitchen.html` — unchanged | Unchanged |
| Admin on any page | Full Admin sidebar | Unchanged |
| Cashier on any page | Full Cashier sidebar | Unchanged |

---

## No CSS hacks used

Navigation items are conditionally rendered server-side. No
`display:none` rules were added.
