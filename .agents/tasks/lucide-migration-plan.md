# Lucide Icons Migration Plan — Kape De Manubag
## Version: Lucide 0.469.0 (UMD CDN)

---

## 1. Current Icon Inventory

### 1.1 base.html
- **Mechanism:** Pure HTML / emoji used inside JS `innerHTML` strings and inline `<span>` elements.
- **Emoji in JS `innerHTML` (inside `base.html` inline scripts):**
  - `kdmConfirm` VARIANTS map: `'🗑️'` (danger), `'⚠️'` (warning), `'ℹ️'` (default)
  - `kdm-remove-backdrop` header div: `🗑️` (literal emoji inside a `<div>`)
  - `#kdm-confirm-icon` — the span that receives `iconEl.textContent = opts.icon || variant.icon`
- **No Lucide icons, no FA classes.** All icon logic is emoji set via `.textContent`.
- **JS files that touch these icons:** None — they are self-contained in inline `<script>` blocks inside base.html.

### 1.2 base_admin.html
- **Mechanism:** Emoji inside `<span class="nav-icon">` for every sidebar nav link; one hand-coded inline SVG for the logout icon.
- **Sidebar nav icons (all `<span class="nav-icon">emoji</span>`):**
  | Nav Link | Current emoji |
  |---|---|
  | Dashboard | 📊 |
  | Order Management | 📋 |
  | POS Terminal | 🖥️ |
  | Products | 🍽️ |
  | Categories | 🏷️ |
  | Inventory | 📦 |
  | Sales Reports | 📈 |
  | Staff Accounts | 👥 |
  | Settings | ⚙️ |
  | GCash Settings | 📱 |
  | Activity Log | 📜 |
  | Finance | 💰 |
  | Customer Menu (Store) | 🛒 |
  | Queue Board | 📺 |
- **Brand logo:** `☕` inside `.brand-logo` div — **do not replace** (decorative brand element).
- **Mobile sidebar close button:** `×` plain text — **do not replace** (styled text character).
- **Desktop sidebar collapse button:** `◀` plain text — replace with Lucide.
- **Sidebar toggle (hamburger):** `☰` plain text — replace with Lucide.
- **Topbar order management link:** `📋` emoji — replace with Lucide.
- **Logout:** Inline SVG with door-frame path and arrow (hand-coded, `<span class="logout-icon">`) — replace with Lucide `log-out`.
- **JS files that interact with these icons:** `main.js` (sidebar toggle toggle), `realtime.js` (badge on `.sidebar-link .badge-awaiting-payment`).

### 1.3 templates/partials/eye_icon.html
- **Mechanism:** A hand-coded inline SVG with two `<g>` groups (`.eye-open`, `.eye-closed`) controlled by `display:none` JS toggling.
- **Used by:** `templates/accounts/change_password.html`, `templates/accounts/password_reset_confirm.html`, `templates/accounts/login.html`.
- **The JS in `change_password.html` and `password_reset_confirm.html` selects `.eye-open` and `.eye-closed` by class name and toggles `display`.** This must be preserved exactly.

### 1.4 templates/partials/payment_modal.html
- **Mechanism:** Emoji in text content only (not icon elements).
  - `✓ Confirm Payment` — button label text, not an icon span
  - `⚠️` — inside a `<span>` within the GCash warning div
- **No standalone icon elements to migrate** — the `⚠️` is inline in a paragraph. Replace with Lucide `triangle-alert` in a dedicated `<i>` tag.

### 1.5 templates/partials/info_row.html
- **No icons.** Pure data display. **No changes needed.**

### 1.6 templates/partials/product_image.html
- **No icons.** Pure `<img>` tag. **No changes needed.**

