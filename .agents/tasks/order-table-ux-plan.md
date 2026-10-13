# Implementation Plan — Order Management Table UX Improvements

## Findings from Exploration

### Template
**File:** `templates/orders/order_list.html`

This is the only Order Management template. It extends `base_admin.html`. It has two rendering paths:
1. **Desktop (≥ 768px):** A standard `<table class="table-orders">` with `<tbody>` rows, server-rendered by Django and re-rendered in-place by JavaScript for realtime events.
2. **Mobile (≤ 767px):** A custom card UI built by `buildOrderCard()` / `buildOrderCards()` — a self-contained IIFE at the bottom of the template's `{% block extra_js %}`. This function reads `row.dataset.*` attributes from the hidden `<tr>` elements and constructs `.om-card` elements. It runs on `DOMContentLoaded`, on window resize, and is also wired into `initResponsiveTables()` so it rebuilds after every realtime row insert.

### URL and View
- **URL name:** `orders:order_detail`
- **URL pattern:** `path('manage/<int:pk>/', views.order_detail, name='order_detail')`
- **kwarg:** `pk` (integer — the Order primary key)
- **Template tag:** `{% url 'orders:order_detail' order.pk %}`
- **View decorator:** `@login_required @cashier_or_admin_required` — access is already restricted to Cashier/Admin roles. No change needed here.

### Existing order-number link
The `<td>` in the desktop table already renders the order number as an `<a>` tag:
```html
<td><a href="{% url 'orders:order_detail' order.pk %}" style="font-weight:700;color:var(--caramel)">{{ order.order_number }}</a></td>
```
The orange/brown color (`var(--caramel)` = `#C17A3B`) is already applied via an inline style. No separate CSS class exists for this element.

Similarly, the `new_order` SSE handler that inserts rows programmatically already renders the link correctly:
```js
row.innerHTML = `<td><a href="${detailUrl}" style="font-weight:700;color:var(--caramel)">${escHtml(order.order_number)}</a></td> ...`
```

And `buildOrderCard()` in the mobile card builder already renders a link:
```js
`<a href="${escHtml(d.detailUrl || '')}" class="om-number">...</a>`
```

So **the order-number link already exists** on all three rendering paths. The only missing pieces are: a hover/focus style on that link, removing the View button, and adding row-click navigation.

### Current Actions Column (desktop)
```html
<td>
  <div style="display:flex;gap:4px;flex-wrap:wrap">
    <a href="{% url 'orders:order_detail' order.pk %}" class="btn btn-sm btn-outline">View</a>
    <a href="{% url 'orders:print_receipt' order.pk %}" class="btn btn-sm btn-outline" target="_blank">
      <i data-lucide="printer" ...></i>
    </a>
    {% if order.next_status %}
    <button onclick="quickAdvance(...)" ...>▶ {{ order.next_status|title }}</button>
    {% endif %}
  </div>
</td>
```
The View button appears in two places: the server-rendered `{% for order in orders %}` loop, and the JavaScript `actionCellHtml` string used by the `new_order` SSE handler (appears twice: `isAwaitingPayment` branch and the else branch).

### Current Actions Column (mobile cards)
The `buildOrderCard()` function renders a `viewBtn` variable:
```js
const viewBtn = `<a href="${escHtml(d.detailUrl || '')}" class="btn btn-sm btn-outline">View</a>`;
```
And includes it in:
```js
const actionsHtml = `
  <div class="om-actions${...}">
    ${viewBtn}
    ${printBtn}
    ${advanceBtn}
  </div>`;
```
The `actionCount` variable drives the grid column count (`om-actions--2` class for 2 buttons). This must be updated when `viewBtn` is removed.

### CSS Framework
**No Tailwind.** The project uses plain CSS in `static/css/main.css` with CSS custom properties (`--caramel`, `--brown-dark`, etc.). All new styles go in the template's `{% block extra_css %}` `<style>` block (consistent with existing mobile card styles in this template).

### Existing row hover
`main.css` line 265:
```css
tbody tr:hover { background: rgba(193, 122, 59, 0.04); }
```
This very subtle hover already exists on all table rows. The clickable-row implementation will enhance it slightly for rows that have a `data-detail-url`.

### Realtime row insertion — JS functions that re-render rows
The following are the critical JS functions/event handlers that insert or rebuild order rows. The row-click delegation handler must survive all of them:

