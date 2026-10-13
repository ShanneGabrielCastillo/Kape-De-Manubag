# Order Management Table UX — Remove View Button, Clickable Rows

The change removes the View button from the Actions column across all three rendering paths (server-rendered rows, SSE-inserted rows, mobile cards), replaces inline order-number styling with a proper `.order-num-link` class, and adds a single delegated `click` handler on `<tbody>` for row navigation. The approach is minimal and architecturally sound: one file changed, delegation anchored to a stable element, interactive-element guard via `closest()`. 

Watch for: **confirmed** — `detailUrl` (and four sibling `const` declarations) are referenced in `row.dataset.*` assignments ~19 lines before they are declared. Under the `const` temporal dead zone this throws a `ReferenceError` every time a realtime new-order event fires, silently falling back to a full page reload via the `catch` block.

**Verdict**: NEEDS_CHANGES

---

## High-level view

The View button is gone from the Django template loop, both SSE branches, and the mobile `buildOrderCard()` function. The order number is now an `<a class="order-num-link">` on every path, with hover and focus-visible styles scoped to the template's `{% block extra_css %}`. Nothing in `static/css/main.css` or `static/js/main.js` was modified — the change is entirely self-contained in `order_list.html`.

The row-click handler is a single delegated listener on `.table-orders tbody`. Because SSE inserts rows with `tbody.prepend(row)` rather than replacing the `<tbody>` itself, the handler covers realtime-inserted rows without re-attachment. The `closest('a, button, input, select, textarea, form, label')` guard correctly lets the Print Receipt link and the quick-advance button handle their own events. No double-navigation is possible: clicking the order-number link hits the `<a>` guard and returns before `window.location.href` is reached.

The one concrete defect: inside the `new_order` SSE handler, `row.dataset.detailUrl`, `row.dataset.receiptUrl`, `row.dataset.advanceUrl`, `row.dataset.payUrl`, `row.dataset.acceptUrl`, and `row.dataset.statusUrl` are all assigned from `const` variables that are not declared until ~19 lines later. `const` bindings are not hoisted to a usable value — accessing them before the declaration throws a `ReferenceError`. Every `new_order` event will hit this and fall into the `catch` block, which triggers a `setTimeout(() => location.reload(), 2000)`. The row never gets `data-detail-url` set in-place, so freshly SSE-inserted rows are not clickable until the reload completes.

<details>
<summary>Issues (2)</summary>

1. **`detailUrl` TDZ crash in `new_order` handler** — `row.dataset.detailUrl = detailUrl` (and five sibling assignments) appear ~19 lines before the corresponding `const` declarations. Every `new_order` SSE event throws `ReferenceError: Cannot access 'detailUrl' before initialization`, falls to the `catch` block, and reloads the page after 2 s. Move all six `const` URL declarations to immediately before the `row.dataset.*` block, or hoist them above the `row = document.createElement('tr')` line.

2. **SSE-inserted rows have no `data-detail-url` until reload** — As a consequence of issue 1, any row inserted live (before the reload) lacks `data-detail-url`, so the delegated click handler's `e.target.closest('tr[data-detail-url]')` check silently drops clicks on it. This resolves automatically once the `const` declarations are moved, but should be verified after the fix.

</details>

<details>
<summary>Details</summary>

### `detailUrl` temporal dead zone in the `new_order` handler

Inside `RealtimeConnection.on('new_order', ...)`, the row's dataset is populated in one block and the URL `const` variables are declared in a second block that comes afterward. The relevant excerpt:

```js
// line ~629 — ASSIGNMENT (before declaration)
row.dataset.detailUrl  = detailUrl;
row.dataset.receiptUrl = receiptUrl;
row.dataset.advanceUrl = advanceUrl;
row.dataset.payUrl     = payUrl;
row.dataset.acceptUrl  = acceptUrl;
row.dataset.statusUrl  = statusFormUrl;
// ...19 lines later...
// line ~648 — DECLARATION (too late)
const statusFormUrl = `{% url 'orders:update_status' 0 %}`.replace(...);
const detailUrl     = `{% url 'orders:order_detail' 0 %}`.replace(...);
const receiptUrl    = `{% url 'orders:print_receipt' 0 %}`.replace(...);
const advanceUrl    = `{% url 'orders:quick_status_advance' 0 %}`.replace(...);
const payUrl        = `{% url 'orders:process_payment' 0 %}`.replace(...);
const acceptUrl     = `{% url 'orders:accept_order' 0 %}`.replace(...);
```

`const` (unlike `var`) is not initialized until the declaration is evaluated. Reading it before that point throws `ReferenceError: Cannot access 'detailUrl' before initialization`. The `try/catch` at the top of the handler catches this, logs a warning, and calls `setTimeout(() => location.reload(), 2000)` — so the page self-heals, but the intended in-place insert never happens, and the freshly inserted (partial) row is never navigable via the click handler.

The fix is to move the six `const` declarations to before `const row = document.createElement('tr')`. The rest of the handler (the `row.innerHTML` block that uses `detailUrl`, `receiptUrl`, etc.) already appears after the declarations so it will be unaffected.

### Delegated handler and double-navigation

The handler's guard is `if (e.target.closest('a, button, input, select, textarea, form, label')) return`. Clicking the `.order-num-link` `<a>` matches the guard and returns before `window.location.href` is set — confirmed. No double-navigation.

### Mobile card path

`buildOrderCard()` uses `escHtml(d.detailUrl || '')` for the `.om-number` anchor — that reads from `row.dataset.detailUrl` which is populated server-side on the `<tr>` for the initial render, so the mobile card path is unaffected by the SSE bug above.

</details>

---

<details>
<summary>File map</summary>

| File | What changed |
|------|--------------|
| `templates/orders/order_list.html` | Removed View button from Django loop, both SSE branches, and `buildOrderCard()`; replaced inline order-number styles with `.order-num-link` class; added delegated `<tbody>` click handler; added `.om-actions--1` CSS rule |

Full diff: `git diff main -- templates/orders/order_list.html`

</details>