### 1.7 templates/dashboard/index.html
- **Emoji used as `.stat-icon` decorations (inside `.stat-icon` div):**
  - ☀️ (Today's Sales), 📅 (Weekly), 📆 (Monthly), 💰 (Total Revenue)
- **Emoji in card header h3 text:** `🏆 Top Products`, `⚠️ Low Stock`
- **Emoji in button labels:** `🖥️ Open POS`, `📋 Orders`, `+ Add Product`, `📈 Reports`
- **Emoji in empty states:** `🏆`, `📋`, `✅`
- **Emoji in chart empty state:** `⚠️`, `📊`
- **Realtime JS:** `main.js` injects new order rows into `#recent-orders-body` via `innerHTML`; those rows use emoji badge text but no icon elements.
- **Note:** `.stat-icon` divs are decorative background accents hidden on mobile — they can use Lucide icons with `.stat-icon` CSS class.

### 1.8 templates/dashboard/settings.html
- **Emoji in text content:** `⚙️ Configurable Settings` (h3), `📦 Take-Out Packaging Fee` (label text), `💾 Save Changes` (button text), `ℹ️ How Settings Work` (h3)
- **No standalone icon elements.** Replace emoji prefix in h3 titles and button text labels.

### 1.9 templates/dashboard/gcash_settings.html
- **Emoji in text content:** `📱 GCash Payment Settings` (h1), `← Settings` (button), `💳 Account Details` (h3), `🔲 GCash QR Code` (h3), `💾 Save GCash Settings` (button), `ℹ️ How Manual GCash Verification Works` (h3), `⚠️` warning text.
- **GCash brand visual:** The `<img>` for the QR code and account details are NOT icons — preserve entirely.
- **No standalone icon elements.** Replace emoji in h1, h3, and button text.

### 1.10 templates/orders/order_list.html
- **Emoji in page header:** `🖥️ Open POS` button.
- **Emoji in search icon span:** `<span class="order-search-icon">🔍</span>` — replace with Lucide `search`.
- **Emoji in filter button:** `🔧 Filter` — replace text label (use Lucide `list-filter` icon).
- **Emoji in action cells (static HTML rows):** `🖨️` (print button), `💳 Pay` button, `🔍 Verify GCash` button.
- **Emoji injected via JS `innerHTML`:** `💳 Awaiting Payment` badge, `💳 Pay` button, `🖨️` print button — **these are JS-generated HTML in `extra_js` block.** Must update the JS template strings too.
- **Notification badge:** `#sidebar-awaiting-badge` and `#topbar-awaiting-badge` — **preserve entirely, never touch**.

### 1.11 templates/orders/order_detail.html
- **Emoji in header:** `🖨️ Print Receipt` button, `← Orders` button.
- **Emoji in order status section:** `💳 Awaiting Payment` text, `📱 GCash Order`, `📋 GCash Reference`, `⚠️` warning span, `✅ Verify Payment` button, `✕ Reject Payment` button, `✓ Confirm Payment` button, `✕ Cancel Order` button, `▶ Mark as…` button, `✓ Paid via…` badge, `📝 Special Instructions` label.
- **Inline content in order items table:** `📦 Packaging Fee` row text.
- **GCash modal:** `✅` icon in header, `⚠️` icon span.
- **JS in extra_js block:** Button text strings like `'✅ Verify Payment'`, `'⏳ Verifying…'`, `'✕ Reject Payment'` — update JS strings.

### 1.12 templates/orders/pos.html
- **Emoji in search input placeholder:** `🔍 Search products...` — update placeholder text.
- **Emoji in category tabs:** `🍽️ All` — text label, update.
- **Category tab content:** `{{ cat.icon }} {{ cat.name }}` — this comes from the Django model (category.icon is a stored emoji field). **Do not replace** — the category icon is model data, not a template icon.
- **Emoji in order panel header:** `🧾 Current Order`, `📦 Packaging Fee`, `✓`.
- **Emoji in payment radio labels:** `💵 Cash`, `📱 GCash`.
- **Emoji in empty state:** `🛒`.
- **Drawer toggle icon `#pos-drawer-toggle-icon`:** `▲`/`▼` — managed by JS. Replace with Lucide `chevron-up`/`chevron-down` (initialize in JS).
- **JS in extra_js block:** `toastOutOfStock` called with emoji-free text; no icon strings in POS JS.
- **Skeleton HTML in base.html:** `🛒` in `.pos-body` empty state.

### 1.13 templates/orders/cart.html
- **Emoji in page title:** `🛒 Your Cart` heading.
- **Emoji in cart remove span:** `🗑️` inside `<span class="cart-remove">` — replace with Lucide `trash-2`.
- **Emoji in cart warning notices:** `⚠️` inline text.
- **Emoji in empty state:** `🛒`.
- **Emoji in checkout button text:** `→` (arrow text, not an icon).
- **Emoji in clear cart button:** `🗑️ Clear Cart` — replace icon part.

### 1.14 templates/orders/checkout.html
- **Emoji in nav brand:** `☕ Kape De Manubag` — **do not replace** (brand element).
- **Emoji in hints:** `📱 GCash Online:`, `💡 Payment is...`, `📱 You will pay...` — inline text.
- **Emoji in packaging fee span:** `📦 Packaging Fee` — text row.
- **No standalone icon elements.** These are all emoji embedded in text content.

### 1.15 templates/orders/order_success.html
- **Emoji in main success icon:** `✅` in `.success-icon` div — replace with Lucide `circle-check`.
- **Emoji in status badge:** `🍳 {{ order.get_status_display }}` — inline text in badge.
- **Emoji in payment block:** `💳` in a `<span>` — replace with Lucide `banknote`.
- **Emoji in packaging fee:** `📦 Packaging Fee` — text.
- **Emoji in action link:** `📱 Track Your Order Live` — replace icon.

### 1.16 templates/orders/queue_board.html
- **Standalone file (does not extend base.html)** — must add Lucide CDN `<script>` directly.
- **Emoji in board header:** `☕ Kape De Manubag` — **do not replace** (brand).
- **Emoji in waiting badge:** `⏳` — JS-managed text; update JS.
- **Emoji in column headers:** `🍳 Now Preparing`, `✅ Ready for Pickup` — h3 text.
- **Emoji in empty states (static HTML + JS-generated):** `🍳`, `✅` in `.empty-state` divs — update both static HTML and `queue_board.js` `emptyState()` function.
- **Ticker text:** `🔄 Auto-refreshes...` — inline text.
- **JS files:** `queue_board.js` uses emoji in `emptyState()` function and `waiting badge` text.

### 1.17 templates/orders/order_tracker.html
- **Standalone file** — must add Lucide CDN `<script>` directly.
- **Emoji in navbar brand:** `☕` — do not replace.
- **Emoji in step dots (`.step-dot`):** `🍳`, `✅`, `🎉` — replace with Lucide icons.
- **Emoji in ready overlay:** `✅` large emoji, `👍` in button — replace.
- **Emoji in packaging fee row:** `📦 Packaging Fee` — text.
- **JS file `order_tracker.js`:** sets `data.status_emoji` (comes from server), `emojiEl.textContent` — these are server-provided status emojis. The `.tracker-status-emoji` element should keep its server-provided content; do NOT replace it.
- **Emoji in hint text:** `🔄 Connecting...`, `🟢 Live...`, `✓ Live updates stopped...` — plain text inside `#tracker-connection-hint`, managed by JS. Update JS strings.

### 1.18 templates/orders/payment_waiting.html
- **Standalone file** — must add Lucide CDN `<script>` directly.
- **Emoji in hero icon `#waiting-icon`:** Dynamically set by server template and JS (`💳✅`, `📋⏳`, `❌`, `📱`, `💳`) — these are set via `.textContent` by `payment_waiting.js`. Keep as text — they are status indicators driven by JS state.
- **Emoji in GCash info card:** `📱 GCash Payment Details` heading, `📱` in payment detail steps text.
- **Emoji in submission form:** `📨 Submit Payment...` button text.
- **Emoji in pay-status pill:** `✓` in badge text — inline text.
- **Emoji in accepted overlay:** `🎉`, `📱 Track Your Order →` — update.
- **JS file `payment_waiting.js`:** sets icon textContent dynamically — preserve those emoji assignments.
- **GCash QR image:** pure `<img>` — do not touch.

### 1.19 templates/orders/receipt.html
- **Standalone file.** Emoji in brand header: `☕ KAPE DE MANUBAG` — **do not replace**.
- **Emoji in print button:** `🖨️ Print Receipt` — not an icon span, plain button text.
- **No icon elements to migrate.**

### 1.20 templates/orders/partials/status_select.html
- **No icons.** Pure select/badge. **No changes needed.**

### 1.21 templates/menu/index.html
- **Emoji in nav brand:** `☕` — do not replace.
- **Emoji in cart FAB:** `🛒` inside `<a class="cart-fab">` — replace with Lucide `shopping-cart`.
- **Emoji in chatbot button:** `💬` inside `<button id="kdm-chat-btn">` — replace with Lucide `message-circle`.
- **Emoji in category navigation tabs:** `🍽️ All`, `{{ cat.icon }} {{ cat.name }}` — `cat.icon` is model data, only the hardcoded `🍽️ All` tab can be replaced; leave `{{ cat.icon }}` alone.
- **Emoji in menu search placeholder:** `🔍 Search menu...` — update placeholder text.
- **Chatbot avatar:** `☕` inside `.kdm-chat-avatar` — **do not replace** (brand).
- **Send button:** `➤` inside `<button id="kdm-chat-send">` — replace with Lucide `send`.
- **Quick replies in chatbot:** Emoji in quick reply button labels (`🍽️`, `⭐`, `💳`, `🥡`, `🛒`, `📦`) — these are hardcoded in `chatbot.js`. Update JS strings.
- **Chatbot unread dot `#kdm-chat-dot`:** CSS-only dot, no icon.

### 1.22 templates/menu/product_list.html
- **Emoji in filter bar search icon:** `<span class="pfb-search-icon">🔍</span>` — replace with Lucide `search`.
- **Emoji in select prefix icon:** `<span class="pfb-select-icon">⊞</span>` — replace with Lucide `tags`.
- **Emoji in select arrow:** `<span class="pfb-select-arrow">▾</span>` — replace with Lucide `chevron-down`.
- **Emoji in filter button:** `🔧 Filter` — replace icon.
- **Emoji in clear button:** `↺ Clear` — replace with Lucide `rotate-ccw`.
- **Emoji in warning alert:** `⚠️` inline.
- **Emoji in mobile card builder JS (extra_js):** `✏ Edit`, `📦 Stock`, `🚫 Deactivate`, `✅ Reactivate` — update JS HTML strings.
- **Emoji in active/inactive badge text in JS:** `● ACTIVE`, `● INACTIVE` — these are text, keep or swap to CSS dots.

### 1.23 templates/menu/product_form.html
- **Emoji in warning alert:** `⚠️` inline text.
- **No icon elements.** No changes needed beyond the inherited base_admin.html sidebar.

### 1.24 templates/menu/category_list.html
- **Emoji in category icon column:** `{{ cat.icon }}` — model data, **do not replace**.
- **Emoji in packaging badge:** `📦 Required`, `🥤 None` — inside badge spans, replace.
- **Emoji in warning alert:** `⚠️` text.

### 1.25 templates/menu/category_form.html
- **No icons.** No changes needed.

### 1.26 templates/inventory/list.html
- **Emoji in `.stat-icon` decorators:** `📦`, `✅`, `⚠️`, `🚫` — replace with Lucide.
- **Emoji in alert:** `⚠️` text.
- **Emoji in restock modal header:** `📦 Restock Product` — h4 text, replace.

### 1.27 templates/inventory/log.html
- **No icon elements.** No changes needed.

### 1.28 templates/reports/index.html
- **Emoji in stat-icons:** `💰`, `📋`, `📊` — replace with Lucide.
- **Emoji in card header h3:** `🏆 Top Products`, `📅 Daily Breakdown` — replace icon part.
- **Emoji in export button:** `📊 Export Excel` — replace icon.

### 1.29 templates/finance/index.html
- **Emoji in page header:** `📋 Full History` button.
- **Emoji in card header:** `✓ Saved`, `⚠️ Auto-generated` badges (text).
- **Emoji in field labels / sub-labels:** `📱 GCash Sales`, `✓` label, `⚠️` warning label — inline text.
- **Emoji in summary section:** `−` characters (not emoji).
- **Emoji in buttons:** `💾 Save Finance Record`, `🖨️ Print Report` — replace icon parts.
- **Finance history mobile cards:** `🖨️` button, `💰` empty state.

### 1.30 templates/finance/history.html
- **Emoji in action buttons:** `🖨️` print button.
- **Emoji in empty state mobile:** `💰`.

### 1.31 templates/finance/print.html
- **Standalone file.** `☕ KAPE DE MANUBAG` — brand, do not replace. `🖨️ Print` / `Close` buttons — plain button text.
- **No icon elements.**

### 1.32 templates/accounts/login.html
- **Emoji in brand:** `☕ Kape De Manubag` — do not replace.
- **Password toggle:** Uses a hand-coded inline SVG (same pattern as eye_icon.html) with `#eye-open` / `#eye-closed` groups toggled by JS.
- **No `<span class="nav-icon">`, no Lucide needed here** — the inline SVG is functional and already styled. **Replace with Lucide eye / eye-off** using the same JS toggle pattern.

### 1.33 templates/accounts/profile.html
- **Emoji in profile edit badge:** `✏️` inside `.profile-edit-badge` span — replace with Lucide `pencil`.
- **Remove Photo button:** `🗑️ Remove Photo` — replace icon part.
- **Card header:** `🔒 Password` — replace with Lucide `lock-keyhole`.
- **Profile image `<img>`:** The `<img id="avatar-preview">` must NOT be replaced.

### 1.34 templates/accounts/change_password.html
- **Password toggle buttons:** Use `{% include "partials/eye_icon.html" %}` (3 instances). The JS selects `.eye-open` and `.eye-closed` by class name. After migration, the `eye_icon.html` partial is replaced with Lucide, and the JS toggle logic updates accordingly.
- **Submit button text:** `🔒 Change Password` — replace icon part.

### 1.35 templates/accounts/staff_list.html
- **No icon elements.** No changes needed.

### 1.36 templates/accounts/staff_form.html
- **No icon elements.** No changes needed.

### 1.37 templates/accounts/password_reset_request.html
- **Emoji brand:** `☕` — do not replace.
- **Emoji standalone icon:** `🔑` in a `<div>` — replace with Lucide `key-round`.

### 1.38 templates/accounts/password_reset_done.html
- **Emoji brand:** `☕` — do not replace.
- **Emoji standalone icon:** `📧` in a `<div>` — replace with Lucide `mail`.

### 1.39 templates/accounts/password_reset_confirm.html
- **Emoji standalone icon (invalid link):** `⚠️` in a `<div>` — replace with Lucide `triangle-alert`.
- **Emoji standalone icon (valid link):** `🔒` in a `<div>` — replace with Lucide `lock-keyhole`.
- **Password toggle buttons:** Use `{% include "partials/eye_icon.html" %}` (2 instances) — same migration as change_password.html.
- **Submit button text:** `🔒 Set New Password` — replace icon part.

### 1.40 templates/audit/activity_log.html
- **Emoji in active filter chips:** `🔍`, `📁`, `👤`, `📅` — inline text spans, replace with Lucide inside chip spans.
- **Emoji in action column TD:** Every `{% if log.action == "..." %}` branch has emoji (💳, ✖, 🔄, ✅, 📤, ➕, ✏️, 🚫, 👤, 🔒, 🔓, 🔑, 📦, 💰, ⚙️) — replace with Lucide `<i>` icons.
- **Emoji in empty state:** `📜` — replace with Lucide `logs`.

### 1.41 templates/404.html and templates/500.html
- **Emoji brand:** `☕` in both — do not replace (brand).
- **Emoji in action link text:** `🍽️ Return to Menu`, `← Try Again`, `🍽️ View Menu`, `← Go Back` — replace icon parts.

### 1.42 JS files — icon interactions
| File | Icon-related behavior |
|---|---|
| `main.js` | `showToast()` uses emoji chars `'✓', '✕', 'ℹ', '⚠'` as `icons` object values; `openPaymentModal` sets `methodDisplay.textContent = '📱 GCash' / '💵 Cash'`; no direct DOM icon creation. |
| `realtime.js` | `showNewOrderNotification` calls `showToast` with `🔔`; badges selected by `.sidebar-link .badge-pending` / `.badge-awaiting-payment` (badge elements, not icon elements). |
| `queue_board.js` | `emptyState()` function accepts emoji strings passed as arguments from caller; empty state `icon` param is `'🍳'` / `'✅'`. |
| `order_tracker.js` | `formatWait()` and `formatPositionSub()` return strings with emoji (`✅`, `🎉`, `❌`); `setHint()` sets text with emoji; `emojiEl.textContent` set to server-provided emoji. These are all status text, not icon elements. |
| `payment_waiting.js` | Sets `iconEl.textContent` to emoji (`💳✅`, `🎉`, `❌`) — status icons in hero. |
| `chatbot.js` | Quick reply button labels contain emoji — update the `QUICK_QUESTIONS` array. |
| `responsive-tables.js` | No icons. |

---

## 2. Integration Strategy

### 2.1 Script placement decision
`base_admin.html` extends `base.html`. `base.html` handles all JS loading via `{% block extra_js %}`. The Lucide script must load **before** `lucide.createIcons()` is called but after the DOM is ready.

**Decision: Add the Lucide CDN `<script>` to `base.html` only**, directly before the existing `<script src="{% static 'js/main.js' %}">` line. Since `base_admin.html` extends `base.html`, both admin and non-admin pages get Lucide from one place.

Pages that do NOT extend `base.html` (standalone pages) each need their own Lucide `<script>` tag:
- `templates/orders/queue_board.html`
- `templates/orders/order_tracker.html`
- `templates/orders/payment_waiting.html`
- `templates/orders/receipt.html` — no icons to migrate, skip
- `templates/finance/print.html` — no icons to migrate, skip

### 2.2 Script tag to add
```html
<script src="https://unpkg.com/lucide@0.469.0/dist/umd/lucide.min.js"></script>
```
Place it immediately before `<script src="{% static 'js/main.js' %}"></script>` in `base.html`.

### 2.3 Initialization
Add at the top of `main.js` (after the CSRF helper, before sidebar toggle):
```js
// ── Lucide Icons ──
document.addEventListener('DOMContentLoaded', function () {
  if (typeof lucide !== 'undefined') lucide.createIcons();
});
```
This covers all pages that load `main.js`.

### 2.4 Standalone pages
Each standalone page that has Lucide icons needs its own inline init after the CDN script:
```html
<script src="https://unpkg.com/lucide@0.469.0/dist/umd/lucide.min.js"></script>
<script>document.addEventListener('DOMContentLoaded', function(){ if(typeof lucide!=='undefined') lucide.createIcons(); });</script>
```

---

## 3. CSS Additions

### 3.1 Existing `.nav-icon` rule analysis
In `main.css`, the existing rule is:
```css
.sidebar-link .nav-icon { font-size: 1.1rem; width: 22px; text-align: center; }
```
`font-size` applies to emoji (text characters) and has **no effect on SVG elements**. After migration, the emoji is gone and the `<i data-lucide>` renders an inline SVG. Without explicit `width`/`height` on the SVG, the icon defaults to Lucide's built-in 24×24 which may exceed the `22px` width slot.

**Resolution:** The CSS additions below include an explicit `width: 20px; height: 20px` on `.nav-icon` that overrides the SVG's intrinsic size. The `font-size: 1.1rem` can remain (it won't hurt SVGs) but `width` takes over sizing duty.

