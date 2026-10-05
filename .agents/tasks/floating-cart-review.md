# Floating cart FAB — always-visible badge with cross-tab sync

The menu page's cart button was split from a conditionally-rendered inline element into a persistent floating action button (FAB) that is always in the DOM. The topbar's inline `<a class="cart-fab">` duplicate was removed, the FAB is now rendered unconditionally with an always-present badge that starts hidden when the cart is empty, all three JS cart handlers now properly show/hide the badge, a cross-tab `storage` listener was added, and the chatbot button's mobile bottom offset was corrected from 80 px to 90 px to maintain a clear gap above the cart FAB.

Watch for: The `storage` listener in `main.js` still selects the badge with `.cart-fab .badge-count` (class-based selector) rather than the new `#cart-fab-badge` id that was added in this change — **confirmed** inconsistency. This doesn't break anything today, but the id was specifically added to make badge targeting robust; the listener should use it.

**Verdict**: APPROVED

---

## High-level view

The topbar duplication is cleanly removed: the inline `<a class="cart-fab" style="position:relative;...">` is gone from the nav `<div>`, and `base.html` was not touched. The floating FAB now exists unconditionally in the DOM, which is the correct prerequisite for JS badge updates without presence checks.

All three AJAX handlers — add-to-cart, qty-btn, cart-remove — now include the `display: 'flex' / 'none'` visibility toggle alongside the `textContent` update, and all three mirror the count to `localStorage`. The server-rendered initial state is handled via the Django template's `{% if not cart_count %}style="display:none"{% endif %}` — badge is hidden on empty-cart page load without any JS execution needed.

The `storage` event listener wires up cross-tab sync using the already-written `kdm_cart_count` key. The listener correctly ignores keys it doesn't own and works only in sibling tabs (the writing tab does not receive the event). No backend files were modified.

The chatbot mobile fix brings the `@media (max-width: 480px)` `.kdm-chat-btn` `bottom` from 80 px to 90 px, matching the desktop rule. At the mobile cart FAB position (`bottom: 20px`, `height: 56px = 76px`), 90 px gives a 14 px clear gap — an improvement over the previous 4 px near-collision.

---

<details>
<summary>Issues (1)</summary>

1. **Inconsistent badge selector in storage listener** — The `window.addEventListener('storage', ...)` block added at line 277 of `main.js` selects the badge with `document.querySelector('.cart-fab .badge-count')`, but the same change added `id="cart-fab-badge"` specifically to allow direct targeting. All three AJAX handlers also still use the class selector. This won't break badge visibility today since the class selector still resolves correctly, but the id goes unused and the intent of the change is not fully realized. Update the storage listener (and optionally the three handlers) to use `document.getElementById('cart-fab-badge')`.

</details>

---

<details>
<summary>Details</summary>

## Badge selector inconsistency in the storage listener

The `id="cart-fab-badge"` attribute was added to the badge span, and the plan's stated intent was to "make badge targeting robust." However, the `window.addEventListener('storage', ...)` block at line 277 uses `document.querySelector('.cart-fab .badge-count')`, not `document.getElementById('cart-fab-badge')`. The three AJAX handlers also use the class selector. The id is never read by any JS in `main.js`.

This is a **confirmed** gap: the id exists solely as a stable hook but no code reaches for it. If a future change renames the CSS class, the storage listener and all three handlers would silently stop updating the badge. Switching to `getElementById('cart-fab-badge')` would close this.

## Mobile overlap fix

The plan noted that at `max-width: 480px`, the cart FAB is at `bottom: 20px` with `height: 56px`, meaning its top edge sits at 76 px. The old `bottom: 80px` on `.kdm-chat-btn` left a 4 px gap — effectively overlapping visually. The new `bottom: 90px` gives 14 px of separation. The desktop rule was already at `bottom: 90px`; the mobile rule now matches it.

</details>

---

<details>
<summary>File map</summary>

| File | What changed |
|---|---|
| `templates/menu/index.html` | Removed topbar inline `<a class="cart-fab">` duplicate; replaced conditional `{% if cart_count %}` FAB with unconditional render; added `id="cart-fab-badge"` and `aria-label` to FAB |
| `static/js/main.js` | All three cart handlers now toggle badge `display` (flex/none) on count change; `localStorage.setItem` calls added to all three; cross-tab `storage` event listener added |
| `static/css/chatbot.css` | `@media (max-width: 480px)` `.kdm-chat-btn` `bottom` changed from `80px` to `90px` |

`base.html` — not modified (confirmed).  
Backend files (`models.py`, `views.py`, `context_processors.py`, migrations) — not modified (confirmed).

</details>
