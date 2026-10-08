# Implementation Plan: Dashboard & POS Page-Heading Consistency

## Findings from Code Inspection

### dashboard/index.html — current state
`{% block content %}` opens **directly** with:
```html
<div id="dashboard-content" class="dash-loading" aria-busy="true">
```
There is **no** `.page-header` / `<h1 class="page-title">` before that div.
→ A "Dashboard" heading must be **added**.

### orders/pos.html — current state
`{% block content %}` opens **directly** with:
```html
<div class="pos-layout" style="margin:-28px">
```
There is **no** page heading of any kind.
→ **No change needed.** This is correct by design — confirm and document.

### base_admin.html — does it inject headings automatically?
No. The layout wraps content in `<div class="page-content">` and renders
`{% block content %}` inside it. No automatic heading injection.

### CSS interaction points
- `.page-content` → `padding: 28px` (desktop), `padding: 16px` (≤767px).
- `.page-header` → `display:flex; align-items:flex-start; justify-content:space-between; gap:20px; margin-bottom:28px; flex-wrap:wrap`.
- `.page-title` → `font-size:1.6rem; color:var(--brown-dark)`.
- `.page-subtitle` → `font-size:0.85rem; color:#999; margin-top:4px`.
- On mobile (≤575px) `responsive.css` stacks `.page-header` vertically — this is the same behaviour all other pages get. No special treatment needed for Dashboard.
- The POS page zeroes out `.page-content` padding on mobile with `padding-left:0 !important; padding-right:0 !important` inside its own `{% block extra_css %}`. That rule is scoped to the POS stylesheet and is **not** affected by anything we do on Dashboard.

### Standard heading markup (from inventory/list.html)
```html
<div class="page-header">
  <div>
    <h1 class="page-title">Inventory</h1>
    <p class="page-subtitle">Monitor and manage product stock levels</p>
  </div>
  <a href="…" class="btn btn-outline">View Log</a>   <!-- optional action -->
</div>
```
Dashboard needs only the heading portion — no subtitle, no action button.

---

## Implementation Plan

- [ ] 1. Add a `Dashboard` page heading to `templates/dashboard/index.html`.

      The heading block is added as the **very first child** of `{% block content %}`,
      immediately before the `<div id="dashboard-content" …>` opening tag.
      No subtitle is needed — the Dashboard is self-explanatory at the top level.
      No action button is needed in the header row.

      **Exact markup to insert** (one blank line after it for readability):

      ```html
      <div class="page-header">
        <div>
          <h1 class="page-title">Dashboard</h1>
        </div>
      </div>
      ```

      Insert it so the content block becomes:

      ```html
      {% block content %}
      <div class="page-header">
        <div>
          <h1 class="page-title">Dashboard</h1>
        </div>
      </div>

      <div id="dashboard-content" class="dash-loading" aria-busy="true">
      … (unchanged)
      ```

      **Files:** `templates/dashboard/index.html`

      **Verify:**
      - `python manage.py check` — must exit 0 with no errors.
      - Load `/dashboard/` in a browser; confirm exactly one "Dashboard" heading
        is visible as the primary page label, above the stat cards.
      - Confirm no second heading appears in the topbar.
      - Inspect `document.querySelector('#app-layout').getBoundingClientRect().top`
        in the browser console — must return `0` (no reintroduction of the 24px gap).

- [ ] 2. Confirm `orders/pos.html` requires **no changes** — document the intentional design.

      Inspection confirmed: `{% block content %}` in `pos.html` opens directly with
      `<div class="pos-layout" style="margin:-28px">`. There is no `<div class="page-header">`,
      no `<h1 class="page-title">`, and no text containing "POS Terminal" or "Point of Sale"
      inside the content block. This is correct by design.

      The `style="margin:-28px"` on `.pos-layout` is the existing negative-margin
      offset that cancels `.page-content`'s 28px padding so the POS workspace fills
      the full available area edge-to-edge on desktop. This should remain untouched.

      **Files:** none — `templates/orders/pos.html` is unchanged.

      **Verify:**
      - Load `/orders/pos/` in a browser; confirm **no** "POS Terminal", "Point of Sale",
        or "POS" page heading is visible.
      - Confirm the category navigation is the first visible UI element.
      - Confirm no blank gap exists above the category tabs.

---

## CSS Implications

No CSS changes are required.

- The `.page-header` / `.page-title` rules already exist in `static/css/main.css`
  (lines ~479–488) and are used by every other management page.
- Adding the heading to Dashboard uses the same system everyone else uses — no
  new rules, no overrides needed.
- Mobile behaviour: `responsive.css` already stacks `.page-header` vertically on
  narrow screens. The heading will reflow naturally on 320 px–412 px just as it
  does on the Inventory, Products, and Orders pages.
- The Dashboard's own `{% block extra_css %}` does not define any rule that would
  conflict with `.page-header` or `.page-title`.
- The POS mobile override that zeroes `.page-content` padding is scoped inside
  `pos.html`'s own `<style>` block — it is completely unaffected.

---

## Full Verification Checklist

After applying the single change in item 1:

### System check
```
python manage.py check
```
Expected: `System check identified no issues (0 silenced).`

### Dashboard — desktop
- Sidebar expanded: "Dashboard" heading visible, stat cards below it.
- Sidebar collapsed: heading still visible, layout intact.
- No duplicate heading in the topbar.
- `document.querySelector('#app-layout').getBoundingClientRect().top` → `0`.

### Dashboard — mobile (320 px / 360 px / 375 px / 390 px / 412 px)
- "Dashboard" heading appears once, above the quick-action buttons.
- Heading wraps cleanly if needed; no overflow.
- No topbar title added.
- Stat cards, chart, recent orders remain correctly positioned.

### POS — desktop
- Sidebar expanded and collapsed: no page heading visible.
- Category tabs appear as the first content row.
- Product grid, Current Order panel, customer name, payment controls, totals, Place Order all present.
- No blank space above category tabs.

### POS — mobile (320 px / 360 px / 375 px / 390 px / 412 px)
- No page heading visible.
- Category tabs appear immediately.
- Drawer behaviour unchanged.
- No horizontal overflow.

### Other admin pages (sanity)
- Inventory, Products, Categories, Order Management pages all retain their existing headings.
- No regression from the Dashboard addition.

### Preserved: desktop topbar removal
- No `<header class="topbar">` content carrying a page title is present.
- The topbar in `base_admin.html` contains only the mobile hamburger and the orders icon — unchanged.

### Preserved: 24px #app-layout gap fix
- The BOM/whitespace issue was previously fixed in `base.html` or the HTML output.
- This plan makes no changes to `base.html` or `base_admin.html`, so the fix is preserved.

---

## Files Changed

| File | Change |
|------|--------|
| `templates/dashboard/index.html` | Add `<div class="page-header"><div><h1 class="page-title">Dashboard</h1></div></div>` as first child of `{% block content %}` |
| `templates/orders/pos.html` | No change — heading absence is intentional |
| `static/css/main.css` | No change |
| `base_admin.html` | No change |

This is the smallest possible change: one 4-line HTML block inserted in one file.