In `responsive.css`, the collapsed sidebar rule:
```css
.sidebar-collapsed .sidebar .sidebar-link .nav-icon { font-size: 1.2rem; width: auto; text-align: center; flex-shrink: 0; }
```
`width: auto` must also gain an SVG-compatible override for the collapsed state.

### 3.2 CSS block to append to `static/css/main.css`
Append this block at the **end** of `main.css`:

```css
/* =====================================================
   LUCIDE ICONS — Kape De Manubag
   Icon sizing utilities for Lucide SVG icons.
   These rules control SVG dimensions since Lucide icons
   are <svg> elements and font-size has no effect on SVGs.
   ===================================================== */

/* Base rule: applies to any element with data-lucide attribute
   or the .lucide class added by lucide.createIcons() */
[data-lucide], .lucide {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  vertical-align: middle;
  stroke-width: 1.75;
  flex-shrink: 0;
}

/* Sidebar nav icons: 20×20 — matches the 22px width slot with 1px padding headroom */
.nav-icon {
  width: 20px;
  height: 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  /* Keep text-align for any remaining non-SVG fallback */
  text-align: center;
}
/* Ensure the SVG inside .nav-icon fills the container */
.nav-icon svg,
.nav-icon [data-lucide] {
  width: 100%;
  height: 100%;
}

/* Topbar icons: 22×22 */
.topbar-icon {
  width: 22px;
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

/* Button icons: 16×16 — used inside .btn elements */
.btn-icon {
  width: 16px;
  height: 16px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

/* Status icons in badges and indicators: 16×16 */
.status-icon {
  width: 16px;
  height: 16px;
  display: inline-flex;
  align-items: center;
  flex-shrink: 0;
}

/* Action icons in table action buttons: 22×22 */
.action-icon {
  width: 22px;
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

/* Chat icons in chatbot UI: 20×20 */
.chat-icon {
  width: 20px;
  height: 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

/* Stat card decorative background icons: scaled for visual accent */
.stat-icon [data-lucide],
.stat-icon .lucide {
  width: 2rem;
  height: 2rem;
  opacity: 0.15;
}

/* Sidebar collapsed state: override width:auto back to icon size */
.sidebar-collapsed .sidebar .sidebar-link .nav-icon {
  width: 22px;
  height: 22px;
}

/* END LUCIDE ICONS */
```

---

## 4. JS Helper — `window.reinitLucide`

### 4.1 Add to `main.js`
After the existing `lucide.createIcons()` DOMContentLoaded call, add:

```js
// ── Lucide reinit helper ──
// Called after any dynamic HTML insertion (AJAX rows, mobile cards, modals)
// to activate Lucide icons in the newly injected markup.
window.reinitLucide = function () {
  if (typeof lucide !== 'undefined') lucide.createIcons();
};
```

### 4.2 Files that need to call `window.reinitLucide`
| File | When to call | Where |
|---|---|---|
| `main.js` | After `initResponsiveTables()` call (line ~`if (typeof window.initResponsiveTables === 'function') { ... }`) in the `new_order` SSE handler in `order_list.html`'s `extra_js` block | After the `initResponsiveTables()` call in the SSE handler |
| `responsive-tables.js` | After `initResponsiveTables()` builds the mobile cards DOM | At end of `processTable()` function |
| `order_list.html extra_js` | After injecting new order row HTML via `row.innerHTML = ...` | Add `window.reinitLucide && window.reinitLucide()` after `tbody.prepend(row)` |

---

## 5. Per-Template Replacement Map

### DECISION on wrapping pattern
- For `<span class="nav-icon">emoji</span>` → becomes `<i data-lucide="icon-name" class="nav-icon"></i>`
- For `<span>emoji text label</span>` in buttons → becomes `<i data-lucide="icon-name" class="btn-icon"></i> Label Text`
- For stat-icon decorative divs → `<i data-lucide="icon-name"></i>` inside `.stat-icon`
- For inline emoji-only in text (e.g. filter chips) → `<i data-lucide="icon-name" class="status-icon"></i>`

---

### 5.1 base.html
| Element | Current | Replacement | CSS class |
|---|---|---|---|
| Lucide CDN script | (none) | Add `<script src="https://unpkg.com/lucide@0.469.0/dist/umd/lucide.min.js"></script>` before `main.js` script tag | — |
| `kdmConfirm` VARIANTS emoji (JS) | `'🗑️'`, `'⚠️'`, `'ℹ️'` | Keep as-is — these set `.textContent` on `#kdm-confirm-icon` which is a plain `<span>`, not an SVG target. They are fallback text, not icon elements. **No change.** | — |
| `kdm-remove-backdrop` header icon div | `<div style="...font-size:1.4rem">🗑️</div>` | `<i data-lucide="trash-2" style="width:28px;height:28px;stroke:currentColor"></i>` (inline style for sizing within the header) | — |

### 5.2 base_admin.html
| Element | Current | Replacement | CSS class |
|---|---|---|---|
| Dashboard nav | `<span class="nav-icon">📊</span>` | `<i data-lucide="layout-dashboard" class="nav-icon"></i>` | `.nav-icon` |
| Order Management nav | `<span class="nav-icon">📋</span>` | `<i data-lucide="clipboard-list" class="nav-icon"></i>` | `.nav-icon` |
| POS Terminal nav | `<span class="nav-icon">🖥️</span>` | `<i data-lucide="monitor" class="nav-icon"></i>` | `.nav-icon` |
| Products nav | `<span class="nav-icon">🍽️</span>` | `<i data-lucide="package" class="nav-icon"></i>` | `.nav-icon` |
| Categories nav | `<span class="nav-icon">🏷️</span>` | `<i data-lucide="tags" class="nav-icon"></i>` | `.nav-icon` |
| Inventory nav | `<span class="nav-icon">📦</span>` | `<i data-lucide="boxes" class="nav-icon"></i>` | `.nav-icon` |
| Sales Reports nav | `<span class="nav-icon">📈</span>` | `<i data-lucide="chart-column" class="nav-icon"></i>` | `.nav-icon` |
| Staff Accounts nav | `<span class="nav-icon">👥</span>` | `<i data-lucide="user-round" class="nav-icon"></i>` | `.nav-icon` |
| Settings nav | `<span class="nav-icon">⚙️</span>` | `<i data-lucide="settings" class="nav-icon"></i>` | `.nav-icon` |
| GCash Settings nav | `<span class="nav-icon">📱</span>` | `<i data-lucide="credit-card" class="nav-icon"></i>` | `.nav-icon` |
| Activity Log nav | `<span class="nav-icon">📜</span>` | `<i data-lucide="logs" class="nav-icon"></i>` | `.nav-icon` |
| Finance nav | `<span class="nav-icon">💰</span>` | `<i data-lucide="wallet" class="nav-icon"></i>` | `.nav-icon` |
| Customer Menu (Store) nav | `<span class="nav-icon">🛒</span>` | `<i data-lucide="shopping-bag" class="nav-icon"></i>` | `.nav-icon` |
| Queue Board nav | `<span class="nav-icon">📺</span>` | `<i data-lucide="monitor" class="nav-icon"></i>` | `.nav-icon` |
| Logout inline SVG | `<span class="logout-icon"><svg ...>...</svg></span>` | `<span class="logout-icon"><i data-lucide="log-out"></i></span>` | via `.logout-icon` existing CSS (`width:18px; height:18px; stroke:currentColor`) |
| Desktop collapse btn `◀` | `<span class="collapse-icon" aria-hidden="true">◀</span>` | `<i data-lucide="panel-left-close" class="collapse-icon" aria-hidden="true" style="width:14px;height:14px"></i>` and JS updates the icon name on toggle |
| Mobile sidebar toggle `☰` | `<button id="sidebar-toggle"...>☰</button>` | `<button id="sidebar-toggle"...><i data-lucide="menu"></i></button>` |  |
| Topbar order link emoji `📋` | `<a id="topbar-orders-link"...>📋<span id="topbar-awaiting-badge"...` | `<a id="topbar-orders-link"...><i data-lucide="clipboard-list" class="topbar-icon"></i><span id="topbar-awaiting-badge"...` | `.topbar-icon` |
| `#sidebar-awaiting-badge` | Preserve exactly as-is | **NO CHANGE** | — |
| `#topbar-awaiting-badge` | Preserve exactly as-is | **NO CHANGE** | — |

**Sidebar collapse JS update:** In `main.js`, the sidebar collapse button toggle currently uses `◀`/`▶` CSS rotation. After migration, the button contains `<i data-lucide="panel-left-close">`. When toggled, swap the `data-lucide` attribute value to `panel-left-open` and call `lucide.createIcons()` on that element. The CSS `rotate(180deg)` on `.collapse-icon` can remain as a fallback but should be removed since the icon name swap handles the visual change directly.

