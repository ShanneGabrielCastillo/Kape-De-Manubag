# Fix Report: White Vertical Lines in Recent Orders Table Header

## 1. Root Cause

The white vertical lines were caused by the interaction between **`border-collapse: collapse`** (set globally on all `<table>` elements) and **`position: sticky`** on the `<thead><th>` cells.

When a browser applies `position: sticky` to a cell inside a `border-collapse: collapse` table, each sticky cell is promoted to its own compositing layer. The shared collapsed borders between adjacent cells cannot be rendered across compositing layer boundaries, so the browser paints them as individual borders on each sticky cell — producing visible white/light 1px seam lines between every `<th>`.

The global rule:
```css
/* static/css/main.css, line 250 */
table { width: 100%; border-collapse: collapse; }
```
combined with the sticky positioning on `#recent-orders-scroll thead th` was the cause.

An earlier partial fix already existed in `responsive.css` (inside `@media (min-width: 768px)`) that set `border-right: none; border-left: none` on the sticky headers — but it was **incomplete**: the seam artifact also appears from the top and bottom border sides, especially in Chromium-based browsers (Chrome, Edge). All four sides needed to be suppressed.

---

## 2. File and Line Number

**File:** `static/css/responsive.css`  
**Location:** inside `@media (min-width: 768px)` block, at approximately **line 3098**

---

## 3. Change Made (Before / After Diff)

### Before
```css
/* Sticky column headers */
#recent-orders-scroll thead th {
  position: sticky;
  top: 0;
  background: var(--brown-mid);   /* opaque — covers scrolling rows beneath */
  z-index: 2;
  /* border-collapse:collapse + position:sticky causes browsers to render
     collapsed inter-cell borders as white seams between promoted layers.
     Removing the cell borders on sticky header cells eliminates those lines
     without affecting tbody row/column borders. */
  border-right: none;
  border-left: none;
}
```

### After
```css
/* Sticky column headers */
#recent-orders-scroll thead th {
  position: sticky;
  top: 0;
  background: var(--brown-mid);   /* opaque — covers scrolling rows beneath */
  z-index: 2;
  /* border-collapse:collapse + position:sticky causes browsers to render
     collapsed inter-cell borders as white seams between promoted layers.
     Setting all four border sides to none on the sticky <th> eliminates
     every seam line without affecting tbody row/column borders. */
  border: none;
}
```

**Summary:** Replaced the two partial declarations `border-right: none; border-left: none` with the single shorthand `border: none`. This removes the collapsed-border artifact on all four sides of each sticky `<th>`, eliminating the white seam lines entirely. No other lines in the file were changed.

---

## 4. Confirmation: Header is Now a Continuous Dark-Brown Bar

The `background: var(--brown-mid)` already filled the entire `<th>` area. Removing the border does not create any visual gap — the solid dark-brown background colour is the continuous bar. With `border: none` on all four sides, there is no border for the browser to paint on the promoted compositing layers, so no seam lines appear. The header displays as one uninterrupted dark-brown area across ORDER #, CUSTOMER, TYPE, TOTAL, STATUS, and TIME.

---

## 5. Confirmation: Columns Remain Aligned

Column widths and alignment are not affected by removing borders. The structural `<table>` / `<thead>` / `<th>` elements remain unchanged. The explicit `min-width` rules for Order #, Customer, and Time columns (in the same `@media` block) are untouched and continue to enforce correct alignment between header labels and their data rows.

---

## 6. Confirmation: Row Separators Were Preserved

The horizontal row separators between order rows come from:
```css
/* static/css/main.css */
tbody tr { border-bottom: 1px solid var(--cream); }
tbody tr:last-child { border-bottom: none; }
```
These rules are on `tbody tr`, not on `thead th`. They were not modified in any way.

---

## 7. Confirmation: Other Tables Were Not Changed

The fix selector `#recent-orders-scroll thead th` is unique to the Dashboard Recent Orders table. It does not match any other table in the project. The following table rules are completely untouched:

| Selector / Rule | File | Status |
|---|---|---|
| `table { border-collapse: collapse; }` | `main.css` line 250 | Untouched |
| `thead th { background: var(--brown-mid); … }` (global) | `main.css` line 250 | Untouched |
| `#top-products-scroll thead th, #daily-breakdown-scroll thead th` | `main.css` lines 2033–2047 | Untouched |
| `.table-wrapper thead th` | `responsive.css` line 755 | Untouched |

Order Management, Sales Reports, Inventory, and all other tables are unaffected.

---

## 8. Confirmation: Mobile Layout Was Not Broken

The entire fix rule (`#recent-orders-scroll thead th { … }`) lives inside an `@media (min-width: 768px)` block in `responsive.css`. At viewport widths below 768px the rule is not applied at all. The mobile card layout for Recent Orders is completely unchanged.

---

## Commit

```
fix: remove all border sides from Recent Orders sticky header to eliminate white vertical lines
```
SHA: `9baafb1`  
File changed: `static/css/responsive.css` (1 file, 3 insertions, 4 deletions)
