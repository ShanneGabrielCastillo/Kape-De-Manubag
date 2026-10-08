# Implementation Plan: Remove Desktop Admin/Staff Topbar

## Investigation Summary

### Topbar element
`base_admin.html` contains a `<header class="topbar">` inside `<main class="main-content">`.
The topbar holds exactly two things:
1. `#sidebar-toggle` — the hamburger button (mobile-only; already hidden on desktop via `#sidebar-toggle { display: none }` in `main.css` and only shown by the `@media (max-width: 768px)` rule).
2. `.topbar-orders-link` / `#topbar-awaiting-badge` — the mobile Order Management badge link (already hidden on desktop via `@media (min-width: 769px) { .topbar-orders-link { display: none !important; } }` in `main.css`).

Both visible topbar items are **already hidden on desktop**. The topbar itself still renders and occupies `height: var(--header-h)` (65px) of vertical space on desktop.

### Desktop breakpoint
The project uses **`min-width: 768px`** as its single desktop breakpoint, established in:
- `main.css` (sidebar collapse, `@media (min-width: 768px)` desktop sidebar rules)
- `responsive.css` (all desktop layout overrides use `min-width: 768px`)
- `mobile-design-system.css` (tablet/desktop rules use `min-width: 768px`)

The existing sidebar-toggle mobile rule uses `@media (max-width: 768px)` (note: ≤768px), and the topbar-orders-link desktop hide uses `@media (min-width: 769px)`. We will use `@media (min-width: 768px)` (the project's established desktop breakpoint) for the new rule.

### `#app-layout` / `.main-content` layout model
```
.app-layout     → display: flex; min-height: 100vh;
  .sidebar      → position: fixed; width: 224px;
  .main-content → margin-left: 224px; flex: 1; display: flex; flex-direction: column;
    .topbar     → height: 65px; position: sticky; top: 0; z-index: 50;
    .page-content → padding: 28px; flex: 1;
```
`.main-content` is a **vertical flex column**. The topbar is the first child, followed by `.page-content`. On desktop, hiding the topbar with `display: none` will allow `.page-content` to start at the very top of `.main-content` — no other offset adjustment is needed.

### Mobile topbar behaviour
On mobile (`@media (max-width: 575.98px)` in `responsive.css`), the topbar is overridden to:
```css
.topbar { position: fixed; top: 0; left: 0; right: 0; height: 56px; z-index: 200; }
.main-content { padding-top: 56px; }
```
This `padding-top: 56px` on `.main-content` compensates for the fixed topbar. It must be preserved on mobile. The desktop-only rule will not touch these mobile rules.

### `--header-h` / POS `calc(100vh - var(--header-h))`
`main.css` contains:
```css
.pos-layout { height: calc(100vh - var(--header-h)); }
```
`--header-h` is `65px`. With the topbar removed on desktop, this POS layout height calculation will be **wrong**: the POS panel would only use `calc(100vh - 65px)` of height even though there is no 65px topbar consuming space.

The fix must override `.pos-layout` to `height: 100vh` on desktop (inside a `@media (min-width: 768px)` block). On mobile, the `@media (max-width: 1024px)` rule already converts `.pos-layout` to a single-column layout with `position: fixed` bottom panel, so that path is unaffected.

### `main-content` `padding-top` on desktop
On desktop, `.main-content` has **no** `padding-top` in `main.css`. The mobile `padding-top: 56px` is scoped to `@media (max-width: 575.98px)` in `responsive.css`. No desktop padding adjustment is needed.

### JavaScript topbar dependencies
`realtime.js` references `document.getElementById('topbar-awaiting-badge')` and `document.getElementById('topbar-orders-link')` via `TopbarOrderBadge`. These DOM elements will still exist in the HTML (the topbar is hidden via CSS, not removed from the DOM). JavaScript will continue to function correctly. No JS changes are required.

### Customer-facing pages
`base.html` (customer pages) does **not** extend `base_admin.html`. Customer pages have their own layout (no sidebar, no `.app-layout`, no `.topbar` from `base_admin.html`). The topbar CSS class `.topbar` does appear on the customer menu nav strip, but the change is scoped to `base_admin.html`'s layout. No customer pages will be affected.

### Previous 24px BOM fix
`#app-layout` starts at `top: 0` because the BOM/whitespace fix was applied to `base_admin.html`. The new CSS rule hides the topbar without touching the HTML structure, so this fix remains intact.

---

## Files to Change

| File | Change |
|------|--------|
| `static/css/main.css` | Add one `@media (min-width: 768px)` block: hide `.topbar` with `display: none` and fix `.pos-layout` height |

That is **the only file that needs to change**. No HTML, no JS, no other CSS files.

---

## Implementation Plan

- [ ] 1. Add a desktop-only CSS rule to `main.css` that hides the topbar and corrects the POS layout height.

      **What to do:** Append the following CSS block at the end of `static/css/main.css` (after all existing rules, before end of file):

      ```css
      /* =====================================================
         DESKTOP TOPBAR REMOVAL
         The desktop topbar is now empty: the hamburger button
         is already hidden on desktop (#sidebar-toggle {display:none}
         base rule), and the mobile order badge is already hidden
         on desktop (min-width:769px rule).  Hide the entire
         topbar element so it occupies no layout space.
         Scoped to the project's established desktop breakpoint
         (min-width:768px) so the mobile topbar is untouched.
         ===================================================== */
      @media (min-width: 768px) {

        /* Remove the topbar from desktop layout entirely.
           .main-content is flex-direction:column so .page-content
           rises naturally to the top — no margin/padding adjustment needed. */
        .topbar {
          display: none;
        }

        /* POS layout height fix: the desktop POS panel previously used
           calc(100vh - var(--header-h)) (= 100vh - 65px) to account for
           the topbar height. With the topbar gone, the full viewport height
           is available. */
        .pos-layout {
          height: 100vh;
        }

      }
      ```

      **Files:** `static/css/main.css`

      **Verify:**
      1. Run `python manage.py check` — should report no issues.
      2. Open the admin interface in a desktop browser (≥768px wide). Confirm:
         - No empty bar above the main content area on any admin/staff page.
         - Main content (page heading + body) starts immediately beside the sidebar.
         - Sidebar is visible, functional, collapsible.
      3. Open the POS Terminal page on desktop. Confirm the product grid and order panel fill the full viewport height with no gap at the top.
      4. Resize the browser to ≤575px. Confirm:
         - The topbar appears at the top of the viewport.
         - The hamburger button is visible.
         - The mobile order badge link is visible.
         - The sidebar overlay opens and closes on hamburger tap.
         - Main content has visible top padding (56px) so it is not hidden behind the fixed topbar.
      5. Confirm `document.querySelector('#app-layout').getBoundingClientRect().top === 0` on desktop (the previous 24px gap fix remains intact).

---

## Answers to Key Questions

**Topbar selector/element:** `<header class="topbar">` in `base_admin.html`, inside `<main class="main-content">`.

**Desktop breakpoint:** `min-width: 768px`. This is the project's single established desktop breakpoint used throughout `main.css`, `responsive.css`, and `mobile-design-system.css`.

**`main-content` padding-top on desktop:** None. Only mobile (`≤575.98px`) applies `padding-top: 56px` to compensate for the fixed topbar. No removal needed for desktop.

**`#app-layout` layout model:** `display: flex` (row). `.sidebar` is `position: fixed`. `.main-content` is `margin-left: 224px; flex: 1; flex-direction: column`. The topbar is the first flex child of `.main-content`. Hiding it with `display: none` causes `.page-content` to occupy from the very top of the content area.

**`calc(100vh - var(--header-h))` rules:** Only `.pos-layout` in `main.css` uses this. Must be overridden to `height: 100vh` on desktop.

**Topbar `position`:** `position: sticky; top: 0` on desktop. `position: fixed` on mobile (set by responsive.css). No separate `top` offset on `.main-content` exists for desktop — only mobile has the compensating `padding-top: 56px`.

**JS topbar dependency:** `realtime.js` `TopbarOrderBadge` module reads `#topbar-awaiting-badge` and `#topbar-orders-link`. These elements remain in the DOM (CSS-hidden only); JS continues to work normally. No JS changes required.

**Topbar shared with customer pages:** No. `base.html` (customer pages) does not extend `base_admin.html`. The topbar in `base_admin.html` is independent of customer pages.

---

## Post-Implementation Verification Checklist

1. `python manage.py check` — no errors.
2. Desktop (≥768px): topbar is absent on all admin/staff pages.
3. Desktop: no empty gap where the topbar was.
4. Desktop: main page headings remain, content starts at the top.
5. Desktop: sidebar expanded — layout correct.
6. Desktop: sidebar collapsed — layout correct.
7. Desktop: POS Terminal — product and order panels fill full viewport height.
8. Mobile 320px: topbar visible, hamburger works, sidebar overlay works.
9. Mobile 360px: topbar visible, notification badge functional.
10. Mobile 375px: topbar visible.
11. Mobile 390px: topbar visible.
12. Mobile 412px: topbar visible.
13. Customer pages: unaffected (no sidebar, no `.topbar` from `base_admin.html`).
14. `document.querySelector('#app-layout').getBoundingClientRect().top === 0` on desktop.
15. No negative margins, no transform hacks, no hardcoded offsets introduced.