### 5.3 partials/eye_icon.html
Replace the entire file content with a Lucide-based implementation that preserves the `.eye-open` / `.eye-closed` toggle API that existing JS depends on:

```html
{# Lucide eye/eye-off toggle — wrapping groups preserve the .eye-open / .eye-closed JS API #}
<i data-lucide="eye" class="eye-open" style="width:20px;height:20px;display:inline-flex;align-items:center" aria-hidden="true"></i>
<i data-lucide="eye-off" class="eye-closed" style="width:20px;height:20px;display:none;align-items:center" aria-hidden="true"></i>
```

The existing JS in `change_password.html` and `password_reset_confirm.html` does:
```js
var open   = btn.querySelector('.eye-open');
var closed = btn.querySelector('.eye-closed');
if (open)   open.style.display   = isHidden ? 'none' : '';
if (closed) closed.style.display = isHidden ? ''     : 'none';
```
This still works with `<i>` elements. After Lucide processes them, `<i>` becomes `<svg>`, but `.eye-open` and `.eye-closed` class names are preserved on the SVG elements. The `display` style toggle works identically.

**Important:** After replacing the partial, call `lucide.createIcons()` on the page after DOMContentLoaded. The password toggle buttons also need their `reinitLucide()` called if they are dynamically inserted, but they are static HTML so the initial `createIcons()` call handles them.

### 5.4 partials/payment_modal.html
| Element | Current | Replacement | CSS class |
|---|---|---|---|
| GCash warning `⚠️` span | `<span style="font-size:1rem;flex-shrink:0;margin-top:1px">⚠️</span>` | `<i data-lucide="triangle-alert" class="status-icon" style="flex-shrink:0;margin-top:1px"></i>` | `.status-icon` |

Button label text `✓ Confirm Payment` — remove the `✓` prefix or replace with icon: `<i data-lucide="circle-check" class="btn-icon"></i> Confirm Payment`. The modal close button `×` is a plain text character — **do not replace**.

### 5.5 dashboard/index.html
| Element | Current | Replacement | CSS class |
|---|---|---|---|
| Today's Sales stat-icon | `<div class="stat-icon">☀️</div>` | `<div class="stat-icon"><i data-lucide="calendar"></i></div>` | via `.stat-icon` rule |
| Weekly Sales stat-icon | `<div class="stat-icon">📅</div>` | `<div class="stat-icon"><i data-lucide="calendar-range"></i></div>` | via `.stat-icon` rule |
| Monthly Sales stat-icon | `<div class="stat-icon">📆</div>` | `<div class="stat-icon"><i data-lucide="calendar-range"></i></div>` | via `.stat-icon` rule |
| Total Revenue stat-icon | `<div class="stat-icon">💰</div>` | `<div class="stat-icon"><i data-lucide="wallet"></i></div>` | via `.stat-icon` rule |
| "🏆 Top Products" h3 | `<h3>🏆 Top Products</h3>` | `<h3><i data-lucide="award" class="btn-icon"></i> Top Products</h3>` | `.btn-icon` |
| "⚠️ Low Stock" h3 | `<h3>⚠️ Low Stock</h3>` | `<h3><i data-lucide="triangle-alert" class="btn-icon"></i> Low Stock</h3>` | `.btn-icon` |
| "🖥️ Open POS" button | `<a ...><span>🖥️</span> Open POS</a>` | `<a ...><i data-lucide="monitor" class="btn-icon"></i> Open POS</a>` | `.btn-icon` |
| "📋 Orders" button | `📋 Orders` | `<i data-lucide="clipboard-list" class="btn-icon"></i> Orders` | `.btn-icon` |
| "📈 Reports" button | `📈 Reports` | `<i data-lucide="chart-column" class="btn-icon"></i> Reports` | `.btn-icon` |
| Empty state icon for chart | `<div class="empty-icon">📊</div>` | `<div class="empty-icon"><i data-lucide="chart-column" style="width:2.2rem;height:2.2rem"></i></div>` | — |
| Top products empty | `<div class="empty-icon">🏆</div>` | `<div class="empty-icon"><i data-lucide="award" style="width:4rem;height:4rem;opacity:0.5"></i></div>` | — |
| Recent orders empty | `<div class="empty-icon">📋</div>` | `<div class="empty-icon"><i data-lucide="clipboard-list" style="width:2.2rem;height:2.2rem"></i></div>` | — |
| Low stock all-ok empty | `<div class="empty-icon">✅</div>` | `<div class="empty-icon"><i data-lucide="circle-check" style="width:4rem;height:4rem;opacity:0.5"></i></div>` | — |

### 5.6 dashboard/settings.html
| Element | Current | Replacement |
|---|---|---|
| Card header "⚙️ Configurable Settings" | `<h3>⚙️ Configurable Settings</h3>` | `<h3><i data-lucide="settings" class="btn-icon"></i> Configurable Settings</h3>` |
| "📦 Take-Out Packaging Fee" label | `{% if setting.key == 'PACKAGING_FEE_PER_ITEM' %} 📦 Take-Out Packaging Fee` | Replace `📦` with `<i data-lucide="package" class="btn-icon"></i>` |
| "💾 Save Changes" button | `💾 Save Changes` | `<i data-lucide="save" class="btn-icon"></i> Save Changes` |
| "ℹ️ How Settings Work" h3 | `<h3>ℹ️ How Settings Work</h3>` | `<h3><i data-lucide="info" class="btn-icon"></i> How Settings Work</h3>` |
| Empty state settings icon | `<div style="font-size:2rem;margin-bottom:8px">⚙️</div>` | `<div style="font-size:2rem;margin-bottom:8px"><i data-lucide="settings" style="width:2rem;height:2rem"></i></div>` |

### 5.7 dashboard/gcash_settings.html
| Element | Current | Replacement |
|---|---|---|
| Page h1 `📱 GCash Payment Settings` | `📱` emoji | `<i data-lucide="credit-card" class="btn-icon"></i>` |
| "← Settings" back link | `← Settings` | `<i data-lucide="arrow-left" class="btn-icon"></i> Settings` |
| `✅ GCash is configured` alert icon | `✅` text | `<i data-lucide="badge-check" class="status-icon"></i>` |
| `⚠️ GCash is not fully configured` alert | `⚠️` text | `<i data-lucide="triangle-alert" class="status-icon"></i>` |
| "💳 Account Details" h3 | `💳` | `<i data-lucide="credit-card" class="btn-icon"></i>` |
| "🔲 GCash QR Code" h3 | `🔲` | `<i data-lucide="image-up" class="btn-icon"></i>` |
| "💾 Save GCash Settings" button | `💾` | `<i data-lucide="save" class="btn-icon"></i>` |
| "ℹ️ How Manual GCash Verification Works" h3 | `ℹ️` | `<i data-lucide="info" class="btn-icon"></i>` |
| `⚠️ Never click Verify...` warning | `⚠️` | `<i data-lucide="triangle-alert" class="status-icon"></i>` |
| `💡 Upload your GCash QR...` hint | `💡` | `<i data-lucide="info" class="status-icon"></i>` |
| GCash QR `<img>` | Preserve entirely | **NO CHANGE** |

### 5.8 orders/order_list.html
| Element | Current | Replacement |
|---|---|---|
| "🖥️ Open POS" button | `🖥️ Open POS` | `<i data-lucide="monitor" class="btn-icon"></i> Open POS` |
| Search icon span | `<span class="order-search-icon">🔍</span>` | `<i data-lucide="search" class="order-search-icon" style="width:1rem;height:1rem;pointer-events:none;color:#aaa"></i>` |
| "🔧 Filter" button | `🔧 Filter` | `<i data-lucide="list-filter" class="btn-icon"></i> Filter` |
| "↺ Clear" button | `↺ Clear` | `<i data-lucide="rotate-ccw" class="btn-icon"></i> Clear` |
| `🖨️` print button (static) | `🖨️` | `<i data-lucide="printer" class="btn-icon"></i>` |
| `💳 Pay` button (static) | `💳 Pay` | `<i data-lucide="banknote" class="btn-icon"></i> Pay` |
| `🔍 Verify GCash` button (static) | `🔍 Verify GCash` | `<i data-lucide="search" class="btn-icon"></i> Verify GCash` |
| `📋 GCash — Verify` badge | `📋 GCash — Verify` badge text | `<i data-lucide="clipboard-list" class="status-icon"></i> GCash — Verify` |
| `❌ GCash Rejected` badge | `❌` | `<i data-lucide="circle-x" class="status-icon"></i>` |
| JS-generated HTML in `extra_js` block | Multiple emoji in `innerHTML` strings | Update all strings (see §5.8.1) |
| `ⓘ Showing ... orders` footer hint | `ⓘ` | `<i data-lucide="info" class="status-icon"></i>` |

**§5.8.1 JS-generated HTML strings to update in `order_list.html` extra_js:**
- `statusCellHtml` for `isAwaitingPayment`: `'💳 Awaiting Payment'` → `'<i data-lucide="banknote" class="status-icon"></i> Awaiting Payment'`
- `paymentCellHtml` Pay button: `'💳 Pay'` → `'<i data-lucide="banknote" class="btn-icon"></i> Pay'`
- `actionCellHtml` print link: `'🖨️'` → `'<i data-lucide="printer" class="btn-icon"></i>'`
- After inserting row via `tbody.prepend(row)`, call `window.reinitLucide && window.reinitLucide()`
- Status changed handler: `payCell.innerHTML` for awaiting payment: update `'💳 Pay'` → Lucide icon

