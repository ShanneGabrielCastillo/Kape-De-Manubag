# Implementation Plan — Floating Cart on Mobile + Realtime Cart Count + Remove Topbar Cart

## Codebase Findings (ground truth from exploration)

### Exact HTML to remove from topbar
In `templates/menu/index.html`, the topbar nav `<div style="display:flex;align-items:center;gap:12px">` contains:
```html
<a href="{% url 'orders:cart' %}" class="cart-fab" style="position:relative;width:42px;height:42px;font-size:1.1rem;bottom:unset;right:unset;box-shadow:var(--shadow)">
  🛒
  {% if cart_count %}<span class="badge-count">{{ cart_count }}</span>{% endif %}
</a>
```
This entire `<a>` element must be removed.  
It is NOT in `base.html` — it is in the menu template's own `{% block body %}` nav section.

### Floating cart (bottom of page)
Also in `templates/menu/index.html`, near the bottom before the chatbot block:
```html
{% if cart_count %}
<a href="{% url 'orders:cart' %}" class="cart-fab">
  🛒
  <span class="badge-count">{{ cart_count }}</span>
</a>
{% endif %}
```
**Problem**: Wrapped in `{% if cart_count %}`, so it vanishes when the cart is empty.  
**Fix**: Remove the conditional — always render the FAB, always render the badge element, and let CSS/JS handle badge visibility.

### cart_count definition
`apps/orders/models.py` Cart.item_count = `sum(item.quantity for item in cart_items)` → **total quantity of all items**, not distinct products. The badge must display this same value.

### Add-to-cart JSON response
`apps/orders/views.py` `add_to_cart()` already returns `cart_count: cart.item_count` (line ~188).  
`update_cart()` and `remove_from_cart()` also return `cart_count`.  
`clear_cart()` returns `cart_count: 0`.  
**No view changes needed.**

### JS badge selectors (main.js)
- `document.querySelector('.cart-count')` — topbar badge (will be removed)
- `document.querySelector('.cart-fab .badge-count')` — floating FAB badge (keep & fix)
- All three handlers (add-to-cart, qty-btn, cart-remove) already update `.cart-fab .badge-count`.

### Floating cart CSS positioning
`static/css/main.css`:
```css
.cart-fab { position: fixed; bottom: 28px; right: 28px; z-index: 100; width: 60px; height: 60px; }
```
`static/css/responsive.css` `@media (max-width: 575.98px)`:
```css
.cart-fab { bottom: 20px; right: 16px; width: 56px; height: 56px; font-size: 1.3rem; }
```
**There is no `display:none` on mobile** — the FAB is already visible on mobile. The only problem is the `{% if cart_count %}` conditional in the template.

### Chatbot button positioning (no conflicts)
`static/css/chatbot.css`:
```css
.kdm-chat-btn { position: fixed; bottom: 90px; right: 28px; z-index: 110; }
/* mobile ≤480px */ .kdm-chat-btn { bottom: 80px; right: 16px; }
```
The chatbot comment says "above the cart FAB (bottom: 28px, height: 60px)" — it was already designed to stack above the cart. No changes needed to chatbot positioning.

### Floating cart destination
The FAB links to `{% url 'orders:cart' %}` (the cart page). No modal/drawer.

---

## Implementation Plan

- [ ] 1. Remove the topbar cart icon from the menu template.
      In `templates/menu/index.html`, delete the `<a class="cart-fab" ...>` element that is
      embedded inline inside the topbar nav `<div style="display:flex;align-items:center;gap:12px">`.
      Keep the Staff Login / Dashboard link. This is the only file to change for this step.
      Files: `templates/menu/index.html`
      Verify: Load the menu page. The topbar shows only the logo + Staff Login/Dashboard button.
              No cart icon or badge appears in the topbar. Browser DevTools confirms the topbar
              `<a class="cart-fab">` element is gone.

- [ ] 2. Make the floating cart always visible (remove the `{% if cart_count %}` conditional).
      The floating FAB near the bottom of `templates/menu/index.html` is wrapped in
      `{% if cart_count %}...{% endif %}`. Remove that conditional so the FAB renders
      unconditionally. Keep the inner badge element unconditional too but make it hidden by
      default when count is 0 (handled in step 3 via CSS). The FAB must always render so
      JS can update its badge without the element being absent from the DOM.

      New HTML (replaces the `{% if cart_count %}...{% endif %}` block):
      ```html
      <a href="{% url 'orders:cart' %}" class="cart-fab" aria-label="View cart">
        🛒
        <span class="badge-count"
              id="cart-fab-badge"
              {% if not cart_count %}style="display:none"{% endif %}>{{ cart_count }}</span>
      </a>
      ```
      Files: `templates/menu/index.html`
      Verify: Load the menu page with an empty cart. The floating cart button is visible at
              bottom-right. The red badge is hidden. Add an item — the badge appears with "1".

