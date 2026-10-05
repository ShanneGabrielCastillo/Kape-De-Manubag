# Mobile Cart FAB Visibility Investigation Report

**Workspace:** `c:\Users\Shecile\kape_de_manubag_system`  
**Date:** Investigation completed — read-only, no code was modified.

---

## Summary Answer (Bottom Line First)

**The cart FAB is completely hidden on all mobile viewports (≤ 575.98px) by an explicit `display: none !important` rule in `responsive.css` at line 1888–1889.**

The rule was added as "FIX C-2" under the comment _"Hide the fixed FAB on mobile because the in-nav button already serves the cart-access purpose."_ However, the in-nav cart button that the comment refers to **no longer exists** — the menu page's `<nav>` contains only a Dashboard/Staff Login link, not a cart button. The floating `.cart-fab` is the **only** cart entry point on the menu page, yet it is explicitly hidden on every mobile screen under 576px.

The chatbot button (`.kdm-chat-btn`) is not hidden; it is visible. What the screenshot shows as the single circular element near the bottom-right is almost certainly the **chatbot button**, not the cart FAB.

---

## Evidence

### 1. The Hiding Rule — `responsive.css` lines 1874–1890

```css
/* FIX C-2: Two cart FABs on the menu page … */
@media (max-width: 575.98px) {

  /* The fixed floating FAB — hidden on mobile in favour of the nav button */
  a.cart-fab[style*="badge-count"],
  a.cart-fab:not([style*="bottom:unset"]) {
    /* … commented-out block, effectively no-op … */
  }

  /* Cleaner approach: scope by page context. The fixed FAB on the menu
     page lives OUTSIDE the <nav>, so target it relative to the body. */
  body > a.cart-fab {
    display: none !important;          /* ← THIS HIDES THE CART FAB */
  }

}
```

The selector `body > a.cart-fab` is a direct match for the cart element in `templates/menu/index.html`:

```html
<!-- Floating Cart Button -->
<a href="{% url 'orders:cart' %}" class="cart-fab" aria-label="View cart">
  🛒
  <span class="badge-count" id="cart-fab-badge" ...>{{ cart_count }}</span>
</a>
```

This `<a>` is a **direct child of `<body>`** (it is inside `{% block body %}` at the top level of the template, outside any wrapper div). The selector matches perfectly. `display: none !important` is applied at every width from 0 to 575.98px — covering 320px, 360px, 375px, 390px, and 412px entirely.

### 2. The Missing "Nav Button" — the comment's premise is false

The comment justifying the hide rule says:
> _"hidden on mobile in favour of the nav button"_

The actual `<nav>` in `templates/menu/index.html` (lines 8–17) contains only:

```html
<nav style="…">
  <div>☕ Kape De Manubag</div>
  <div style="display:flex;align-items:center;gap:12px">
    {% if user.is_authenticated %}
      <a href="…" class="btn btn-sm btn-outline">Dashboard</a>
    {% else %}
      <a href="…" class="btn btn-sm btn-outline">Staff Login</a>
    {% endif %}
  </div>
</nav>
```

There is **no cart button in the nav.** The fallback the CSS relied on was already removed (or was never added). Hiding the FAB leaves zero cart access on mobile.

### 3. CSS Rules for `.cart-fab` at Every Breakpoint

| Breakpoint | Source | Rules |
|---|---|---|
| Desktop (all widths) | `main.css` line 825 | `position:fixed; bottom:28px; right:28px; width:60px; height:60px; border-radius:50%; display:flex; z-index:100;` |
| ≤ 575.98px | `responsive.css` line 344 | `bottom:20px; right:16px; width:56px; height:56px; font-size:1.3rem;` (position override — stays fixed, stays visible) |
| ≤ 575.98px | `responsive.css` line 1888 | **`display: none !important`** — this overrides everything and hides the FAB |
| Print | `main.css` line 1231 | `display: none !important` — irrelevant to mobile screen |

The two mobile blocks are separate `@media (max-width: 575.98px)` rules. The position override (line 344) executes first; the hide rule (line 1888) executes later in the cascade and wins because it is declared after and carries `!important`.

### 4. CSS Rules for `.kdm-chat-btn` at Every Breakpoint

| Breakpoint | Source | Rules |
|---|---|---|
| Desktop | `chatbot.css` line 8 | `position:fixed; bottom:90px; right:28px; width:56px; height:56px; border-radius:50%; z-index:110; display:flex;` |
| ≤ 480px | `chatbot.css` line 329 | `bottom:90px; right:16px;` (unchanged visibility) |

The chatbot button has **no hide rule** at any mobile breakpoint. It remains fully visible at 320–576px.

### 5. Pixel Positions at 375px Viewport Width

Assuming a typical mobile viewport height of ~812px (iPhone 12/13):

**Cart FAB (if it were visible):**
- bottom: 20px (responsive.css override applies)
- right: 16px
- size: 56×56px
- top edge: 812 − 20 − 56 = 736px from top

**Chatbot button:**
- bottom: 90px (chatbot.css mobile rule)
- right: 16px
- size: 56×56px
- top edge: 812 − 90 − 56 = 666px from top
- bottom edge: 812 − 90 = 722px from top

**Overlap check at 375px:**
- Chatbot occupies vertical range: 666–722px from top
- Cart FAB would occupy: 736–792px from top
- Gap between them: 736 − 722 = **14px** — they would NOT overlap at these bottom values