### 5.9 orders/order_detail.html
| Element | Current | Replacement |
|---|---|---|
| "🖨️ Print Receipt" button | `🖨️ Print Receipt` | `<i data-lucide="printer" class="btn-icon"></i> Print Receipt` |
| "← Orders" button | `← Orders` | `<i data-lucide="arrow-left" class="btn-icon"></i> Orders` |
| "💳 Awaiting Payment" heading text | `💳` | `<i data-lucide="banknote" class="status-icon"></i>` |
| "📱 GCash Order" notice | `📱` | `<i data-lucide="smartphone" class="status-icon"></i>` |
| "📋 GCash Reference Submitted" heading | `📋` | `<i data-lucide="clipboard-list" class="status-icon"></i>` |
| `⚠️` in GCash pending warning | `⚠️` span | `<i data-lucide="triangle-alert" class="status-icon" style="flex-shrink:0;margin-top:1px"></i>` |
| "✅ Verify Payment" button | `✅` | `<i data-lucide="circle-check" class="btn-icon"></i>` |
| "✕ Reject Payment" button | `✕` | `<i data-lucide="circle-x" class="btn-icon"></i>` |
| "❌ GCash payment was rejected" notice | `❌` | `<i data-lucide="circle-x" class="status-icon"></i>` |
| "✓ Confirm Payment" button | `✓` | `<i data-lucide="circle-check" class="btn-icon"></i>` |
| "✕ Cancel Order" button | `✕` | `<i data-lucide="circle-x" class="btn-icon"></i>` |
| "▶ Mark as..." button | `▶` | `<i data-lucide="chevron-right" class="btn-icon"></i>` |
| "✓ Paid via..." badge | `✓` | `<i data-lucide="circle-check" class="status-icon"></i>` |
| "📝 Special Instructions" label | `📝` | `<i data-lucide="info" class="status-icon"></i>` |
| "📦 Packaging Fee" row | `📦` text | `<i data-lucide="package" class="status-icon"></i>` |
| GCash verify modal header icon `✅` | `<span style="font-size:1.25rem">✅</span>` | `<i data-lucide="circle-check" style="width:1.25rem;height:1.25rem;color:#fff"></i>` |
| GCash modal warning `⚠️` | `⚠️` span | `<i data-lucide="triangle-alert" class="status-icon" style="flex-shrink:0;margin-top:1px"></i>` |
| JS strings in extra_js | `'✅ Verify Payment'`, `'⏳ Verifying…'`, `'✕ Reject Payment'`, `'⏳ Rejecting…'`, `'▶ Mark as ...'`, `'⏳ Updating…'` | Update: use plain text or `<i data-lucide="...">` in JS-set `textContent` strings — **NOTE:** elements set via `.textContent` cannot contain HTML. For JS strings used in `textContent`, remove emoji. For strings used in `innerHTML`, use icon tags and call `reinitLucide`. |

### 5.10 orders/pos.html
| Element | Current | Replacement |
|---|---|---|
| Search placeholder | `🔍 Search products...` | `Search products...` (remove emoji from placeholder) |
| "🍽️ All" category tab | `🍽️ All` | `<i data-lucide="package" class="btn-icon"></i> All` |
| Category tabs `{{ cat.icon }}` | Model data — **do not replace** | **NO CHANGE** |
| "🧾 Current Order" POS header text | `🧾 Current Order` | `<i data-lucide="receipt-text" class="btn-icon"></i> Current Order` |
| "📦 Packaging Fee" row | `📦 Packaging Fee` span | `<i data-lucide="package" class="btn-icon"></i> Packaging Fee` |
| "💵 Cash" radio label | `💵 Cash` | `<i data-lucide="banknote" class="status-icon"></i> Cash` |
| "📱 GCash" radio label | `📱 GCash` | `<i data-lucide="smartphone" class="status-icon"></i> GCash` |
| Empty state `🛒` | `<div class="empty-icon">🛒</div>` | `<div class="empty-icon"><i data-lucide="shopping-cart" style="width:4rem;height:4rem;opacity:0.5"></i></div>` |
| Drawer toggle icon `▲`/`▼` | `<span id="pos-drawer-toggle-icon">▲</span>` | `<i id="pos-drawer-toggle-icon" data-lucide="chevron-up" style="width:0.9rem;height:0.9rem"></i>` — JS must also update `data-lucide` attribute instead of `textContent` and call `lucide.createIcons()` on the element |
| "Place Order ✓" button | `✓` text at end | Remove `✓` or replace with `<i data-lucide="circle-check" class="btn-icon"></i>` before text |

**POS drawer JS update:** The `setExpanded()` function currently does `icon.textContent = isExpanded ? '▼' : '▲'`. Change to:
```js
icon.setAttribute('data-lucide', isExpanded ? 'chevron-down' : 'chevron-up');
lucide.createIcons({ nodes: [icon] });
```

### 5.11 orders/cart.html
| Element | Current | Replacement |
|---|---|---|
| "🛒 Your Cart" heading | `🛒 Your Cart` | `<i data-lucide="shopping-cart" class="btn-icon"></i> Your Cart` |
| `🗑️` cart-remove span | `<span class="cart-remove">🗑️</span>` | `<span class="cart-remove"><i data-lucide="trash-2" class="btn-icon"></i></span>` |
| `⚠️` cart item notice | `⚠️` inline | `<i data-lucide="triangle-alert" class="status-icon"></i>` |
| Empty state `🛒` | `<div class="empty-icon">🛒</div>` | `<div class="empty-icon"><i data-lucide="shopping-cart" style="width:4rem;height:4rem;opacity:0.5"></i></div>` |
| `🗑️ Clear Cart` button | `🗑️ Clear Cart` | `<i data-lucide="trash-2" class="btn-icon"></i> Clear Cart` |
| "← Continue Shopping" link | `← Continue Shopping` | `<i data-lucide="arrow-left" class="btn-icon"></i> Continue Shopping` |
| Stale session notice `⏱️` | `⏱️` | `<i data-lucide="clock-alert" class="status-icon"></i>` |

### 5.12 orders/checkout.html
- No standalone icon elements beyond nav brand and inline text hints. 
- `📱 GCash Online:` / `💡 Payment is made...` / `📦 Packaging Fee` — these are text content inside `<p>` and `<span>`, not icon elements.
- **Minimal migration needed:** Replace `📦` in packaging fee row text with `<i data-lucide="package" class="status-icon"></i>`, replace payment hint emojis `💡` and `📱` with Lucide icons.
- `←Back to Cart` link: `<i data-lucide="arrow-left" class="btn-icon"></i> Back to Cart`

### 5.13 orders/order_success.html
| Element | Current | Replacement |
|---|---|---|
| `.success-icon` div | `<div class="success-icon">✅</div>` | `<div class="success-icon"><i data-lucide="circle-check-big" style="width:5rem;height:5rem;color:var(--green-success)"></i></div>` |
| Status badge `🍳` | `🍳 {{ order.get_status_display }}` | `<i data-lucide="cooking-pot" class="status-icon"></i> {{ order.get_status_display }}` |
| Payment icon span `💳` | `<span style="font-size:1.3rem">💳</span>` | `<i data-lucide="banknote" style="width:1.3rem;height:1.3rem;flex-shrink:0"></i>` |
| `📦 Packaging Fee` | `📦` | `<i data-lucide="package" class="status-icon"></i>` |
| "📱 Track Your Order Live" button | `📱` | `<i data-lucide="smartphone" class="btn-icon"></i>` |

### 5.14 orders/queue_board.html (standalone)
Add Lucide CDN script + init before `</body>`.

| Element | Current | Replacement |
|---|---|---|
| "🍳 Now Preparing" column title | `🍳 Now Preparing` | `<i data-lucide="cooking-pot" style="width:1.3rem;height:1.3rem;vertical-align:middle;margin-right:4px"></i> Now Preparing` |
| "✅ Ready for Pickup" column title | `✅ Ready for Pickup` | `<i data-lucide="circle-check" style="width:1.3rem;height:1.3rem;vertical-align:middle;margin-right:4px"></i> Ready for Pickup` |
| Static empty state icon `🍳` | `<div class="empty-icon">🍳</div>` | `<div class="empty-icon"><i data-lucide="cooking-pot" style="width:2.5rem;height:2.5rem;opacity:0.4"></i></div>` |
| Static empty state icon `✅` | `<div class="empty-icon">✅</div>` | `<div class="empty-icon"><i data-lucide="circle-check" style="width:2.5rem;height:2.5rem;opacity:0.4"></i></div>` |
| Ticker text `🔄` | `🔄 Auto-refreshes...` | Remove emoji — plain text: `Auto-refreshes every 5 seconds` |

**`queue_board.js` updates:**
- `emptyState(icon, text)` function: The board column sync calls it with `emptyIcon: '🍳'` etc. Replace: pass HTML string for icon: `emptyIcon: '<i data-lucide="cooking-pot" style="..."></i>'`. Then in `emptyState()`, set `div.innerHTML = ...icon...text...` and call `lucide.createIcons({ nodes: [div] })`.
- `waiting badge` text `'⏳ ${data.waiting_count} waiting'` → `data.waiting_count + ' waiting'` (remove emoji).

### 5.15 orders/order_tracker.html (standalone)
Add Lucide CDN script + init.

| Element | Current | Replacement |
|---|---|---|
| Step dot "🍳" (Preparing) | `<div class="step-dot">🍳</div>` | `<div class="step-dot"><i data-lucide="cooking-pot" style="width:1.1rem;height:1.1rem"></i></div>` |
| Step dot "✅" (Ready) | `<div class="step-dot">✅</div>` | `<div class="step-dot"><i data-lucide="circle-check" style="width:1.1rem;height:1.1rem"></i></div>` |
| Step dot "🎉" (Complete) | `<div class="step-dot">🎉</div>` | `<div class="step-dot"><i data-lucide="badge-check" style="width:1.1rem;height:1.1rem"></i></div>` |
| Ready overlay `✅` big emoji | `<div class="ready-overlay-emoji">✅</div>` | `<div class="ready-overlay-emoji"><i data-lucide="circle-check" style="width:4rem;height:4rem;color:var(--status-ready)"></i></div>` |
| Ready overlay "Got it! 👍" button | `Got it! 👍` | `Got it!` (remove emoji from button) |
| `← Menu` nav link | `← Menu` | `<i data-lucide="arrow-left" class="btn-icon"></i> Menu` |
| "💳 Please go to cashier" awaiting payment notice | `💳` | `<i data-lucide="banknote" class="status-icon"></i>` |
| "📦 Packaging Fee" item row | `📦` | `<i data-lucide="package" class="status-icon"></i>` |
| `#tracker-status-emoji` div | Set by JS via `emojiEl.textContent = data.status_emoji` — server-provided | **NO CHANGE** — preserve server-provided emoji content |
| Connection hint text in JS (`order_tracker.js`) | `'🔄 Reconnecting...'`, `'🟢 Live...'`, `'✓ Live updates stopped'`, `'⚠️ Connection issue...'` | Remove emoji from `setHint()` text strings in `order_tracker.js` |

### 5.16 orders/payment_waiting.html (standalone)
Add Lucide CDN script + init.

| Element | Current | Replacement |
|---|---|---|
| `#waiting-icon` div | Set by template conditionals and JS — emoji via `.textContent` | **NO CHANGE** — preserve JS-driven textContent icons |
| `← Menu` nav link | `← Menu` | `<i data-lucide="arrow-left" class="btn-icon"></i> Menu` |
| GCash info card `📱 GCash Payment Details` h3 | `📱` | `<i data-lucide="smartphone" style="width:1rem;height:1rem;vertical-align:middle"></i>` |
| Amount label / how-to-pay steps text `📱` / `1. Open GCash...` | `📱` inline text | `<i data-lucide="smartphone" class="status-icon"></i>` |
| `📨 Submit Payment for Verification` button | `📨` | `<i data-lucide="send" class="btn-icon"></i>` |
| GCash pending card / rejected notices `❌` | `❌` inline | `<i data-lucide="circle-x" class="status-icon"></i>` |
| Accepted overlay `🎉` | `<div class="accepted-overlay-emoji">🎉</div>` | `<div class="accepted-overlay-emoji"><i data-lucide="circle-check-big" style="width:4rem;height:4rem;color:#1976d2"></i></div>` |
| "📱 Track Your Order →" tracker link | `📱` | `<i data-lucide="smartphone" class="btn-icon"></i>` |
| `⚠️ GCash payment details not configured` notice | `⚠️` | `<i data-lucide="triangle-alert" class="status-icon"></i>` |
| GCash QR `<img>` | Preserve entirely | **NO CHANGE** |
| JS `payment_waiting.js` hint strings `🔄`, `🟢`, `⚠️` | Emoji in `setHint()` calls | Remove emoji from hint strings |
| JS GCash form submit button text `'📨 Submit Payment for Verification'` | `📨` in JS `.textContent` | Remove emoji: `'Submit Payment for Verification'` |

