# Role-aware sidebar restriction for Kitchen Staff

A single-file change to `templates/base_admin.html` makes the shared admin layout
render a Kitchen-only sidebar when `user.is_kitchen_staff` is true, and the
unchanged full Admin/Cashier nav otherwise. The profile and change-password pages,
which both extend `base_admin.html`, inherit the restricted view automatically. The
Kitchen Orders link uses the existing Lucide `utensils` icon and the same
`request.resolver_match.app_name == 'kitchen'` active-state pattern already used
in `base_kitchen.html`. `base_kitchen.html` itself is untouched.

Watch for: (1) **confirmed** — the kitchen branch carries a duplicate `class="nav-icon"` on the `<i>` element (`<span class="nav-icon"><i data-lucide="utensils" class="nav-icon" ...>`), matching a pre-existing pattern across the whole file, so this is cosmetically harmless but worth noting for housekeeping; (2) **confirmed** — `brand-sub` is changed to "Kitchen" for Kitchen Staff, which is a minor scope creep beyond the sidebar nav requirement but aligns with `base_kitchen.html`'s own label.

**Verdict**: APPROVED

---

## High-level view

The condition `{% if user.is_kitchen_staff %}` is placed exclusively around the `<nav>` block inside `.sidebar-nav-wrapper`. The brand block, sidebar-user block (profile picture + logout), and mobile close/toggle buttons are outside the branch and render identically for all roles. The `{% else %}` branch is the verbatim original full nav — no items removed or reordered relative to the pre-existing file.

The topbar clipboard-list link (the mobile awaiting-orders icon) is also gated with `{% if not user.is_kitchen_staff %}`, correctly hiding a link Kitchen Staff cannot access without breaking the badge for Admin/Cashier.

`python manage.py check` passes with zero issues. The kitchen-specific test suite (16 tests) passes cleanly. The `is_kitchen_staff` property is a simple `role == 'kitchen_staff'` check on the custom user model — no new permission infrastructure was introduced.

---

<details>
<summary>Issues (2)</summary>

1. **Duplicate `nav-icon` class on `<i>` in kitchen branch** — `<span class="nav-icon"><i data-lucide="utensils" class="nav-icon" ...>` applies `nav-icon` to both the wrapping `<span>` and the inner `<i>`. This is a cosmetic pre-existing pattern across the entire file (all links do the same), so it causes no functional harm, but if the pattern is ever cleaned up file-wide it should be addressed consistently. No action required for this PR.

2. **`brand-sub` changed to "Kitchen" for Kitchen Staff** — the change replaces `<span class="brand-sub">Management System</span>` with a role branch showing "Kitchen" for Kitchen Staff and "Management System" otherwise. This is a reasonable UX choice and matches `base_kitchen.html`, but it is a scope addition beyond the sidebar-nav requirement in the task. Informational only; no action required.

</details>

---

<details>
<summary>Details</summary>

### Kitchen branch completeness

The kitchen nav renders exactly:

```
KITCHEN
🍳 Kitchen Orders  →  {% url 'kitchen:orders' %}
```

No other links or section titles are present. Active state uses `request.resolver_match.app_name == 'kitchen'`, the same expression used in `base_kitchen.html` — confirmed by direct comparison of both files.

The duplicate `class="nav-icon"` on the `<i>` tag (`<span class="nav-icon"><i data-lucide="utensils" class="nav-icon">`) mirrors the identical pattern on every other nav link in the file. It is pre-existing across the whole file, not introduced by this change.

### Admin/Cashier regression

The `{% else %}` branch carries all navigation items in original order: Dashboard, Order Management, POS Terminal, then `{% if user.is_admin_user %}`-gated Products / Categories / Inventory / Sales Reports / Staff Accounts / Settings / GCash Settings / Activity Log, then Finance, Customer Menu, Queue Board. The inner `is_admin_user` guard is preserved intact.

</details>

---

<details>
<summary>File map</summary>

| File | Change |
|---|---|
| `templates/base_admin.html` | Added `is_kitchen_staff` nav branch; gated topbar orders link; branched brand-sub label |
| `templates/base_kitchen.html` | Untouched |
| `templates/accounts/profile.html` | Untouched (inherits fix via `base_admin.html`) |
| `templates/accounts/change_password.html` | Untouched (inherits fix via `base_admin.html`) |
| `apps/accounts/models.py` | `is_kitchen_staff` property pre-exists; not modified |

</details>