At 320px viewport width, positions are the same (both use `right: 16px`), still no overlap.

**However, the cart FAB is hidden before overlap even becomes a question.**

### 6. Z-Index Hierarchy

| Element | z-index | Source |
|---|---|---|
| `.cart-fab` | 100 | `main.css` line 842 |
| `.kdm-chat-btn` | 110 | `chatbot.css` line 26 |
| `.kdm-chat-panel` | 120 | `chatbot.css` line 68 |
| `<nav>` (menu page) | 50 | inline style on nav element |

If both FABs were visible simultaneously, the chatbot button (z-index 110) would render above the cart FAB (z-index 100), but at the computed positions above they do not overlap. No z-index conflict causes the cart to be hidden.

### 7. Stacking Context / Scroll Container Check

The `<a class="cart-fab">` element in `templates/menu/index.html` is a **direct child of `<body>`**, placed after `.menu-container` and before the chatbot markup. It is:

- Not inside any scrollable overflow container
- Not inside any element with `transform`, `filter`, `will-change`, or `isolation`
- Not inside the `<nav>` or `.menu-container`

The `position: fixed` declaration in `main.css` anchors it to the viewport correctly. No stacking context traps it. The sole cause of invisibility is the `display: none !important` rule.

### 8. JavaScript Badge/FAB Logic — Does It Hide the FAB?

The add-to-cart, quantity, and remove handlers in `static/js/main.js` only manipulate the **badge span** (`#cart-fab-badge`), not the parent `<a>` element. From lines 151–156:

```js
const cartFabBadge = document.querySelector('.cart-fab .badge-count');
if (cartFabBadge) {
  const _count = parseInt(data.cart_count, 10) || 0;
  cartFabBadge.textContent = _count;
  cartFabBadge.style.display = _count > 0 ? 'flex' : 'none';
}
```

The cross-tab `storage` event handler (lines 278–285) also targets only `#cart-fab-badge`:

```js
const badge = document.getElementById('cart-fab-badge');
badge.style.display = count > 0 ? 'flex' : 'none';
```

Neither handler touches the `.cart-fab` parent. **JavaScript is not the cause of the problem.**

Note: The add-to-cart/qty/remove handlers still use the class selector `.cart-fab .badge-count` while the storage handler uses `#cart-fab-badge`. Both refer to the same element; both work. The pending "selector consistency" cleanup (message 2) is a separate, unrelated task.

### 9. CSS Files in `static/css/`

```
chatbot.css              7,955 bytes
main.css                60,273 bytes
mobile-design-system.css 19,244 bytes
responsive.css          100,119 bytes
```

`mobile-design-system.css` contains no rules referencing `.cart-fab` (confirmed by grep — no matches).

---

## Root Cause

**Single cause, single location:**

`static/css/responsive.css`, lines 1874–1890, inside `@media (max-width: 575.98px)`:

```css
body > a.cart-fab {
    display: none !important;
}
```

This rule was written under the assumption that a "nav button" provides cart access on mobile. That nav button does not exist. The rule hides the only cart entry point on the menu page for every mobile user.

---

## What Is Visible on Mobile (the Screenshot Explanation)

The single circular element visible near the bottom-right of the mobile screenshot is the **chatbot button** (`.kdm-chat-btn`). It is positioned `bottom: 90px; right: 16px` with `z-index: 110` and has no hide rule — it is fully visible. The cart FAB is `display: none`, so it is completely absent from the rendered page.

---

## Recommended Fix

Remove (or limit) the `body > a.cart-fab { display: none !important; }` rule.

**Option A — Delete the hide block entirely** (recommended):

In `static/css/responsive.css`, remove the entire "FIX C-2" block (lines 1866–1890). The cart FAB position override at line 344 (inside the first `@media (max-width: 575.98px)` block) already provides correct mobile sizing (`bottom: 20px; right: 16px; 56×56px`). No other change is needed.

After this deletion:
- Cart FAB: `position: fixed; bottom: 20px; right: 16px; width: 56px; height: 56px; z-index: 100` on mobile
- Chatbot button: `position: fixed; bottom: 90px; right: 16px; width: 56px; height: 56px; z-index: 110` on mobile
- Vertical gap between them: 90 − 20 − 56 = **14px** — they do not overlap
- Both are inside the viewport at 320–412px

**Option B — Replace with a no-op** (if the block must be kept for history):

Replace `display: none !important` with a comment explaining the nav button was removed, and leave the block empty. Less clean than Option A.

**No other files need to change** for the visibility fix. The cart element exists in the template, has correct `position: fixed` CSS, and has working JavaScript badge logic.

---

## Verification Checklist (Post-Fix)

1. Load the menu page at 375px viewport width in DevTools device emulation.
2. Confirm `.cart-fab` is visible near bottom-right (not hidden).
3. Confirm `.kdm-chat-btn` is visible above the cart FAB with a ~14px gap.
4. Confirm they do not overlap.
5. Add an item to cart — badge updates on the FAB.
6. Check at 320px, 360px, 390px, 412px — FAB remains inside viewport.
7. Check desktop (> 576px) — behavior unchanged (the deleted rule was mobile-only).
8. Open two tabs — change cart in one tab — badge syncs in the other tab via `storage` event.

---

## Files to Modify

| File | Action |
|---|---|
| `static/css/responsive.css` | Remove the "FIX C-2" block (lines 1866–1890) that contains `body > a.cart-fab { display: none !important; }` |

No other files need modification for the mobile visibility fix.