### 5.17 menu/index.html
| Element | Current | Replacement |
|---|---|---|
| Cart FAB `🛒` | `<a class="cart-fab">🛒<span class="badge-count">...</span></a>` | `<a class="cart-fab"><i data-lucide="shopping-cart" class="chat-icon"></i><span class="badge-count">...</span></a>` |
| Chatbot button `💬` | `<button id="kdm-chat-btn">💬</button>` | `<button id="kdm-chat-btn"><i data-lucide="message-circle" class="chat-icon"></i></button>` |
| Chatbot send button `➤` | `<button id="kdm-chat-send">➤</button>` | `<button id="kdm-chat-send"><i data-lucide="send" class="chat-icon"></i></button>` |
| "🍽️ All" category tab | `🍽️ All` | `<i data-lucide="package" class="btn-icon"></i> All` |
| `{{ cat.icon }}` in tabs | Model data | **NO CHANGE** |
| Search placeholder `🔍` | `🔍 Search menu...` | `Search menu...` |
| Empty state `☕` | `<div class="empty-icon">☕</div>` | Keep as-is — this is the brand/decorative empty state |

**`chatbot.js` updates:**
- `QUICK_QUESTIONS` array: remove emoji from label strings or keep them as decorative text (they are set as button `textContent`). Since quick reply buttons use `.textContent`, replacing with icons isn't possible via textContent. **Decision: Keep quick reply emoji as plain text decorations** — they are user-facing button labels that benefit from emoji for quick scanability. Do not replace them with Lucide.
- Close button `×` in chat header — plain text char, keep.

### 5.18 menu/product_list.html
| Element | Current | Replacement |
|---|---|---|
| Search icon span | `<span class="pfb-search-icon">🔍</span>` | `<i data-lucide="search" class="pfb-search-icon" style="position:absolute;left:14px;top:50%;transform:translateY(-50%);pointer-events:none;z-index:1"></i>` |
| Select prefix icon | `<span class="pfb-select-icon">⊞</span>` | `<i data-lucide="tags" class="pfb-select-icon" style="position:absolute;left:14px;font-size:1rem;pointer-events:none;z-index:1;color:var(--brown-mid)"></i>` |
| Select arrow | `<span class="pfb-select-arrow">▾</span>` | `<i data-lucide="chevron-down" class="pfb-select-arrow" style="position:absolute;right:14px;pointer-events:none;color:var(--brown-mid);width:1rem;height:1rem"></i>` |
| "🔧 Filter" button | `🔧 Filter` | `<i data-lucide="list-filter" class="btn-icon"></i> Filter` |
| "↺ Clear" button | `↺ Clear` | `<i data-lucide="rotate-ccw" class="btn-icon"></i> Clear` |
| Warning alert `⚠️` | `⚠️` | `<i data-lucide="triangle-alert" class="status-icon"></i>` |
| JS mobile card builder — `'✏ Edit'` | `✏ Edit` in card innerHTML | `<i data-lucide="pencil" class="btn-icon"></i> Edit` + call `reinitLucide` |
| JS mobile card builder — `'📦 Stock'` | `📦 Stock` | `<i data-lucide="packages" class="btn-icon"></i> Stock` |
| JS mobile card builder — `'🚫 Deactivate'` | `🚫 Deactivate` | `<i data-lucide="circle-x" class="btn-icon"></i> Deactivate` |
| JS mobile card builder — `'✅ Reactivate'` | `✅ Reactivate` | `<i data-lucide="circle-check" class="btn-icon"></i> Reactivate` |
| JS mobile card builder — `'📦 Stock'` label in meta | `📦 Stock` span | `<i data-lucide="boxes" class="status-icon"></i> Stock` |
| After `buildProductCards()` DOM insertion | — | Add `window.reinitLucide && window.reinitLucide()` at end of `buildProductCards()` |

### 5.19 menu/category_list.html
| Element | Current | Replacement |
|---|---|---|
| `{{ cat.icon }}` column | Model data | **NO CHANGE** |
| `📦 Required` packaging badge | `📦 Required` text inside badge | `<i data-lucide="package" class="status-icon"></i> Required` |
| `🥤 None` packaging badge | `🥤 None` | `<i data-lucide="circle-x" class="status-icon"></i> None` |
| Warning alert `⚠️` | `⚠️` | `<i data-lucide="triangle-alert" class="status-icon"></i>` |

### 5.20 inventory/list.html
| Element | Current | Replacement |
|---|---|---|
| Total Products stat-icon | `<div class="stat-icon">📦</div>` | `<div class="stat-icon"><i data-lucide="boxes"></i></div>` |
| Active Products stat-icon | `<div class="stat-icon">✅</div>` | `<div class="stat-icon"><i data-lucide="circle-check"></i></div>` |
| Low Stock stat-icon | `<div class="stat-icon">⚠️</div>` | `<div class="stat-icon"><i data-lucide="triangle-alert"></i></div>` |
| Out of Stock stat-icon | `<div class="stat-icon">🚫</div>` | `<div class="stat-icon"><i data-lucide="circle-x"></i></div>` |
| Alert `⚠️` | `⚠️` | `<i data-lucide="triangle-alert" class="status-icon"></i>` |
| "📦 Restock Product" modal h4 | `📦` | `<i data-lucide="package-plus" class="btn-icon"></i>` |

### 5.21 reports/index.html
| Element | Current | Replacement |
|---|---|---|
| Total Revenue stat-icon | `<div class="stat-icon">💰</div>` | `<div class="stat-icon"><i data-lucide="wallet"></i></div>` |
| Total Orders stat-icon | `<div class="stat-icon">📋</div>` | `<div class="stat-icon"><i data-lucide="clipboard-list"></i></div>` |
| Average Order stat-icon | `<div class="stat-icon">📊</div>` | `<div class="stat-icon"><i data-lucide="chart-column"></i></div>` |
| "🏆 Top Products" h3 | `🏆` | `<i data-lucide="award" class="btn-icon"></i>` |
| "📅 Daily Breakdown" h3 | `📅` | `<i data-lucide="calendar-range" class="btn-icon"></i>` |
| "📊 Export Excel" button | `📊` | `<i data-lucide="download" class="btn-icon"></i>` |

### 5.22 finance/index.html
| Element | Current | Replacement |
|---|---|---|
| "📋 Full History" button | `📋` | `<i data-lucide="clock" class="btn-icon"></i>` |
| "📱 GCash Sales" sub-label | `📱` | `<i data-lucide="smartphone" class="status-icon"></i>` |
| "✓ Saved" badge | `✓` text | `<i data-lucide="circle-check" class="status-icon"></i>` |
| "⚠️ Auto-generated" badge | `⚠️` | `<i data-lucide="triangle-alert" class="status-icon"></i>` |
| "⚠️" auto-gen info block | `⚠️` span | `<i data-lucide="triangle-alert" class="status-icon"></i>` |
| "💾 Save Finance Record" button | `💾` | `<i data-lucide="save" class="btn-icon"></i>` |
| "🖨️ Print Report" button | `🖨️` | `<i data-lucide="printer" class="btn-icon"></i>` |
| Empty state `💰` (mobile cards) | `<div class="empty-icon">💰</div>` | `<div class="empty-icon"><i data-lucide="wallet" style="width:4rem;height:4rem;opacity:0.5"></i></div>` |
| Finance history `🖨️` button | `🖨️` | `<i data-lucide="printer" class="btn-icon"></i>` |

### 5.23 finance/history.html
| Element | Current | Replacement |
|---|---|---|
| `🖨️` print button (table) | `🖨️` | `<i data-lucide="printer" class="btn-icon"></i>` |
| `🖨️` print button (mobile card) | `🖨️` | `<i data-lucide="printer" class="btn-icon"></i>` |
| Empty state `💰` | `<div class="empty-icon">💰</div>` | `<div class="empty-icon"><i data-lucide="wallet" style="width:4rem;height:4rem;opacity:0.5"></i></div>` |

### 5.24 accounts/login.html
| Element | Current | Replacement |
|---|---|---|
| Password toggle inline SVG | Hand-coded SVG with `#eye-open` / `#eye-closed` groups | Replace with `<i data-lucide="eye" class="eye-open" style="width:20px;height:20px;display:inline-flex;align-items:center"></i><i data-lucide="eye-off" class="eye-closed" style="width:20px;height:20px;display:none;align-items:center"></i>` and update the JS toggle to target `.eye-open` / `.eye-closed` (same as `eye_icon.html` partial pattern) |
| Brand `☕ Kape De Manubag` | Keep | **NO CHANGE** |

### 5.25 accounts/profile.html
| Element | Current | Replacement |
|---|---|---|
| `.profile-edit-badge` span `✏️` | `<span class="profile-edit-badge">✏️</span>` | `<span class="profile-edit-badge"><i data-lucide="pencil" style="width:12px;height:12px"></i></span>` |
| "🗑️ Remove Photo" button | `🗑️ Remove Photo` | `<i data-lucide="trash-2" class="btn-icon"></i> Remove Photo` |
| "🔒 Password" card header | `<h3>🔒 Password</h3>` | `<h3><i data-lucide="lock-keyhole" class="btn-icon"></i> Password</h3>` |
| Profile image `<img>` | **PRESERVE** — `<img id="avatar-preview">` and `.user-avatar img` | **NO CHANGE** |

### 5.26 accounts/change_password.html
| Element | Current | Replacement |
|---|---|---|
| 3× `{% include "partials/eye_icon.html" %}` | Inline SVG partial | After eye_icon.html migration, these automatically become Lucide icons |
| "🔒 Change Password" button | `🔒 Change Password` | `<i data-lucide="lock-keyhole" class="btn-icon"></i> Change Password` |

