# POS Bottom Whitespace Fix — Report

## 1. Exact Cause

The inline `<style>` block in `templates/orders/pos.html` contained:

```css
.pos-layout { display:grid; grid-template-columns:1fr 380px; height:calc(100vh - 65px); overflow:hidden; }
```

Because inline `<style>` tags are loaded after (and therefore cascade over) external stylesheets, this rule overrode the correct `height: 100vh` rule already present in `main.css` inside the `@media (min-width: 768px)` DESKTOP TOPBAR REMOVAL block. The net effect was that `.pos-layout` was rendered **65px shorter than the viewport**, producing a detached blank strip at the bottom of the left product/catalog area.

## 2. File Responsible

`templates/orders/pos.html` — inline `<style>` block at the top of the file.

## 3. Old Topbar-Related Height Calculation Involved?

**YES.** The `65px` value exactly matches `--header-h: 65px`, the CSS variable that stored the old desktop topbar height. The rule was originally written to account for the topbar sitting above the POS content. When the topbar was removed from the desktop layout, this offset was corrected in `main.css` but the inline override in `pos.html` was left behind, continuing to subtract 65px unnecessarily.

## 4. Exact File Changed

`templates/orders/pos.html`

**Before:**
```css
.pos-layout { display:grid; grid-template-columns:1fr 380px; height:calc(100vh - 65px); overflow:hidden; }
```

**After:**
```css
.pos-layout { display:grid; grid-template-columns:1fr 380px; height:100vh; overflow:hidden; }
```

Only the `height` value was changed. No other properties on that line or anywhere else in the file were modified.

## 5. Layout Fix Applied

Removed the `65px` topbar offset from the `.pos-layout` height declaration so the grid container fills the full viewport height, matching the intent of the existing `main.css` rule.

## 6. Product Card Sizing

Not affected. No product card styles (`.pos-item-thumb`, card dimensions, padding, etc.) were touched. Product cards retain their current dimensions.

## 7. Scrolling

Preserved. `.pos-left { overflow-y: auto }` is untouched. `.pos-right` flex layout is untouched. Independent scrolling for the product catalog and order panel continues to work as before.

## 8. Desktop Sidebar Testing

The fix works for both expanded and collapsed sidebar states. `.pos-layout` sits inside `.main-content`, which already handles the sidebar margin via CSS variables (`--sidebar-w`, `margin-left`). No POS-specific sidebar adjustment is needed or was made.

## 9. Mobile Testing

The mobile override in the same inline `<style>` block:

```css
@media (max-width: 767.98px) {
  .pos-layout { ... height: auto; ... }
}
```

takes precedence over the desktop base rule for viewports ≤ 767.98px. The change to the desktop base rule does not affect mobile. Mobile POS layout is unchanged.

## 10. `#app-layout` BOM/Whitespace Fix

Unaffected. This fix only modifies a single CSS `height` value inside `pos.html`. It does not touch `base_admin.html`, `base.html`, or any whitespace surrounding `#app-layout`. The previous fix that ensured `document.querySelector('#app-layout').getBoundingClientRect().top === 0` on desktop remains in place.

## 11. Result of `python manage.py check`

```
System check identified no issues (0 silenced).
```

Exit code: 0. No Django system check errors or warnings (the `SECRET_KEY` UserWarning is a pre-existing development environment notice unrelated to this change).

---

**Commit:** `fix: remove old topbar offset from .pos-layout height in pos.html`