- [ ] 3. Add `id="cart-fab-badge"` to the badge and update the JS badge selectors.
      In `static/js/main.js`, all three handlers (add-to-cart form submit, `.qty-btn` click,
      `.cart-remove` click) use `document.querySelector('.cart-fab .badge-count')`. This still
      works after step 2, but they must also show/hide the badge when the count reaches 0 or
      comes back from 0.

      For each handler that updates `cartFabBadge.textContent = data.cart_count`:
      - If `data.cart_count > 0`: set `cartFabBadge.textContent = data.cart_count` and
        `cartFabBadge.style.display = 'flex'`.
      - If `data.cart_count == 0` or `data.cart_count` is falsy: set
        `cartFabBadge.style.display = 'none'`.

      Also update the `localStorage.setItem('kdm_cart_count', data.cart_count)` calls —
      these are already present and correct.

      **Decision**: The add-to-cart handler also updates `document.querySelector('.cart-count')`.
      Since the topbar cart element is removed in step 1, this selector will find nothing and
      the `.textContent` assignment is a safe no-op. Do NOT remove that line — it is harmless
      and avoids breaking any other page that might use `.cart-count`.

      Files: `static/js/main.js`
      Verify: Start with empty cart. Add one item → badge appears with "1". Add again → "2".
              Open cart page, reduce quantity to 0 (remove item) → badge decreases. Remove
              all items → badge hides. No page refresh at any step.

- [ ] 4. Ensure the floating cart badge is correct on page load (server-rendered count).
      The badge already receives `{{ cart_count }}` from the Django context processor
      (`apps/orders/context_processors.cart_count`), which is registered in settings and
      runs on every request. No changes needed to the context processor.
      The `style="display:none"` applied in step 2 when `cart_count` is falsy ensures the
      badge is correctly hidden on first render for an empty cart.
      Files: No changes (verification step only).
      Verify: Navigate to the menu page with items already in the cart (e.g., after adding
              items and reloading). The badge shows the correct count immediately on page load
              without any JS execution required.

- [ ] 5. Fix mobile floating cart positioning to not overlap the chatbot button.
      The chatbot CSS already accounts for the cart at `bottom:90px` (above `cart-fab`'s
      `bottom:28px` + `height:60px = 88px`). On mobile ≤480px the chatbot is at `bottom:80px`
      and `responsive.css` puts the cart at `bottom:20px` with `height:56px = 76px` —
      the chatbot at 80px sits 4px above the cart top edge, which is too tight.

      In `static/css/chatbot.css`, update the `@media (max-width: 480px)` rule:
      ```css
      @media (max-width: 480px) {
        .kdm-chat-btn {
          bottom: 90px;   /* was 80px — cart is bottom:20px + height:56px = 76px; 90px gives 14px gap */
          right: 16px;
        }
        ...
      }
      ```
      No changes needed for desktop — the existing `bottom:90px` on `.kdm-chat-btn` already
      provides a comfortable gap above the cart FAB at `bottom:28px + height:60px = 88px`.

      Files: `static/css/chatbot.css`
      Verify: On a 375px viewport, both the cart FAB and chatbot button are visible, clearly
              separated with a visible gap. Neither overlaps. Both are fully within the viewport.

- [ ] 6. Cross-tab badge synchronization via localStorage (opt-in, no architecture change).
      The JS handlers already write `localStorage.setItem('kdm_cart_count', data.cart_count)`
      after every cart update. Add a `storage` event listener in `main.js` that reads the
      `kdm_cart_count` key and updates the FAB badge when another tab changes the cart.
      This is clean because the key already exists; no new storage keys or server calls needed.

      Add to `static/js/main.js` (near the bottom, after the existing cart handlers):
      ```js
      // ── Cross-tab cart count sync via localStorage ──
      window.addEventListener('storage', function(e) {
        if (e.key !== 'kdm_cart_count') return;
        const badge = document.querySelector('.cart-fab .badge-count');
        if (!badge) return;
        const count = parseInt(e.newValue, 10) || 0;
        badge.textContent = count;
        badge.style.display = count > 0 ? 'flex' : 'none';
      });
      ```
      This only fires in OTHER tabs (not the one that wrote it), which is the desired behavior.

      Files: `static/js/main.js`
      Verify: Open the menu in two tabs. Add an item in tab 1. Tab 2's badge updates without
              a page refresh. This is a best-effort enhancement — if the `storage` event does
              not fire in a particular browser/session it degrades gracefully.

---

## Questions answered

| Question | Answer |
|---|---|
| Exact HTML/block to remove from topbar | The `<a class="cart-fab" style="position:relative;width:42px;height:42px;font-size:1.1rem;bottom:unset;right:unset;...">` in the menu template's inline topbar nav (NOT in base.html) |
| CSS class/selector hiding floating cart on mobile | None — no `display:none` on mobile. The only hiding is `{% if cart_count %}` in the template. |
| Chatbot bottom/right | Desktop: `bottom:90px; right:28px`. Mobile ≤480px: `bottom:80px; right:16px` (needs fix to 90px) |
| Cart FAB bottom/right | Desktop: `bottom:28px; right:28px`. Mobile ≤575px: `bottom:20px; right:16px` |
| add-to-cart JSON includes cart_count? | Yes — `cart_count: cart.item_count` already returned. No view changes needed. |
| JS selector for cart badge | `document.querySelector('.cart-fab .badge-count')` (main.js, all three handlers) |
| cart_count = total quantity or distinct products | Total quantity of all items (`sum(item.quantity for item in cart_items)`) |
| Floating cart opens page/modal/drawer | Links to `{% url 'orders:cart' %}` — cart page (not modal/drawer) |