### 5.27 accounts/password_reset_request.html
| Element | Current | Replacement |
|---|---|---|
| Standalone icon div `🔑` | `<div style="font-size:2rem;margin-bottom:8px">🔑</div>` | `<div style="margin-bottom:8px"><i data-lucide="key-round" style="width:2rem;height:2rem;color:var(--caramel)"></i></div>` |
| Brand `☕` | Keep | **NO CHANGE** |

### 5.28 accounts/password_reset_done.html
| Element | Current | Replacement |
|---|---|---|
| Standalone icon div `📧` | `<div style="font-size:3rem;margin-bottom:16px">📧</div>` | `<div style="margin-bottom:16px"><i data-lucide="mail" style="width:3rem;height:3rem;color:var(--caramel)"></i></div>` |

### 5.29 accounts/password_reset_confirm.html
| Element | Current | Replacement |
|---|---|---|
| Invalid link icon `⚠️` | `<div style="font-size:2.5rem;margin-bottom:12px">⚠️</div>` | `<div style="margin-bottom:12px"><i data-lucide="triangle-alert" style="width:2.5rem;height:2.5rem;color:var(--gold)"></i></div>` |
| Valid link icon `🔒` | `<div style="font-size:2rem;margin-bottom:8px">🔒</div>` | `<div style="margin-bottom:8px"><i data-lucide="lock-keyhole" style="width:2rem;height:2rem;color:var(--caramel)"></i></div>` |
| 2× `{% include "partials/eye_icon.html" %}` | SVG partial | Auto-migrated after eye_icon.html update |
| "🔒 Set New Password" button | `🔒` | `<i data-lucide="lock-keyhole" class="btn-icon"></i>` |

### 5.30 audit/activity_log.html
**Filter chip spans:** Replace emoji in filter chip text spans with Lucide `<i>` tags inline:
- `🔍 "{{ q }}"` → `<i data-lucide="search" class="status-icon"></i> "{{ q }}"`
- `📁 {{ category }}` → `<i data-lucide="tags" class="status-icon"></i> {{ category }}`
- `👤 User filtered` → `<i data-lucide="user-round" class="status-icon"></i> User filtered`
- `📅 From/To {{ date }}` → `<i data-lucide="calendar" class="status-icon"></i> From/To {{ date }}`

**Action column — replace emoji in every `{% elif log.action == ... %}` branch.** Replace each emoji with the appropriate Lucide `<i>`:

| Action | Current emoji | Lucide icon |
|---|---|---|
| order.payment | `💳` | `<i data-lucide="banknote" class="status-icon"></i>` |
| order.cancel | `✖` | `<i data-lucide="circle-x" class="status-icon"></i>` |
| order.status_changed | `🔄` | `<i data-lucide="arrow-left-right" class="status-icon"></i>` |
| order.accepted | `✅` | `<i data-lucide="circle-check" class="status-icon"></i>` |
| order.gcash_submitted | `📤` | `<i data-lucide="send" class="status-icon"></i>` |
| order.gcash_verified | `✅` | `<i data-lucide="badge-check" class="status-icon"></i>` |
| order.gcash_rejected | `✖` | `<i data-lucide="circle-x" class="status-icon"></i>` |
| product.create | `➕` | `<i data-lucide="plus" class="status-icon"></i>` |
| product.update | `✏️` | `<i data-lucide="pencil" class="status-icon"></i>` |
| product.deactivate | `🚫` | `<i data-lucide="circle-x" class="status-icon"></i>` |
| product.reactivate | `✅` | `<i data-lucide="circle-check" class="status-icon"></i>` |
| product.availability | `👁️` | `<i data-lucide="eye" class="status-icon"></i>` |
| category.create | `➕` | `<i data-lucide="plus" class="status-icon"></i>` |
| category.update | `✏️` | `<i data-lucide="pencil" class="status-icon"></i>` |
| category.deactivate | `🚫` | `<i data-lucide="circle-x" class="status-icon"></i>` |
| category.reactivate | `✅` | `<i data-lucide="circle-check" class="status-icon"></i>` |
| category.delete | `🗑️` | `<i data-lucide="trash-2" class="status-icon"></i>` |
| staff.create | `👤` | `<i data-lucide="user-round" class="status-icon"></i>` |
| staff.deactivate | `🔒` | `<i data-lucide="lock-keyhole" class="status-icon"></i>` |
| staff.activate | `🔓` | `<i data-lucide="circle-check" class="status-icon"></i>` |
| account.password_change | `🔑` | `<i data-lucide="key-round" class="status-icon"></i>` |
| account.password_reset | `🔑` | `<i data-lucide="key-round" class="status-icon"></i>` |
| inventory.restock | `📦` | `<i data-lucide="package-plus" class="status-icon"></i>` |
| finance.create | `💰` | `<i data-lucide="wallet" class="status-icon"></i>` |
| finance.update | `💰` | `<i data-lucide="wallet" class="status-icon"></i>` |
| settings.update | `⚙️` | `<i data-lucide="settings" class="status-icon"></i>` |

**Empty state:** `<div style="font-size:2rem;margin-bottom:8px">📜</div>` → `<div style="margin-bottom:8px"><i data-lucide="logs" style="width:2rem;height:2rem;opacity:0.5"></i></div>`

### 5.31 404.html and 500.html
| Element | Current | Replacement |
|---|---|---|
| Brand icon `☕` | `☕` in `<div>` | **NO CHANGE** (brand) |
| "🍽️ Return to Menu" / "🍽️ View Menu" links | `🍽️` | `<i data-lucide="store" style="width:1rem;height:1rem;vertical-align:middle"></i>` |
| "← Try Again" / "← Go Back" links | `←` | `<i data-lucide="arrow-left" style="width:1rem;height:1rem;vertical-align:middle"></i>` |

---

## 6. Icon Name Mapping (Valid Lucide 0.469.0 Names)

| Concept | Lucide name | Notes |
|---|---|---|
| Dashboard | `layout-dashboard` | |
| Order Management | `clipboard-list` | |
| POS Terminal | `monitor` | |
| Products | `package` | |
| Categories | `tags` | |
| Inventory | `boxes` | |
| Sales Reports | `chart-column` | |
| Finance | `wallet` | |
| Activity Log | `logs` | |
| Settings | `settings` | |
| GCash Settings (nav) | `credit-card` | Card with chip |
| Sidebar expand | `panel-left-open` | |
| Sidebar collapse | `panel-left-close` | |
| ChevronLeft | `chevron-left` | |
| ChevronRight | `chevron-right` | |
| ChevronDown | `chevron-down` | |
| ChevronUp | `chevron-up` | |
| My Profile | `circle-user-round` | |
| Change Password | `key-round` | |
| Logout | `log-out` | |
| Edit / Pencil | `pencil` | |
| Camera | `camera` | |
| Trash/Delete | `trash-2` | |
| Mobile menu | `menu` | |
| Close | `x` | |
| Bell | `bell` | |
| Back / Arrow left | `arrow-left` | |
| Arrow right | `arrow-right` | |
| Eye | `eye` | |
| Eye off | `eye-off` | |
| Printer | `printer` | |
| Search | `search` | |
| Filter | `list-filter` | |
| Refresh | `rotate-ccw` | |
| Clock | `clock` | |
| CookingPot | `cooking-pot` | |
| CircleCheck | `circle-check` | |
| CircleCheckBig | `circle-check-big` | |
| CircleX | `circle-x` | |
| Cash / Banknote | `banknote` | |
| GCash generic payment | `smartphone` | |
| BadgeCheck | `badge-check` | |
| Send | `send` | |
| Cart | `shopping-cart` | |
| ShoppingBag | `shopping-bag` | |
| Plus | `plus` | |
| Minus | `minus` | |
| PackagePlus | `package-plus` | |
| ImageUp | `image-up` | |
| Tag | `tag` | |
| ArrowLeftRight | `arrow-left-right` | |
| StockIn | `arrow-down-to-line` | |
| StockOut | `arrow-up-from-line` | |
| ChartLine | `chart-line` | |
| ChartPie | `chart-pie` | |
| CalendarRange | `calendar-range` | |
| Calendar | `calendar` | |
| Download | `download` | |
| Save | `save` | |
| Calculator | `calculator` | |
| ReceiptText | `receipt-text` | |
| Coins | `coins` | |
| HandCoins | `hand-coins` | |
| UserRound | `user-round` | |
| Store | `store` | |
| LockKeyhole | `lock-keyhole` | |
| Mail | `mail` | |
| MailCheck | `mail-check` | |
| ClockAlert | `clock-alert` | |
| MessageCircle | `message-circle` | |
| Info | `info` | |
| CircleAlert | `circle-alert` | |
| Copy | `copy` | |
| Upload | `upload` | |
| Help | `circle-help` | NOT `circle-question-mark` |
| Award / Trophy | `award` | |
| TriangleAlert | `triangle-alert` | |
| WalletCards | `wallet-cards` | |
| ListOrdered | `list-ordered` | |
| Maximize2 | `maximize-2` | |
| Minimize2 | `minimize-2` | |
| Boxes | `boxes` | |
| Logs | `logs` | |
| Monitor | `monitor` | |
| ReceiptText | `receipt-text` | |

---

## 7. Sidebar Icon Wrapper Note

**Current HTML pattern:**
```html
<span class="nav-icon">📊</span>
```

**After migration:**
```html
<i data-lucide="layout-dashboard" class="nav-icon"></i>
```
When `lucide.createIcons()` processes this, it transforms the `<i>` into an `<svg>` element. The `.nav-icon` class is preserved on the SVG.

**CSS compatibility check:**
The existing `.nav-icon` rule in `main.css` is:
```css
.sidebar-link .nav-icon { font-size: 1.1rem; width: 22px; text-align: center; }
```
- `font-size: 1.1rem` — **no effect on SVGs** (SVG dimensions are controlled by `width`/`height` attributes or CSS `width`/`height` properties). This rule won't break anything but won't size the icon.
- `width: 22px` — **this will size the SVG correctly** if Lucide doesn't override it. Lucide's `createIcons()` by default sets `width="24" height="24"` as HTML attributes, but CSS `width`/`height` properties override HTML attributes. The CSS `width: 22px` on `.nav-icon` will constrain the SVG width correctly.
- However, **height is not set** in the existing rule. The SVG will default to its `height="24"` attribute. Add `height: 20px` to the new `.nav-icon` rule (see Section 3).

**The existing rule is partially compatible** — it correctly sets width but misses height. The new CSS block in Section 3 adds explicit `width: 20px; height: 20px` which fully overrides SVG intrinsic sizing.

**Collapsed sidebar special case:** `responsive.css` has:
```css
.sidebar-collapsed .sidebar .sidebar-link .nav-icon { font-size: 1.2rem; width: auto; text-align: center; flex-shrink: 0; }
```
`width: auto` would let the SVG expand to its natural 24px. The new CSS block adds a corrective override (see Section 3.2).

---

## 8. Notification Badge Preservation

