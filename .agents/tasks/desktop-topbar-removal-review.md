# Desktop topbar removal for admin/staff layout

The desktop admin/staff topbar — a sticky `<header class="topbar">` inside `.main-content` — was consuming 65 px of vertical space on desktop despite containing no desktop-visible content (both of its children, the hamburger button and the mobile order badge, were already hidden on desktop by pre-existing CSS rules). The fix hides the topbar element on desktop using a single `@media (min-width: 768px)` block appended to `main.css`, and corrects the POS layout height calculation that was offsetting for the now-absent topbar. One file changed; no HTML, JavaScript, or other CSS files were touched.

Watch for: Between 576 px and 767 px, the topbar renders at full 65 px height but contains nothing visible — a pre-existing dead zone in the project's breakpoint architecture, not introduced by this change. (confirmed — `max-width: 575.98px` in `responsive.css`, `min-width: 768px` in the new rule; non-blocking.)

**Verdict**: APPROVED

---

## High-level view

The `.main-content` container is a `flex-direction: column`. The topbar was its first child; `.page-content` was second. Setting `display: none` on `.topbar` at `min-width: 768px` removes it from the flex flow so `.page-content` naturally occupies the top of the content area — no offset compensation, no padding removal, no positioning hacks needed. The plan correctly identified that `.main-content` carries no `padding-top` on desktop; the 56 px `padding-top` that compensates for the fixed mobile topbar lives inside `@media (max-width: 575.98px)` in `responsive.css` and is untouched.

The POS layout correction (`height: 100vh` replacing `calc(100vh - var(--header-h))`) is scoped to the same `@media (min-width: 768px)` block. At mobile widths the POS page switches to a single-column fixed-panel layout that doesn't use `.pos-layout` height the same way, so this override is safe.

The topbar HTML element stays in the DOM. `realtime.js`'s `TopbarOrderBadge` reads `#topbar-awaiting-badge` and `#topbar-orders-link` by ID; those elements still exist and the JS continues to run without errors. The sidebar notification badge (`#sidebar-awaiting-badge`) is independent and unaffected.

Customer pages extend `templates/base.html` directly, not `base_admin.html`. They carry no `.app-layout` / `.main-content` / `.topbar` structure, so the new CSS rule matches nothing on customer pages.

The previous 24 px gap fix removed a BOM/whitespace text node from `base_admin.html`. This change adds only CSS; `base_admin.html` is unchanged, so the `#app-layout` top-position fix is intact.

---

<details>
<summary>Issues (1)</summary>

1. **Breakpoint dead zone (informational, non-blocking)** — Between 576 px and 767 px, the topbar is rendered (the desktop-hide rule doesn't apply) but its contents remain invisible (hamburger hidden by a base rule, order badge hidden by `min-width: 769px`). This is inherited from the project's pre-existing breakpoint architecture, not introduced here. No action required from this change, but worth tracking if a tablet layout is ever designed.

</details>

<details>
<summary>Details</summary>

### Breakpoint correctness and mobile preservation

The new rule uses `@media (min-width: 768px)`, matching the project's established desktop breakpoint used throughout `main.css`, `responsive.css`, and `mobile-design-system.css`. The mobile topbar rules in `responsive.css` live inside `@media (max-width: 575.98px)` and are structurally independent — a change to the ≥768 px block cannot cascade into the ≤575.98 px block. The mobile topbar's `position: fixed`, `height: 56px`, `z-index: 200`, and the compensating `main-content { padding-top: 56px }` are all untouched.

There is a gap between 576 px and 767 px where neither set of breakpoint rules applies. In that range the topbar renders at its base height (`var(--header-h)` = 65 px) with its contents already hidden. This gap predates this change — the pre-existing hide rules for `#sidebar-toggle` and `.topbar-orders-link` already created it — but the new desktop-hide rule does not close it. Non-blocking; the affected viewport range is between a narrow tablet and a small desktop, and the project does not appear to target that range specifically.

### `.topbar { display: none }` specificity and layout flow

The base `.topbar` rule in `main.css` (line 457) sets `display: flex`. The new `@media (min-width: 768px) { .topbar { display: none } }` block overrides this at the same specificity (single class selector). The media query wins because it appears later in the file and carries the media condition. No specificity conflict exists.

Because `.main-content` is `flex-direction: column` with no `padding-top` on desktop, hiding the first flex child is sufficient to bring `.page-content` flush with the top of the content area. Confirmed: the base `.main-content` rule carries only `margin-left`, `flex: 1`, `display: flex`, `flex-direction: column`, `min-height: 100vh`, and `min-width: 0` — no `padding-top`.

### POS layout height correction

`main.css` defines `.pos-layout { height: calc(100vh - var(--header-h)) }`. With the 65 px topbar removed from the layout, this calculation would have left the POS panel 65 px short of full viewport height. The fix overrides this to `height: 100vh` inside the same `@media (min-width: 768px)` block. The mobile POS layout converts to a stacked single-column view under `@media (max-width: 1024px)` in `main.css` and does not depend on `.pos-layout` height the same way.



</details>

---

<details>
<summary>File map</summary>

| File | Change |
|------|--------|
| `static/css/main.css` | Appended one `@media (min-width: 768px)` block: `.topbar { display: none }` and `.pos-layout { height: 100vh }` |

Full diff: `git show 49b8e03` or `git diff main -- static/css/main.css`

</details>
