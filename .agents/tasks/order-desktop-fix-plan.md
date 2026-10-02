# Implementation Plan — Restore Desktop Order Management Layout

## Investigation Summary

Traced all responsive styling through:
- `templates/orders/order_list.html` (template with inline `<style>` and `<script>` blocks)
- `static/css/main.css` (desktop table base styles)
- `static/css/mobile-design-system.css` (responsive table card toggle system)
- `static/css/responsive.css` (mobile overrides)
- `static/js/responsive-tables.js` (generic table→card converter)
- `static/js/main.js` (sidebar, no order-card logic)

---

## Root Cause Analysis

### Root Cause 1 — CSS card styles escaped the mobile media query (PRIMARY)

**File:** `templates/orders/order_list.html`, `{% block extra_css %}` `<style>` block

**The bug:** The `@media (max-width: 767.98px)` block in the inline `<style>` block closes at **line ~76** — after the `.status-filter-bar` and search bar rules. A second `@media (min-width: 768px)` block then follows (lines ~79–88) for the desktop search bar fixes. That block closes with `}` at line 88.

Immediately after line 88 — with **no wrapping media query** — the entire mobile order card stylesheet begins:

```css
/* line 88 — after the desktop @media block closes */
  .om-cards ~ .table-mobile-cards,
  .table-mobile-cards:not(.om-cards) { display: none !important; }

  /* ── Order card ── */
  .om-card { ... }
  .om-top { ... }
  .om-icon { ... }
  .om-info { ... }
  .om-number { ... }
  .om-customer { ... }
  .om-total { ... }
  .om-meta { ... }
  .om-divider { ... }
  .om-paysec { ... }
  .om-pay-col { ... }
  .om-status-col { ... }
  .om-date { ... }
  .om-actions { ... }
  /* ... ~200 more lines of card-specific styles ... */
```

All of this CSS is at **global scope**, applying at every viewport width including 1280px, 1366px, 1440px, and 1920px.

### Root Cause 2 — JS card cleanup missing on desktop resize

**File:** `templates/orders/order_list.html`, `{% block extra_js %}` `<script>` block, `buildOrderCards()` function

The resize listener calls `buildOrderCards()` on every resize event. The function's guard is:

```js
if (window.innerWidth > 767) return;  // line ~937
```

This guard **returns early without removing** any `.om-cards` div that was previously built at mobile width. When a user:
- Loads the page in DevTools mobile mode, OR
- Opens the browser at a narrow width, OR
- Resizes from mobile to desktop

...the `.om-cards` div persists in the DOM. Because its CSS is now global (Root Cause 1), the card layout renders fully on desktop.

The fix for this is to add cleanup inside the early-return path: remove any `.om-cards` divs and unhide the table when going back to desktop width.

### Root Cause 3 — Minor global CSS leak (secondary)

**File:** `templates/orders/order_list.html`, same `<style>` block, line ~89

```css
.table-mobile-cards:not(.om-cards) { display: none !important; }
```

This rule is also outside any media query. It permanently hides `responsive-tables.js`-generated `.table-mobile-cards` at all widths. On this specific page, `mobile-design-system.css` already hides `.table-mobile-cards` at ≥768px, so this is redundant; but at <768px, it interferes with the fallback card display. This rule should be moved inside `@media (max-width: 767.98px)`.

---

## Existing Desktop Layout

The desktop layout is a standard table inside `.card > .table-wrapper`:

```html
<div class="card">
  <div class="table-wrapper">
    <table class="table-orders" data-mobile-cards="true">
      <thead>...</thead>
      <tbody>
        <tr data-order-id="..."> <!-- one row per order --> </tr>
      </tbody>
    </table>
  </div>
</div>
```

Desktop styles (from `main.css`):
- `table` — `width: 100%; border-collapse: collapse`
- `thead th` — brown background, white text, uppercase
- `tbody tr` — `border-bottom: 1px solid var(--cream)`, hover tint
- `tbody td` — `padding: 12px 16px; font-size: 0.9rem`
- `.table-wrapper` — `overflow-x: auto; border-radius: var(--radius)`

`mobile-design-system.css` (≥768px) confirms:
- `.table-mobile-cards { display: none; }` — hides card output
- `.table-wrapper table { display: table !important; }` — forces table visible

No template changes are needed — the desktop HTML structure is intact and correct.

---

## Breakpoint to Use

The project's existing mobile breakpoint for Order Management is **`767.98px`** (≤767.98px = mobile, ≥768px = desktop). This is used consistently throughout `responsive.css`, `mobile-design-system.css`, and the existing `@media (max-width: 767.98px)` blocks already in the template's `<style>`.

Do NOT introduce a new breakpoint.

---

## Files to Change

| File | Change |
|------|--------|
| `templates/orders/order_list.html` | 1. Wrap all `.om-card` and sub-element CSS inside `@media (max-width: 767.98px)` 2. Move the `.table-mobile-cards:not(.om-cards)` rule inside that same media query 3. Add desktop cleanup to `buildOrderCards()` JS function |

No changes to any other file. No backend changes.

---

## Implementation Plan