**Critical constraint — do NOT modify these elements:**

1. `#sidebar-awaiting-badge` — in `base_admin.html`:
   ```html
   <span id="sidebar-awaiting-badge" class="sidebar-awaiting-badge" aria-hidden="true" style="display:none">0</span>
   ```
   This element is controlled entirely by `main.js` (`TopbarOrderBadge` module) and `realtime.js`. Its `display` style is set via JavaScript. **The element must remain exactly as-is**, sibling to `.nav-label` inside the Order Management `<a>` tag.

2. `#topbar-awaiting-badge` — in `base_admin.html`:
   ```html
   <span id="topbar-awaiting-badge" class="topbar-awaiting-badge" aria-hidden="true" style="display:none">0</span>
   ```
   Inside `#topbar-orders-link`. When the topbar link's emoji `📋` is replaced with Lucide, the `<span id="topbar-awaiting-badge">` must remain as the **next sibling** of the icon `<i>`. The badge uses `position:absolute; top:-6px; right:-8px` which depends on `.topbar-orders-link` having `position:relative` — that CSS is already in place.

---

## 9. Profile Image Preservation

The `<img>` elements that represent user profile photos must **never be replaced**:
- `base_admin.html`: `<img src="{{ user.profile_image.url }}" alt="...">` inside `.user-avatar`
- `accounts/profile.html`: `<img id="avatar-preview" src="..." class="profile-avatar">` — the live preview image
- The `.profile-edit-badge` span overlay (currently `✏️`) IS replaced with Lucide `pencil` — it is an action indicator, not the photo itself.

---

## 10. GCash Brand Visual Preservation

- In `orders/payment_waiting.html`: the GCash QR code `<img src="{{ gcash_settings.qr_image.url }}" alt="GCash QR Code">` must not be touched.
- In `dashboard/gcash_settings.html`: the `<img src="{{ gcash.qr_image.url }}" alt="Current GCash QR">` preview must not be touched.
- The `smartphone` icon (`<i data-lucide="smartphone">`) is used only for generic GCash payment method indicators in buttons and label text, never to replace a real image.

---

## 11. Implementation Order

Execute in this order to minimize risk and keep the codebase buildable at every step:

### Phase 1 — Foundation (no template changes yet)
1. **Add Lucide CDN script to `base.html`** (Section 2.2) and add `lucide.createIcons()` DOMContentLoaded call.
2. **Append CSS block to `static/css/main.css`** (Section 3.2) — icon sizing utilities.
3. **Add `window.reinitLucide` helper to `static/js/main.js`** (Section 4.1) — enables all subsequent dynamic reinit calls.
4. **Verify foundation:** Run `python manage.py check` from `c:\Users\Shecile\kape_de_manubag_system`. Load the dashboard page; confirm no JS errors.

### Phase 2 — Core admin layout
5. **Migrate `base_admin.html`** — all sidebar nav icons, logout SVG, collapse button, hamburger toggle, topbar order link. This gives every admin page Lucide icons in one edit.
6. **Migrate `partials/eye_icon.html`** — replaces the hand-coded SVG with Lucide eye/eye-off. All pages using this partial (`change_password.html`, `password_reset_confirm.html`, `login.html`) get updated eye icons automatically.
7. **Verify:** Open dashboard; check sidebar, logout, topbar. Open change_password; test eye toggle.

### Phase 3 — Modal partials
8. **Migrate `partials/payment_modal.html`** — warning icon.
9. **Verify:** Open order list; trigger payment modal; check icon.

### Phase 4 — Dashboard templates
10. **Migrate `dashboard/index.html`** — stat icons, card headers, buttons, empty states.
11. **Migrate `dashboard/settings.html`** — header icons, button icons.
12. **Migrate `dashboard/gcash_settings.html`** — icons, preserve QR image.
13. **Verify:** Load each page; confirm all icons render.

### Phase 5 — Order management
14. **Migrate `orders/order_list.html`** — header, search, filter, table cells, JS strings, reinitLucide call.
15. **Migrate `orders/order_detail.html`** — all status icons, action buttons, modal icons.
16. **Migrate `orders/pos.html`** — search, panel header, drawer toggle JS update.
17. **Verify:** Order list, detail, POS — check icons and JS-generated rows.

### Phase 6 — Customer-facing templates
18. **Migrate `orders/cart.html`** — remove icon, heading, empty state.
19. **Migrate `orders/checkout.html`** — packaging fee row, payment hints.
20. **Migrate `orders/order_success.html`** — success icon, badges.
21. **Migrate `menu/index.html`** — cart FAB, chatbot button, send button.
22. **Verify:** Customer flow: menu → cart → checkout → success.

### Phase 7 — Standalone customer pages (add Lucide CDN each)
23. **Migrate `orders/queue_board.html`** — add CDN, column headers, empty states, update JS.
24. **Migrate `orders/order_tracker.html`** — add CDN, step dots, overlay, update JS.
25. **Migrate `orders/payment_waiting.html`** — add CDN, action buttons, overlay.
26. **Verify:** Load queue board, tracker, payment waiting pages.

### Phase 8 — Menu management
27. **Migrate `menu/product_list.html`** — filter icons, JS card builder strings, reinitLucide.
28. **Migrate `menu/category_list.html`** — packaging badges (preserve cat.icon).
29. **Verify:** Products and categories pages.

### Phase 9 — Inventory, reports, finance
30. **Migrate `inventory/list.html`** — stat icons, modal header, alert.
31. **Migrate `reports/index.html`** — stat icons, card headers, export button.
32. **Migrate `finance/index.html`** and **`finance/history.html`** — printer icons, save button, empty states.
33. **Verify:** Load each page.

### Phase 10 — Account templates
34. **Migrate `accounts/login.html`** — eye toggle (inline SVG replacement).
35. **Migrate `accounts/profile.html`** — edit badge, remove button, lock header.
36. **Migrate `accounts/change_password.html`** — button icon (eye icons via partial migration).
37. **Migrate `accounts/password_reset_request.html`** — key icon.
38. **Migrate `accounts/password_reset_done.html`** — mail icon.
39. **Migrate `accounts/password_reset_confirm.html`** — warning/lock icons, button icon.
40. **Verify:** Account flow: login, profile, change password, reset password.

### Phase 11 — Audit and error pages
41. **Migrate `audit/activity_log.html`** — all action column icons, filter chips, empty state.
42. **Migrate `404.html`** and **`500.html`** — menu and back link icons.
43. **Verify:** Load audit log; trigger a 404.

---

## 12. Verification Steps

### 12.1 Django system check
From `c:\Users\Shecile\kape_de_manubag_system`:
```powershell
Set-Location "c:\Users\Shecile\kape_de_manubag_system"; python manage.py check
```
Expected output: `System check identified no issues (0 silenced).` (The `UserWarning` about SECRET_KEY is expected in dev.)

### 12.2 Search for remaining emoji in templates
After completing all migrations, search for residual emoji in template files:
```powershell
# Search for common icon emoji in HTML templates
Get-ChildItem "c:\Users\Shecile\kape_de_manubag_system\templates" -Recurse -Filter "*.html" |
  Select-String -Pattern "📊|📋|🖥️|🍽️|🏷️|📦|📈|👥|⚙️|📱|📜|💰|🛒|📺|📋|🖨️|💳|🔑|📧|🔍|✏️|🗑️|🏆|⚠️|✅|🚫|💾|⚙|☰|◀|▶" -Encoding UTF8 |
  Where-Object { $_.Line -notmatch "cat\.icon|emoji|comment|#\|<!--" } |
  Select-Object Filename, LineNumber, Line
```

### 12.3 Search for remaining emoji in JS files
```powershell
Get-ChildItem "c:\Users\Shecile\kape_de_manubag_system\static\js" -Filter "*.js" |
  Select-String -Pattern "🔔|⏳|🔄|🟢|✓|⚠️|🍳|✅" -Encoding UTF8 |
  Select-Object Filename, LineNumber, Line
```

### 12.4 Verify Lucide icons render
Manual browser check for each template module — open in browser and confirm:
- Sidebar icons appear (not blank spaces)
- Eye toggle on login/change-password works (eye ↔ eye-off)
- Notification badges still appear and animate
- Mobile card builder generates icons in product list

### 12.5 Verify notification badges still function
On the Order Management page, confirm:
- `#sidebar-awaiting-badge` appears/disappears correctly when orders are in awaiting_payment state
- `#topbar-awaiting-badge` appears only on mobile (< 769px) and shows count

### 12.6 Check no layout shift
On the sidebar (both expanded and collapsed), confirm icons are 20–22px and don't push nav labels off-screen.

---

## 13. Funnel Icon Note

The prompt specifies "funnel" → verify this exists in Lucide 0.469.0. As of Lucide 0.469.0, `funnel` is a valid icon name. If it fails to render, fall back to `list-filter`. No template in this project currently uses a funnel icon, so this is a forward reference.

---

## 14. Known Constraints and Assumptions

1. **`{{ cat.icon }}` in sidebar and menu** — Category icons are model-stored emoji. They must not be replaced by this migration. The coder must only replace hardcoded template emoji, never template variables.

2. **JS `.textContent` assignments cannot contain HTML tags.** For `payment_waiting.js`, `order_tracker.js`, and other files that set `el.textContent = 'emoji text'`, the emoji must simply be removed from the string (not replaced with an icon tag). These are status hint messages, and plain text is acceptable.

3. **`eye_icon.html` JS contract.** The JS in `change_password.html` and `password_reset_confirm.html` selects `.eye-open` and `.eye-closed` within the button. This class-name contract must be preserved after migration. The Lucide `<i>` elements with those classes become `<svg>` elements after `createIcons()`, but the class names survive the transformation, so the JS selectors still work.

4. **POS drawer toggle icon.** The current JS does `icon.textContent = '▲'/'▼'`. After migration, the `<i>` becomes `<svg>` after `createIcons()`, and `textContent` on an SVG element would corrupt it. Use `icon.setAttribute('data-lucide', 'chevron-up'/'chevron-down')` followed by `lucide.createIcons({ nodes: [icon] })` for targeted re-render.

5. **`realtime.js` toast with `🔔` emoji** — `showNewOrderNotification` passes `🔔 New Order #...` as a string to `showToast()`. The toast renders HTML via `innerHTML`, so keeping the emoji here as a string is acceptable (it is user-visible notification text, not a styled icon). No change needed.

6. **`main.js` toast icons object** — `{ success: '✓', error: '✕', info: 'ℹ', warning: '⚠' }` — these are set via `toast.innerHTML` template literal. They can optionally be upgraded to Lucide `<i>` tags within the innerHTML string + reinitLucide, but they are small enough that plain Unicode text symbols are acceptable. **Decision: keep as plain text characters** — they are tiny inline symbols, not the focus of this migration.
