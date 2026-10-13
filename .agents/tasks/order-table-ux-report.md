# Order Management Table UX — Implementation Report

## Files Changed

**Only one file was modified:**
`templates/orders/order_list.html`

Commit: `d3cd6e9` — feat: remove View button, add clickable order rows with delegated handler

---

## Change 1 — View Button Removed

Removed from three locations in `templates/orders/order_list.html`:

**1. Django template loop (Actions `<td>`):**
```html
<!-- BEFORE -->
<a href="{% url 'orders:order_detail' order.pk %}" class="btn btn-sm btn-outline">View</a>
<a href="{% url 'orders:print_receipt' order.pk %}" class="btn btn-sm btn-outline" target="_blank">…</a>

<!-- AFTER -->
<a href="{% url 'orders:print_receipt' order.pk %}" class="btn btn-sm btn-outline" target="_blank">…</a>
```

**2. SSE `new_order` handler — `isAwaitingPayment` branch `actionCellHtml`:**
```js
// BEFORE
<a href="${detailUrl}" class="btn btn-sm btn-outline">View</a>
<a href="${receiptUrl}" class="btn btn-sm btn-outline" target="_blank">…</a>

// AFTER
<a href="${receiptUrl}" class="btn btn-sm btn-outline" target="_blank">…</a>
```

**3. SSE `new_order` handler — `else` branch `actionCellHtml`:**
```js
// BEFORE
<a href="${detailUrl}" class="btn btn-sm btn-outline">View</a>
<a href="${receiptUrl}" …> + quickAdvance button

// AFTER
<a href="${receiptUrl}" …> + quickAdvance button (unchanged)
```

**4. `buildOrderCard()` (mobile cards):**
- Removed `const viewBtn = …` declaration
- Removed `${viewBtn}` from `actionsHtml`
- Updated `actionCount`: `advanceBtn ? 3 : 2` → `advanceBtn ? 2 : 1`
- Updated `om-actions` class: uses `om-actions--1` (print-only) or `om-actions--2` (print + advance)
- Added CSS rule for `om-actions--1` in `{% block extra_css %}`:
  ```css
  @media (max-width: 767.98px) {
    .om-actions.om-actions--1 { grid-template-columns: 1fr; }
  }
  ```

---

## Change 2 — Order Number Link

**How it works:** The order number `<a>` tag already existed on all three rendering paths. The inline `style="font-weight:700;color:var(--caramel)"` was replaced with `class="order-num-link"` in both the Django template loop and the SSE handler's `row.innerHTML`. The mobile card's `.om-number` class already had its own existing CSS and was not changed.

**URL name and kwarg used:**
- Template loop: `{% url 'orders:order_detail' order.pk %}`
- SSE handler: `` `{% url 'orders:order_detail' 0 %}`.replace('/0/', `/${order.order_id}/`) ``

**New CSS added to `{% block extra_css %}`:**
```css
.order-num-link {
  font-weight: 700;
  color: var(--caramel);
  text-decoration: none;
  border-radius: 3px;
  transition: color 0.15s;
}
.order-num-link:hover {
  color: var(--brown-dark);
  text-decoration: underline;
}
.order-num-link:focus-visible {
  outline: 2px solid var(--caramel);
  outline-offset: 2px;
}
```

---

## Change 3 — Row-Click Navigation

**How it works:** A single delegated `click` handler is attached to the `<tbody>` of `.table-orders` via an IIFE added at the bottom of `{% block extra_js %}` (after the mobile card IIFE):

```javascript
(function () {
  const tbody = document.querySelector('.table-orders tbody');
  if (!tbody) return;

  tbody.addEventListener('click', function (e) {
    const row = e.target.closest('tr[data-detail-url]');
    if (!row) return;
    if (e.target.closest('a, button, input, select, textarea, form, label')) return;
    const url = row.dataset.detailUrl;
    if (url) window.location.href = url;
  });
}());
```

**Parent element:** `<tbody>` of `.table-orders` — this element is never replaced by SSE handlers (they use `tbody.prepend(row)` to insert rows, not replace the tbody itself). No re-attachment is needed after realtime updates.

**`data-detail-url` source:**
- Server-rendered rows: already present as `data-detail-url="{% url 'orders:order_detail' order.pk %}"` on each `<tr>`
- SSE-inserted rows: `row.dataset.detailUrl = detailUrl` (which sets the `data-detail-url` attribute)

**CSS added for desktop:**
```css
@media (min-width: 768px) {
  .table-orders tbody tr[data-detail-url] { cursor: pointer; }
  .table-orders tbody tr[data-detail-url]:hover { background: rgba(193, 122, 59, 0.08); }
}
```

---

## Printing Remains Independent

The Print Receipt button is an `<a>` tag (`target="_blank"`). When clicked:
1. `e.target.closest('a, button, input, select, textarea, form, label')` returns the `<a>` element (truthy)
2. The delegated handler returns early — no navigation is triggered
3. The browser follows the `href` in a new tab as intended

No `stopPropagation()` was needed; the `closest()` guard is sufficient.

---

## Desktop and Mobile Behavior

**Desktop (≥ 768px):**
- Rows with `data-detail-url` show `cursor: pointer` and a slightly stronger hover background (`rgba(193,122,59,0.08)`)
- Order number shows underline + darker color on hover; visible focus ring on keyboard focus
- Clicking any non-interactive area of a row navigates to the order detail page
- Print Receipt button opens receipt in a new tab without triggering row navigation
- Quick-advance button advances status without triggering row navigation

**Mobile (≤ 767px):**
- The mobile card layout uses `buildOrderCard()` — cards are NOT made wholesale clickable (avoids inaccessible clickable markup)
- The `.om-number` link inside each card remains the accessible entry point to order details
- Print button spans full width when it is the only action (`om-actions--1`)
- Print + Advance show as two equal columns (`om-actions--2`)

---

## Preservation Confirmation

| Feature | Status |
|---|---|
| Print Receipt | ✅ Unmodified; bubbling prevented by `closest()` guard |
| Search | ✅ No changes to search form or filtering logic |
| Filters (status tabs) | ✅ No changes to status filter bar |
| Pagination | ✅ No changes to pagination block |
| Status badges | ✅ No changes to badge rendering |
| Currency/date formatting | ✅ No changes |
| Realtime SSE updates | ✅ Delegated handler on stable `<tbody>` covers SSE-inserted rows automatically |
| Kitchen Staff workflow | ✅ No changes — Kitchen Staff use a separate view/page |
| Cashier/Admin permissions | ✅ `@login_required @cashier_or_admin_required` on `order_detail` view unchanged |
| Audit logging | ✅ No changes to view logic |
| No new routes or views | ✅ Only template changes |
| No duplicate handlers | ✅ Single IIFE; no re-attachment on updates |

---

## `python manage.py check` Result

```
System check identified no issues (0 silenced).
```

(The command also emits a `UserWarning` about `SECRET_KEY` not being set — this is a pre-existing environment configuration note, not a Django system check issue.)
