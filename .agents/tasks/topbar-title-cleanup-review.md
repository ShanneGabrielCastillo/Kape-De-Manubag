# Topbar page-title removal from admin/staff shared layout

The change removes the redundant `<span class="topbar-title">` from the shared `base_admin.html` topbar, eliminating the duplicate page-name that appeared beside the hamburger toggle on every admin/staff page. Three dead `.topbar-title` CSS rules in `main.css`, `responsive.css`, and `mobile-design-system.css` were also pruned. Four files total — one HTML template and three CSS files — with no backend, routing, sidebar, or per-page template modifications.

Watch for: The `page_title` block is orphaned — it no longer renders anywhere but still exists as dead declarations in all 19 child templates. No behavioral risk today, but it is an ambiguity to clean up (see Issues).

**Verdict**: APPROVED

---

## High-level view

The fix is correctly applied at the shared-layout level. One deletion in `base_admin.html` removes the duplication across all 19 admin/staff pages simultaneously. The topbar is a `display:flex; justify-content:space-between` container, so the left group compresses naturally to just the hamburger button with no layout hacks needed. The `{% block page_title %}` overrides remain in all 19 child templates as harmless no-ops, but they are now dead template code — a follow-up cleanup would remove the ambiguity.

All 19 admin/staff page headings are intact. Dashboard and POS Terminal intentionally lack `<h1>` page headings and are documented as such in both the plan and the report. Customer-facing pages extend `base.html` directly and are unreachable by this change. `.app-layout` has no `padding-top` or `margin-top`, so the previous 24px gap fix is undisturbed.

---

<details>
<summary>Issues (1)</summary>

1. **Orphaned `{% block page_title %}` declarations** — The block no longer renders anywhere in `base_admin.html`. All 19 child templates still override it silently. If a future developer re-adds a `{% block page_title %}` rendering point (e.g., for a `<title>` tag or breadcrumb), those 19 templates will silently populate it with values that may or may not be appropriate. A follow-up pass removing the overrides from child templates — or wiring `page_title` into `{% block title %}` in `base_admin.html` to unify browser-tab titles — would close this gap. Not blocking.

</details>

---

<details>
<summary>Details</summary>

## Confirmed deletion and reflow

The `<span class="topbar-title">` and its `{% block page_title %}` wrapper are gone from `base_admin.html` (confirmed by direct inspection, lines 179–183). Grep for `topbar-title` across all HTML and CSS returns zero matches — element and all three CSS rules fully removed.

Post-change topbar structure:

```
<header class="topbar">            ← flex, justify-content: space-between
  <div>                            ← left group: shrinks to button width
    <button id="sidebar-toggle">   ← hamburger icon only
  </div>
  <div class="topbar-actions">     ← right group: mobile orders badge
    <a id="topbar-orders-link">
      <i data-lucide="clipboard-list">
      <span id="topbar-awaiting-badge">
    </a>
  </div>
</header>
```

No arbitrary widths, negative margins, absolute positioning, or empty spacers introduced.

## Orphaned block declarations (likely — confirmed by grep)

All 19 child templates retain `{% block page_title %}...{% endblock %}` overrides that now render nothing. The report acknowledges this as intentional. The risk is that a future rendering point for this block would silently inherit stale or mismatched values from templates written without that context in mind. See Issues.

</details>

---

<details>
<summary>File map</summary>

| File | Change |
|------|--------|
| `templates/base_admin.html` | Removed `<span class="topbar-title">` and `{% block page_title %}` from topbar left group |
| `static/css/main.css` | Removed dead `.topbar-title` desktop rule |
| `static/css/responsive.css` | Removed dead `.topbar-title` mobile rule |
| `static/css/mobile-design-system.css` | Removed dead `.topbar-title` mobile rule |

</details>