1. **`RealtimeConnection.on('new_order', ...)`** — inserts a new `<tr>` by calling `tbody.prepend(row)`, then calls `window.initResponsiveTables()` (which calls `buildOrderCards()`).
2. **`RealtimeConnection.on('status_changed', ...)`** — updates cells in an existing row in-place; calls `window.initResponsiveTables()` at the end.
3. **`RealtimeConnection.on('gcash_submitted', ...)`** — updates cells in an existing row in-place; calls `window.initResponsiveTables()` at the end.
4. **`buildOrderCards()`** (IIFE at bottom of template) — rebuilds all `.om-cards` div elements from `<tr data-order-id>` data attributes. Mobile-only.
5. **`window.initResponsiveTables`** — monkey-patched to call `buildOrderCards()` after calling the original. Called from multiple realtime handlers.

**Event delegation strategy:** Attach one `click` listener to the `<tbody>` (not to individual rows). On each click, walk up from `event.target` to the nearest `<tr>` with `data-detail-url`. Skip the click if the event target is inside an `<a>` tag, a `<button>`, a `<form>`, or a `<select>`. This single delegated handler naturally covers all rows, including those prepended by the `new_order` SSE handler — no re-attachment needed.

---

## Changes to Make

### Change 1 — Remove the View button (3 locations)

**What:** Delete the `<a href="... order_detail ...">View</a>` button from the Actions column. It appears in three places in `templates/orders/order_list.html`:

1. The Django template loop (`{% for order in orders %}`) — the static rendered rows.
2. The `actionCellHtml` string in the `isAwaitingPayment` branch of the `new_order` SSE handler.
3. The `actionCellHtml` string in the `else` branch of the `new_order` SSE handler.

Also remove `viewBtn` from `buildOrderCard()` and update the `actionCount` variable from `advanceBtn ? 3 : 2` to `advanceBtn ? 2 : 1` (since with `viewBtn` gone, there are 2 buttons with an advance button or 1 without). Update the `om-actions` div class logic accordingly: use `om-actions--1` for 1 button and `om-actions--2` for 2 buttons (the print button is always present; the advance button is conditional).

> Note: The print receipt button and quick-advance button are kept exactly as-is.

### Change 2 — Add hover + focus-visible style to the order-number link

**What:** The order-number `<a>` already has `style="font-weight:700;color:var(--caramel)"`. Add a CSS rule in the template's `{% block extra_css %}` `<style>` block targeting this link by a new class `order-num-link`. Add that class to the `<a>` in the Django template loop and in the `new_order` SSE handler's `row.innerHTML`. (The mobile card's `.om-number` already has its own CSS in the template's style block.)

The new CSS:
```css
.order-num-link {
  font-weight: 700;
  color: var(--caramel);
  text-decoration: none;
  border-radius: 3px;
  transition: color 0.15s;
}
.order-num-link:hover {
  color: var(--brown-warm);
  text-decoration: underline;
}
.order-num-link:focus-visible {
  outline: 2px solid var(--caramel);
  outline-offset: 2px;
}
```

Replace the inline `style="font-weight:700;color:var(--caramel)"` on both `<a>` tags (template loop + SSE handler) with `class="order-num-link"`. The `.om-number` class in the mobile card builder already handles the mobile link — no change needed there beyond the existing `.om-number:hover` style (add one if not present).

### Change 3 — Make entire row clickable via event delegation

**What:** Add a single delegated click handler on `document` (or on the `.table-orders tbody`) that navigates to `row.dataset.detailUrl` when the user clicks on a non-interactive part of a `<tr>` that has `data-detail-url`. Add `cursor: pointer` and a slightly more visible hover background to rows that have `data-detail-url`.

**Implementation in the template's `{% block extra_js %}` `<script>` block** (after the IIFE, at the bottom):

```javascript
// ── Clickable order rows (row-level navigation) ───────────────────────────
// Uses event delegation on the table body so dynamically inserted rows
// (new_order SSE) are covered without re-attaching handlers.
(function () {
  const tbody = document.querySelector('.table-orders tbody');
  if (!tbody) return;

  tbody.addEventListener('click', function (e) {
    // Walk up to the nearest <tr> that has a detail URL.
    const row = e.target.closest('tr[data-detail-url]');
    if (!row) return;

    // Ignore clicks on interactive elements — let them handle themselves.
    if (e.target.closest('a, button, input, select, textarea, form, label')) return;

    const url = row.dataset.detailUrl;
    if (url) window.location.href = url;
  });
}());
```