- [ ] 1. Wrap all mobile order-card CSS inside the existing `@media (max-width: 767.98px)` breakpoint.

      **What:** In `templates/orders/order_list.html`, inside the `{% block extra_css %}` `<style>` block, the `@media (min-width: 768px)` desktop block closes at line ~88 with `}`. Everything from the `.om-cards ~ .table-mobile-cards` rule onward through the end of `</style>` (approximately lines 88–300 of the style block) is at global scope. Wrap all of it — every `.om-*` rule — inside a single `@media (max-width: 767.98px) { ... }` block.

      Specifically, change:
      ```css
      /* BEFORE — these rules are OUTSIDE any media query, global scope */
        .om-cards ~ .table-mobile-cards,
        .table-mobile-cards:not(.om-cards) { display: none !important; }

        /* ── Order card ── */
        .om-card { ... }
        .om-top { ... }
        /* ... all other .om-* rules ... */
        .om-actions--2 { ... }
      ```
      To:
      ```css
      /* AFTER — all .om-* rules scoped to mobile only */
      @media (max-width: 767.98px) {
        .om-cards ~ .table-mobile-cards,
        .table-mobile-cards:not(.om-cards) { display: none !important; }

        /* ── Order card ── */
        .om-card { ... }
        .om-top { ... }
        /* ... all other .om-* rules ... */
        .om-actions--2 { ... }
      }
      ```

      **Files:** `templates/orders/order_list.html`

      **Verify:** Load the Order Management page at 1280px+ in a browser. Confirm the standard desktop table (brown header row, order rows) is displayed. Confirm no `.om-card` elements are visible. Confirm the table has columns: Order #, Customer, Type/Table, Items, Total, Status, Payment, Time, Actions.

- [ ] 2. Fix `buildOrderCards()` to remove stale cards when viewport returns to desktop width.

      **What:** In `templates/orders/order_list.html`, in the `{% block extra_js %}` `<script>` block, update `buildOrderCards()` so that the early-return path at `innerWidth > 767` first removes any previously-built `.om-cards` divs and ensures the table remains visible. Currently the guard is:

      ```js
      if (window.innerWidth > 767) return;
      ```

      Replace it with:
      ```js
      if (window.innerWidth > 767) {
        // Desktop: remove any mobile cards that were built before resize
        document.querySelectorAll('.om-cards').forEach(el => el.remove());
        return;
      }
      ```

      This ensures that resizing from mobile to desktop (or DevTools responsive mode) removes the card DOM and restores the native table view. The table itself is never hidden by JS — it is always present in the DOM; CSS at ≥768px already ensures it is `display: table`.

      **Files:** `templates/orders/order_list.html`

      **Verify:**
      1. Open Order Management on desktop. Table shows correctly.
      2. Open DevTools, switch to a mobile device preset (e.g. iPhone 12, 390px). Confirm mobile cards appear.
      3. Switch DevTools back to desktop (1280px). Confirm mobile cards disappear and the desktop table reappears.
      4. Resize browser window below 767px manually, then drag back above 768px. Confirm the layout switches correctly both ways.

- [ ] 3. Regression verification — confirm all preserved features still work.

      **What:** After both CSS and JS fixes are applied, perform the full regression checklist manually in a browser (no automated test runner needed for a CSS-only change; the project has no test suite covering frontend layout):

      Desktop (test at 1280px, 1366px, 1440px, 1920px):
      - Standard table layout is restored (columns: Order #, Customer, Type/Table, Items, Total, Status, Payment, Time, Actions)
      - No `.om-card` elements in DOM on desktop load
      - Order filtering (status tabs) works
      - Search bar works
      - Pagination works (if enough orders exist)
      - Payment buttons (Pay / Verify GCash) are present and clickable
      - View button navigates to order detail
      - Print Receipt (🖨️) button opens receipt
      - Quick-advance (▶ Preparing / ▶ Ready) button is visible and works
      - No horizontal page overflow at any tested width

      Mobile (test at 320px, 360px, 375px, 390px, 412px via DevTools):
      - `.om-cards` container is injected by JS
      - Order cards display correctly (icon, order number, customer, total, meta row, payment section, status section, date, action buttons)
      - Status filter pill strip scrolls horizontally
      - Mobile search bar stacked layout is correct
      - Mobile action buttons are accessible
      - Resize from mobile to desktop removes cards; resize back to mobile re-builds them

      Console:
      - No JavaScript errors at any width

      **Files:** No file changes — verification only.

      **Verify:** All checklist items pass at the tested breakpoints.

---

## What NOT to Change

- `static/css/main.css` — desktop table styles are correct; no changes needed
- `static/css/responsive.css` — mobile overrides are correctly scoped; no changes needed
- `static/css/mobile-design-system.css` — table card toggle system is correct; no changes needed
- `static/js/responsive-tables.js` — generic table converter works correctly; no changes needed
- `static/js/main.js` — no order-card logic; no changes needed
- All backend files (models, views, services, urls, payment logic, GCash logic, realtime)
- All other templates
- Database

---

## Exact Breakpoint Used

`max-width: 767.98px` — matches the project's existing mobile breakpoint used throughout `responsive.css` and `mobile-design-system.css`.
