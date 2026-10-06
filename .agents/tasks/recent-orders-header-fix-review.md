# Recent Orders sticky-header white vertical lines — CSS fix

The fix removes the visible white seam lines between column headers in the Recent Orders table on the Dashboard. The root cause was the well-known browser compositing artifact that occurs when `position: sticky` is applied to `<th>` cells inside a `border-collapse: collapse` table: each sticky cell is promoted to its own compositing layer, and the shared collapsed borders cannot be painted across layer boundaries, so the browser renders them as individual 1 px white seams. A partial fix already existed — `border-right: none; border-left: none` — but Chromium-based browsers also produce seams from the top and bottom sides, so the two partial declarations were replaced with the shorthand `border: none`.

Watch for: the `border: none` shorthand resets all four border sides on the sticky `<th>` — confirm that no desired border on the header row (e.g. a bottom separator between the header and the first data row) was relying on `border-bottom` being present on the `<th>`. Confirmed: the row separator rule lives on `tbody tr { border-bottom: 1px solid var(--cream); }` in `main.css` (unmodified), so the bottom-of-header line comes from the first `tbody tr` border, not from the `<th>` itself.

**Verdict**: APPROVED

---

## High-level view

The change is a single-property replacement (`border-right: none; border-left: none` → `border: none`) inside an `#recent-orders-scroll thead th` rule that already existed. The selector is uniquely scoped to the Recent Orders table and lives inside an `@media (min-width: 768px)` block, so it has no mobile effect. Every other CSS file, every template, and every Python file is untouched — confirmed by the diff showing exactly one changed file (`static/css/responsive.css`) and zero changes to `templates/` or any `.py`.

The tbody row separators (`border-bottom: 1px solid var(--cream)` on `tbody tr`) live in `main.css` and were not touched. Column `min-width` rules that prevent Order #, Customer, and Time from wrapping also live inside the same `@media` block and are untouched.

<details>
<summary>Issues (0)</summary>

No blocking concerns. No action items.

</details>

<details>
<summary>Details</summary>

### Sticky-header compositing artifact — cause and fix

The global `table { border-collapse: collapse; }` rule in `main.css` sets up the artifact. Under `border-collapse: collapse`, adjacent cells share a single logical border. When a cell is made sticky, it is promoted to a GPU compositing layer; the shared collapsed border from the adjacent cell's layer cannot blend across the boundary at 1× pixel density in Chromium, producing the visible white seam. The pre-existing partial fix covered the left and right sides but missed top and bottom, which are also promoted and seam in Chromium/Edge.

`border: none` removes all four sides from the sticky `<th>` only. The bottom-of-header separator does not come from `<th> border-bottom` — under `border-collapse: collapse` it comes from the shared collapsed border on the first `tbody tr`, which is governed by `tbody tr { border-bottom: 1px solid var(--cream); }` in `main.css` (untouched). Removing `border` from the `<th>` therefore does not eliminate the visual line between header and first data row.

</details>

---

<details>
<summary>File map</summary>

| File | What changed |
|---|---|
| `static/css/responsive.css` | Line 3103–3105: replaced `border-right: none; border-left: none` with `border: none` inside `#recent-orders-scroll thead th` |

Full diff: `git diff HEAD~1 HEAD`

</details>