**CSS** (add to `{% block extra_css %}` `<style>` block):
```css
/* Desktop: clickable row cursor + slightly stronger hover */
@media (min-width: 768px) {
  .table-orders tbody tr[data-detail-url] {
    cursor: pointer;
  }
  .table-orders tbody tr[data-detail-url]:hover {
    background: rgba(193, 122, 59, 0.08);
  }
}
/* Mobile: pressed/active state for the card (already navigable via om-number link) */
@media (max-width: 767.98px) {
  .om-card[data-order-id] {
    /* Cards are not made row-clickable — the om-number link and action buttons
       already cover all interactions. Active state provides tactile feedback. */
    transition: box-shadow 0.15s, background 0.15s;
  }
}
```

> Mobile cards are NOT made wholesale clickable (consistent with the requirement: "Do not make the entire table row a fake link using inaccessible clickable markup"). The `.om-number` link inside each card is already a genuine accessible link.

---

## Plan Items

- [ ] 1. **Remove the View button from all three locations in `templates/orders/order_list.html`.**

  - In the `{% for order in orders %}` template loop, delete the line:
    ```html
    <a href="{% url 'orders:order_detail' order.pk %}" class="btn btn-sm btn-outline">View</a>
    ```
  - In the `new_order` SSE handler's `isAwaitingPayment` branch (`actionCellHtml`), remove:
    ```js
    <a href="${detailUrl}" class="btn btn-sm btn-outline">View</a>
    ```
  - In the same handler's `else` branch (`actionCellHtml`), remove the same `View` anchor.
  - In `buildOrderCard()`, remove the `viewBtn` variable declaration and its use in `actionsHtml`.
  - Update `actionCount` in `buildOrderCard()`: change `advanceBtn ? 3 : 2` → `advanceBtn ? 2 : 1`.
  - Update the `om-actions` class on the div: use `om-actions--1` when there is no advance button (print-only), `om-actions--2` when there is an advance button. Also add a CSS rule for `om-actions--1` in the `{% block extra_css %}` `<style>` block so the lone print button spans full width on mobile.

  **Files:** `templates/orders/order_list.html`

  **Verify:** `python manage.py check` — zero issues. Open `http://127.0.0.1:8000/orders/manage/` in a browser and confirm the View button is gone from the Actions column. Confirm the print icon and (where applicable) the quick-advance button remain.

- [ ] 2. **Add `order-num-link` class and CSS to the order-number link.**

  - In `{% block extra_css %}` `<style>`, add the `.order-num-link` rules (hover color, underline, focus-visible outline).
  - In the Django template loop's `<td>`, replace `style="font-weight:700;color:var(--caramel)"` on the `<a>` with `class="order-num-link"`.
  - In the `new_order` SSE handler's `row.innerHTML`, replace `style="font-weight:700;color:var(--caramel)"` with `class="order-num-link"` on the order-number `<a>`.

  **Files:** `templates/orders/order_list.html`

  **Verify:** `python manage.py check` — zero issues. In the browser, hover over an order number and confirm it shows underline + darker caramel color. Tab to it with keyboard and confirm a visible focus ring appears.

- [ ] 3. **Add delegated row-click handler and cursor/hover CSS.**

  - In `{% block extra_css %}` `<style>`, add the `@media (min-width: 768px)` rule for `.table-orders tbody tr[data-detail-url]` — `cursor: pointer` and slightly stronger hover background (`rgba(193, 122, 59, 0.08)`).
  - In `{% block extra_js %}` `<script>`, after the mobile card IIFE, add the delegated `click` handler IIFE on `.table-orders tbody`.
  - The handler must call `e.target.closest('a, button, input, select, textarea, form, label')` to bail out for interactive elements, preserving Print Receipt and Quick Advance behavior.

  **Files:** `templates/orders/order_list.html`

  **Verify:**
  - `python manage.py check` — zero issues.
  - In the browser, click on the Customer name cell of a row — confirm you navigate to the order detail page.
  - Click on the print icon — confirm it opens the receipt in a new tab without triggering navigation.
  - Click on the quick-advance button — confirm it advances the status without triggering navigation.
  - If realtime is running, wait for a new order to appear and confirm clicking that row also navigates correctly.

---

## Final Check

After all three items are done, run:
```
python manage.py check
```
Expected: `System check identified no issues (0 silenced).`

There are no model changes, no migrations, and no new views or URL patterns — this is purely a template + inline CSS/JS change.
