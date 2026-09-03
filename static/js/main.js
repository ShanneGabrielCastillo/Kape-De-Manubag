/* =====================================================
   KAPE DE MANUBAG - Main JavaScript
   ===================================================== */

// ── CSRF Token Helper ──
function getCookie(name) {
  let cookieValue = null;
  if (document.cookie && document.cookie !== '') {
    const cookies = document.cookie.split(';');
    for (let cookie of cookies) {
      cookie = cookie.trim();
      if (cookie.substring(0, name.length + 1) === (name + '=')) {
        cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
        break;
      }
    }
  }
  return cookieValue;
}

const CSRF_TOKEN = getCookie('csrftoken');

// ── Toast Notifications ──
const toastContainer = document.createElement('div');
toastContainer.className = 'toast-container';
document.body.appendChild(toastContainer);

function showToast(message, type = 'info', duration = 3500) {
  const icons = { success: '✓', error: '✕', info: 'ℹ', warning: '⚠' };
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerHTML = `
    <span>${icons[type] || 'ℹ'}</span>
    <span>${message}</span>
    <button class="toast-close" onclick="this.parentElement.remove()">×</button>
  `;
  toastContainer.appendChild(toast);
  setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 300); }, duration);
}

// Auto-dismiss Django messages
document.querySelectorAll('.alert').forEach(alert => {
  setTimeout(() => {
    alert.style.opacity = '0';
    alert.style.transition = 'opacity 0.4s';
    setTimeout(() => alert.remove(), 400);
  }, 4000);
  const dismissBtn = alert.querySelector('.alert-dismiss');
  if (dismissBtn) dismissBtn.addEventListener('click', () => alert.remove());
});

// ── Shared formatting / search helpers ────────────────────────────────────────
// Small pure helpers shared by the menu page, the POS terminal and anywhere
// else that formats prices or filters catalog cards. Kept as globals (like
// showToast / getCookie) so inline template scripts can call them too.

function formatPeso(amount) {
  return '₱' + Number(amount).toFixed(2);
}

function normalizeSearchText(s) {
  return (s || '').toLowerCase().replace(/\s+/g, ' ').trim();
}

function toastOutOfStock(name) {
  showToast(`${name} is out of stock`, 'error');
}

// ── Sidebar Toggle ──
const sidebarToggle = document.getElementById('sidebar-toggle');
const sidebar = document.querySelector('.sidebar');
const sidebarOverlay = document.querySelector('.sidebar-overlay');

if (sidebarToggle && sidebar) {
  // Open/close on hamburger click
  sidebarToggle.addEventListener('click', () => {
    sidebar.classList.toggle('open');
    sidebarOverlay && sidebarOverlay.classList.toggle('open');
    // Prevent body scroll when sidebar open
    const isOpen = sidebar.classList.contains('open');
    document.body.classList.toggle('sidebar-open', isOpen);
    document.body.style.overflow = isOpen ? 'hidden' : '';
    // Update aria-expanded
    sidebarToggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
  });

  // Close on overlay click
  if (sidebarOverlay) {
    sidebarOverlay.addEventListener('click', () => {
      sidebar.classList.remove('open');
      sidebarOverlay.classList.remove('open');
      document.body.classList.remove('sidebar-open');
      document.body.style.overflow = '';
      sidebarToggle.setAttribute('aria-expanded', 'false');
    });
  }

  // Close sidebar when any nav link is tapped on mobile
  document.querySelectorAll('.sidebar-link').forEach(link => {
    link.addEventListener('click', () => {
      if (window.innerWidth < 768) {
        sidebar.classList.remove('open');
        sidebarOverlay && sidebarOverlay.classList.remove('open');
        document.body.classList.remove('sidebar-open');
        document.body.style.overflow = '';
        sidebarToggle.setAttribute('aria-expanded', 'false');
      }
    });
  });

  // Close sidebar on Escape key
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && sidebar.classList.contains('open')) {
      sidebar.classList.remove('open');
      sidebarOverlay && sidebarOverlay.classList.remove('open');
      document.body.classList.remove('sidebar-open');
      document.body.style.overflow = '';
      sidebarToggle.setAttribute('aria-expanded', 'false');
      sidebarToggle.focus();
    }
  });
}

// ── Active Nav Link ──
document.querySelectorAll('.sidebar-link').forEach(link => {
  if (link.href === window.location.href || window.location.pathname.startsWith(link.getAttribute('href'))) {
    link.classList.add('active');
  }
});

// ── Add to Cart (AJAX) ──
document.querySelectorAll('.add-to-cart-form').forEach(form => {
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = form.querySelector('.add-btn, [type=submit]');
    if (btn) btn.disabled = true;

    try {
      const response = await fetch(form.action, {
        method: 'POST',
        headers: { 'X-CSRFToken': CSRF_TOKEN, 'X-Requested-With': 'XMLHttpRequest' },
        body: new FormData(form),
      });
      const data = await response.json();
      if (data.success) {
        showToast(data.message || 'Added to cart!', 'success');
        // Update cart count
        const cartCount = document.querySelector('.cart-count');
        if (cartCount) cartCount.textContent = data.cart_count;
        const cartFabBadge = document.querySelector('.cart-fab .badge-count');
        if (cartFabBadge) cartFabBadge.textContent = data.cart_count;
        // Persist the count so the cart page can detect session expiry.
        try { localStorage.setItem('kdm_cart_count', data.cart_count); } catch(e) {}
      } else {
        // Surface the server's reason (e.g. "out of stock") instead of a
        // generic failure.
        showToast(data.error || 'Failed to add to cart', 'error');
      }
    } catch (err) {
      showToast('Network error. Try again.', 'error');
    } finally {
      if (btn) btn.disabled = false;
    }
  });
});

// ── Cart Quantity Controls ──
document.querySelectorAll('.qty-btn').forEach(btn => {
  btn.addEventListener('click', async function() {
    const itemId = this.dataset.itemId;
    const action = this.dataset.action;
    const display = this.parentElement.querySelector('.qty-display');
    let qty = parseInt(display.textContent);

    if (action === 'increase') qty++;
    else if (action === 'decrease') qty--;

    if (qty < 1) {
      if (!confirm('Remove this item from cart?')) return;
    }

    try {
      const url = this.dataset.url || `/orders/cart/update/${itemId}/`;
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'X-CSRFToken': CSRF_TOKEN, 'Content-Type': 'application/x-www-form-urlencoded' },
        body: `quantity=${qty}`,
      });
      const data = await response.json();
      if (data.success) {
        // The server is authoritative: it may have removed the item (out of
        // stock / unavailable) or capped the quantity at what's available.
        if (data.removed || data.quantity <= 0) {
          this.closest('.cart-item-row').remove();
        } else {
          display.textContent = data.quantity;
          const priceEl = this.closest('.cart-item-row').querySelector('.cart-item-price');
          if (priceEl) priceEl.textContent = `₱${data.item_subtotal.toFixed(2)}`;
        }
        if (data.message) showToast(data.message, 'warning', 4000);
        // Update totals
        const totalEl = document.querySelector('.cart-grand-total');
        if (totalEl) totalEl.textContent = `₱${data.cart_total.toFixed(2)}`;
        const cartFabBadge = document.querySelector('.cart-fab .badge-count');
        if (cartFabBadge) cartFabBadge.textContent = data.cart_count;
        try { localStorage.setItem('kdm_cart_count', data.cart_count); } catch(e) {}
      } else {
        showToast(data.error || 'Could not update item. Please try again.', 'error');
      }
    } catch (err) {
      showToast('Error updating cart', 'error');
    }
  });
});

// ── Remove from Cart ──
document.querySelectorAll('.cart-remove').forEach(btn => {
  btn.addEventListener('click', async function() {
    // Walk up to the row to read live values — qty and price are updated
    // in-place by the +/− controls and would be stale in data attributes.
    const row      = this.closest('.cart-item-row');
    const liveQty  = row ? row.querySelector('.qty-display')?.textContent.trim()  : null;
    const livePrice= row ? row.querySelector('.cart-item-price')?.textContent.trim() : null;

    const ok = await kdmRemoveItem({
      name:   this.dataset.name  || 'this item',
      qty:    liveQty  || this.dataset.qty   || '1',
      price:  livePrice|| this.dataset.price || '',
      imgSrc: this.dataset.img   || '',
    });
    if (!ok) return;
    const itemId = this.dataset.itemId;
    try {
      const url = this.dataset.url || `/orders/cart/remove/${itemId}/`;
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'X-CSRFToken': CSRF_TOKEN },
      });
      const data = await response.json();
      if (data.success) {
        this.closest('.cart-item-row').remove();
        // Keep the "N item(s)" header count in sync
        const countEl = document.getElementById('cart-item-count');
        if (countEl) {
          const remaining = document.querySelectorAll('.cart-item-row').length;
          countEl.textContent = remaining === 1 ? '1 item' : `${remaining} items`;
        }
        const totalEl = document.querySelector('.cart-grand-total');
        if (totalEl) totalEl.textContent = `₱${data.cart_total.toFixed(2)}`;
        const cartFabBadge = document.querySelector('.cart-fab .badge-count');
        if (cartFabBadge) cartFabBadge.textContent = data.cart_count;
        try { localStorage.setItem('kdm_cart_count', data.cart_count); } catch(e) {}
        showToast('Item removed', 'info');
      } else {
        showToast(data.error || 'Could not remove item. Please try again.', 'error');
      }
    } catch (err) {
      showToast('Error removing item', 'error');
    }
  });
});

// ── Order Status Update ──
document.querySelectorAll('.status-update-form').forEach(form => {
  const statusSelect = form.querySelector('[name=status]');
  const submitBtn    = form.querySelector('button[type=submit]');
  const hint         = form.querySelector('.complete-unpaid-hint');

  // Read payment status from the closest <tr data-is-paid="..."> so the
  // check works on both the order list (tr-level) and order detail (no tr).
  function isPaid() {
    const row = form.closest('tr[data-is-paid]');
    if (row) return row.dataset.isPaid === 'true';
    // order_detail.html — no <tr> wrapper; use the data attr on the form
    // itself if present, otherwise treat as unknown (allow the server to decide).
    return form.dataset.isPaid !== 'false';
  }

  // Show/hide the inline hint as soon as the cashier changes the select —
  // instant feedback without waiting for a network round-trip.
  function syncHint() {
    if (!hint || !statusSelect) return;
    const willComplete = statusSelect.value === 'completed';
    const blocked      = willComplete && !isPaid();
    hint.style.display  = blocked ? 'block' : 'none';
    if (submitBtn) submitBtn.disabled = blocked;
  }

  if (statusSelect) statusSelect.addEventListener('change', syncHint);

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const orderId = form.dataset.orderId;
    const status  = statusSelect ? statusSelect.value : null;
    if (!status) return;

    // Client-side payment gate: block the request immediately so the cashier
    // sees the inline hint rather than waiting for a server round-trip.
    // The backend enforces the same rule — this is defence-in-depth UX only.
    if (status === 'completed' && !isPaid()) {
      if (hint) hint.style.display = 'block';
      showToast('Cannot complete order — payment not collected yet.', 'error');
      return;
    }

    // Disable the submit button for the duration of the request so rapid
    // double-clicks cannot fire two concurrent status-change requests.
    if (submitBtn) submitBtn.disabled = true;

    try {
      const url = form.dataset.url || `/orders/manage/${orderId}/status/`;
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'X-CSRFToken': CSRF_TOKEN, 'Content-Type': 'application/x-www-form-urlencoded' },
        body: `status=${encodeURIComponent(status)}`,
      });
      const data = await response.json();
      if (data.success) {
        showToast(`Status updated to ${data.status}`, 'success');
        setTimeout(() => location.reload(), 1000);
      } else {
        // Surface the server's rejection reason — this includes transition
        // validation errors and the payment gate so the cashier understands
        // why the change was refused instead of seeing a silent failure.
        showToast(data.error || 'Status update failed', 'error');
        if (submitBtn) submitBtn.disabled = false;
      }
    } catch (err) {
      showToast('Error updating status', 'error');
      if (submitBtn) submitBtn.disabled = false;
    }
  });
});

// ── Product Toggle (Available/Unavailable) ──
document.querySelectorAll('.toggle-product-btn').forEach(btn => {
  btn.addEventListener('click', async function() {
    const productId = this.dataset.productId;
    const willMarkUnavailable = this.textContent.trim() === 'Available';
    const activeOrders = parseInt(this.dataset.activeOrders || '0', 10);
    // Making a product unavailable while live orders reference it deserves an
    // explicit warning before the server-side lifecycle guard is exercised.
    if (willMarkUnavailable && activeOrders > 0) {
      if (!confirm(`Mark this product unavailable? It is currently in ${activeOrders} active order(s) that are still being fulfilled. It will be hidden from the menu and POS immediately, but those orders keep their saved line items and remain fulfillable.`)) return;
    }
    try {
      const url = this.dataset.url || `/manage/products/${productId}/toggle/`;
      const body = willMarkUnavailable && activeOrders > 0
        ? new URLSearchParams({ confirm: '1' })
        : new URLSearchParams();
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'X-CSRFToken': CSRF_TOKEN },
        body,
      });
      const data = await response.json();
      if (data.success) {
        const label = data.is_available ? 'Available' : 'Unavailable';
        this.textContent = label;
        this.className = `badge ${data.is_available ? 'badge-ready' : 'badge-cancelled'}`;
        showToast(`Product marked as ${label}`, 'success');
      } else if (data.requires_confirmation) {
        // Stale page data: server reports live orders we did not know about.
        if (confirm(`${data.message}\n\nClick OK to confirm.`)) {
          const r2 = await fetch(url, {
            method: 'POST',
            headers: { 'X-CSRFToken': CSRF_TOKEN },
            body: new URLSearchParams({ confirm: '1' }),
          });
          const d2 = await r2.json();
          if (d2.success) {
            showToast(`Product marked as ${d2.is_available ? 'Available' : 'Unavailable'}`, 'success');
          } else {
            showToast(d2.error || 'Action failed', 'error');
          }
        }
      } else {
        showToast(data.error || 'Error updating product', 'error');
      }
    } catch (err) {
      showToast('Error updating product', 'error');
    }
  });
});

// ── Confirm Delete Modal ──
document.querySelectorAll('.delete-btn').forEach(btn => {
  btn.addEventListener('click', function(e) {
    e.preventDefault();
    const name = this.dataset.name || 'this item';
    const form = document.querySelector(`#delete-form-${this.dataset.id}`);
    if (confirm(`Are you sure you want to delete "${name}"? This cannot be undone.`)) {
      if (form) form.submit();
      else {
        fetch(this.href, { method: 'POST', headers: { 'X-CSRFToken': CSRF_TOKEN } })
          .then(r => r.json())
          .then(d => { if (d.success) { showToast('Deleted!', 'success'); this.closest('tr, .card')?.remove(); } });
      }
    }
  });
});

// ── Menu Search + Category Filter (menu page) ──
// The search box and the category tabs filter the same product cards, so they
// share one module instead of stepping on each other:
//  - queries and product names are normalized (lowercased, leading/trailing
//    whitespace trimmed, internal whitespace collapsed), so e.g. "  ICED
//    Coffee " still matches "Iced Coffee";
//  - the DOM is queried once on load and cached, keeping keystroke filtering
//    cheap even for long menus;
//  - a search only ever filters the section(s) the active category tab has
//    shown, and can never re-show a section the tab has hidden.
const menuSearchInput = document.querySelector('#menu-search');
const menuCatBtns = Array.from(document.querySelectorAll('.menu-cat-btn'));
const menuSections = Array.from(document.querySelectorAll('.menu-section'));

if (menuSearchInput && menuCatBtns.length) {
  // One pass over the cards: cache each card, its normalized searchable name
  // and its section, so filtering never re-queries the DOM.
  const menuCards = Array.from(document.querySelectorAll('.product-card')).map(card => ({
    col: card.closest('.product-col'),
    section: card.closest('.menu-section'),
    name: normalizeSearchText(card.querySelector('.product-card-name')?.textContent),
  }));

  // A section belongs to the tab the user selected (``all`` or a specific
  // category). Tab visibility is derived from the active button alone -- never
  // from style.display, which the search also writes -- so a search can never
  // mistake its own hiding for the tab's, and clearing the query restores
  // exactly what the tab had shown.
  function activeCategory() {
    const active = menuCatBtns.find((b) => b.classList.contains('active'));
    return active ? active.dataset.cat : 'all';
  }

  function matchesTab(section) {
    const cat = activeCategory();
    return cat === 'all' || section.dataset.cat === cat;
  }

  function applySearch(query) {
    // Filter only the cards inside the section(s) the active tab has shown;
    // sections the tab has hidden are left completely untouched.
    menuCards.forEach(({ col, section, name }) => {
      if (!matchesTab(section)) return;
      col.style.display = (!query || name.includes(query)) ? '' : 'none';
    });
    // Hide tab-shown sections that ended up with no visible cards. With an
    // empty query every card is visible again, so those sections are restored.
    const visiblePerSection = new Map();
    menuCards.forEach(({ col, section }) => {
      if (!matchesTab(section)) return;
      visiblePerSection.set(section, (visiblePerSection.get(section) || 0) + (col.style.display !== 'none' ? 1 : 0));
    });
    menuSections.forEach((section) => {
      if (!matchesTab(section)) return;
      section.style.display = (query && !visiblePerSection.get(section)) ? 'none' : '';
    });
  }

  menuSearchInput.addEventListener('input', () => {
    applySearch(normalizeSearchText(menuSearchInput.value));
  });

  menuCatBtns.forEach((btn) => {
    btn.addEventListener('click', function() {
      menuCatBtns.forEach((b) => b.classList.remove('active'));
      this.classList.add('active');
      const cat = this.dataset.cat;
      menuSections.forEach((section) => {
        section.style.display = (cat === 'all' || section.dataset.cat === cat) ? '' : 'none';
      });
      // Re-run the active query on the newly shown section so the tabs and
      // the search box always agree.
      applySearch(normalizeSearchText(menuSearchInput.value));
    });
  });
}

// ── Payment Modal ──
window.openPaymentModal = function(orderId, orderTotal, paymentUrl) {
  document.getElementById('payment-order-id').value = orderId;
  document.getElementById('payment-form').dataset.url = paymentUrl || '';
  document.getElementById('payment-total').textContent = `₱${parseFloat(orderTotal).toFixed(2)}`;
  document.getElementById('amount-paid').value = '';
  document.getElementById('change-display').textContent = '₱0.00';
  document.getElementById('payment-modal').style.display = 'flex';
};

const amountInput = document.getElementById('amount-paid');
if (amountInput) {
  amountInput.addEventListener('input', function() {
    const total = parseFloat(document.getElementById('payment-total').textContent.replace('₱', '')) || 0;
    const paid = parseFloat(this.value) || 0;
    const change = paid - total;
    document.getElementById('change-display').textContent = `₱${Math.max(0, change).toFixed(2)}`;
  });
}

const paymentForm = document.getElementById('payment-form');
if (paymentForm) {
  paymentForm.addEventListener('submit', async function(e) {
    e.preventDefault();
    const orderId = document.getElementById('payment-order-id').value;
    const formData = new FormData(this);
    const url = this.dataset.url || `/orders/manage/${orderId}/payment/`;

    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'X-CSRFToken': CSRF_TOKEN },
        body: formData,
      });
      const data = await response.json();
      if (data.success) {
        showToast(`Payment accepted! Change: ₱${data.change.toFixed(2)}`, 'success');
        document.getElementById('payment-modal').style.display = 'none';
        setTimeout(() => location.reload(), 1500);
      } else {
        showToast(data.error || 'Payment failed', 'error');
      }
    } catch (err) {
      showToast('Error processing payment', 'error');
    }
  });
}

// ── Checkout: prevent duplicate submission (double-click, slow network,
//    rapid taps) ──
// Two-layer client-side protection:
//   1. data-submitting flag on the form itself — checked first so programmatic
//      form.submit() calls and any browser-native retry are also blocked.
//   2. Button disabled + spinner — visual feedback so the customer knows the
//      request is in flight.
// The server still enforces idempotency via the request_token hidden field, so
// even if this handler were bypassed the backend would never create two orders.
const checkoutForm = document.getElementById('checkout-form');
if (checkoutForm) {
  checkoutForm.addEventListener('submit', function(e) {
    // If a submission is already in flight, swallow the event completely.
    if (this.dataset.submitting === 'true') {
      e.preventDefault();
      return;
    }
    this.dataset.submitting = 'true';
    const btn = this.querySelector('button[type=submit]');
    if (btn) {
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');
      btn.innerHTML = '<span class="spinner"></span> Placing Order…';
    }
    // Network-interruption recovery: if the server does not respond within
    // 30 seconds the spinner would spin forever.  Re-enable the button with
    // a clear message so the customer can tap again — the server-side
    // request_token idempotency ensures a retry cannot create a duplicate.
    const recoveryTimer = setTimeout(() => {
      if (this.dataset.submitting !== 'true') return; // already resolved
      this.dataset.submitting = 'false';
      if (btn) {
        btn.disabled = false;
        btn.removeAttribute('aria-busy');
        btn.innerHTML = 'Try Again →';
        btn.style.background = 'var(--red-accent, #dc3545)';
      }
      // Show a recoverable message above the button
      const existing = this.querySelector('.checkout-network-error');
      if (!existing) {
        const msg = document.createElement('p');
        msg.className = 'checkout-network-error';
        msg.style.cssText = 'color:var(--red-accent,#dc3545);font-size:0.85rem;margin-bottom:10px;text-align:center';
        msg.textContent = 'Network issue — your order was not placed. Please try again.';
        btn.parentNode.insertBefore(msg, btn);
      }
    }, 30000);
    // Store the timer id so a fast successful response can cancel it.
    this.dataset.recoveryTimer = recoveryTimer;
    // If the server returns an error (non-redirect response) the page will
    // reload with a fresh form.  The flag lives on the form element which is
    // replaced on reload, so no cleanup is needed.
  });
}

// ── Close modals on backdrop click ──
document.querySelectorAll('.modal-backdrop').forEach(backdrop => {
  backdrop.addEventListener('click', function(e) {
    if (e.target === this) this.style.display = 'none';
  });
});

// ── Size selector price update ──
document.querySelectorAll('.size-selector').forEach(sel => {
  sel.addEventListener('change', function() {
    const productId = this.dataset.productId;
    const size = this.value;
    const priceEl = document.querySelector(`#price-${productId}`);
    if (!priceEl) return;

    const url = this.dataset.url || `/api/product/${productId}/price/`;
    fetch(`${url}?size=${size}`)
      .then(r => r.json())
      .then(d => {
        // Only touch the price when the response actually carried one; a
        // stale or partial response must never blank out the displayed price.
        if (priceEl && d && typeof d.formatted === 'string') {
          priceEl.textContent = d.formatted;
        }
      })
      .catch(() => { /* offline / server error: keep showing the last price */ });
  });
});

// ── Sales Chart (Dashboard) ──
window.initSalesChart = function(labels, data) {
  const canvas = document.getElementById('sales-chart');
  if (!canvas || typeof Chart === 'undefined') return;

  // Chart.js v4 correct API — canvas._chart does not exist
  const existingChart = Chart.getChart(canvas);
  if (existingChart) {
    existingChart.destroy();
  }

  new Chart(canvas, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'Sales (₱)',
        data,
        borderColor: '#C17A3B',
        backgroundColor: 'rgba(193, 122, 59, 0.1)',
        borderWidth: 2.5,
        pointBackgroundColor: '#C17A3B',
        pointRadius: 4,
        tension: 0.4,
        fill: true,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => `₱${ctx.raw.toFixed(2)}`,
          },
        },
      },
      scales: {
        y: {
          beginAtZero: true,
          grid: { color: 'rgba(0,0,0,0.05)' },
          ticks: {
            callback: v => `₱${v}`,
            font: { size: 11 },
            maxTicksLimit: 6,
          },
        },
        x: {
          grid: { display: false },
          ticks: {
            maxRotation: 45,
            minRotation: 0,
            font: { size: 10 },
            maxTicksLimit: 7,
          },
        },
      },
    },
  });
};

// ── POS System ──
// Duplicate-order protection lives in two layers:
//  - client: ``submitting`` blocks re-entry while a request is in flight and
//    the button is disabled with a spinner, so a double-click / rapid tap
//    cannot even fire a second request;
//  - server: every submission carries a request_token (generated lazily per
//    cart and reused across retries of the same logical order). A replayed
//    token returns the original order, so even a stray duplicate request
//    can never create a second order.
window.POS = {
  items: [],
  packagingFee: 0,
  submitting: false,
  _requestToken: null,
  // Set to true immediately before the post-success page reload so the
  // beforeunload navigation guard does not fire for a normal checkout.
  _checkoutSuccess: false,
  // Live stock snapshot keyed by product id, kept fresh by the realtime
  // inventory_changed event. Lazily seeded from the product cards' data-stock
  // attributes on first use.
  _stock: {},

  clear() {
    this.items = [];
    this._resetRequestToken();
    this.render();
    // Manual cancellation: the temporary cart is gone for good.
    this.clearDraft();
  },

  getStock(id) {
    if (this._stock[id] === undefined) {
      const el = document.querySelector(`.pos-item[data-product-id="${id}"]`);
      this._stock[id] = el ? parseInt(el.dataset.stock || '0', 10) : 0;
    }
    return this._stock[id];
  },

  _generateRequestToken() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }
    // Non-secure contexts (e.g. a LAN POS over plain HTTP) have no
    // randomUUID -- fall back to a random, time-seeded key.
    return `pos-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
  },

  _getRequestToken() {
    if (!this._requestToken) this._requestToken = this._generateRequestToken();
    return this._requestToken;
  },

  _resetRequestToken() {
    this._requestToken = null;
  },

  // ── Draft persistence (accidental-refresh resilience) ──
  // The current order lives in memory; a refresh would lose it. A lightweight
  // draft (items, customer info, order type, idempotency token) is mirrored to
  // sessionStorage after every change, so a refresh restores the order instead
  // of forcing the cashier to rebuild it. The draft is strictly temporary: it
  // dies with the tab, expires after a few hours, and is removed on a
  // successful order or manual Clear.
  _DRAFT_KEY: 'kdm_pos_draft',
  _DRAFT_TTL_MS: 3 * 60 * 60 * 1000,   // 3 hours

  _EMPTY_STATE_HTML:
    '<div class="empty-state" style="padding:20px"><div class="empty-icon">🛒</div><p>No items yet</p></div>',

  _draftPayload() {
    return {
      items: this.items,
      customerName: document.getElementById('pos-customer-name')?.value || '',
      orderType: document.querySelector('[name=pos-order-type]:checked')?.value || 'dine_in',
      requestToken: this._requestToken || null,
      savedAt: new Date().toISOString(),
    };
  },

  saveDraft() {
    try {
      const payload = this._draftPayload();
      // Nothing worth preserving yet -- don't create a draft.
      if (!payload.items.length && !payload.customerName) return;
      sessionStorage.setItem(this._DRAFT_KEY, JSON.stringify(payload));
    } catch (e) {
      // Storage unavailable (private mode / quota): refresh protection is
      // best-effort and the server still guards every submission.
    }
  },

  clearDraft() {
    try { sessionStorage.removeItem(this._DRAFT_KEY); } catch (e) {}
  },

  initDraftPersistence() {
    ['pos-customer-name'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.addEventListener('input', () => this.saveDraft());
    });
    // One change handler for the order-type radios: keep the draft in sync and
    // refresh the packaging-fee preview. (The fee update used to live in
    // pos.html as a second, duplicate listener on these same radios.)
    document.querySelectorAll('[name=pos-order-type]').forEach(r =>
      r.addEventListener('change', () => { this.saveDraft(); this.updatePackagingFee(); })
    );
  },

  async restoreDraft() {
    let raw = null;
    try { raw = sessionStorage.getItem(this._DRAFT_KEY); } catch (e) { return; }
    if (!raw) return;
    let draft;
    try { draft = JSON.parse(raw); } catch (e) { this.clearDraft(); return; }

    // Stale drafts (e.g. a tab left open overnight) are dropped, never
    // restored.
    const age = Date.now() - new Date(draft.savedAt || 0).getTime();
    if (!draft.savedAt || age > this._DRAFT_TTL_MS) { this.clearDraft(); return; }

    // If this draft's idempotency token already created an order (the refresh
    // raced a submission that actually succeeded), the order is complete -- do
    // NOT restore it.
    if (draft.requestToken) {
      try {
        const statusUrl = (window.KDM_URLS && window.KDM_URLS.posDraftStatus) || '/orders/pos/draft-status/';
        const r = await fetch(statusUrl + '?request_token=' + encodeURIComponent(draft.requestToken));
        const data = await r.json();
        if (data.placed) {
          this.clearDraft();
          showToast(`Order ${data.order_number || ''} was already placed`, 'info', 4000);
          return;
        }
      } catch (e) {
        // Network hiccup: restore anyway; the server still prevents
        // duplicates if the order was actually placed.
      }
    }

    // Restore items, re-validating against live stock: drop items that are no
    // longer orderable, cap quantities that now exceed the available stock.
    const restored = [];
    (draft.items || []).forEach(item => {
      if (!item || item.id === undefined) return;
      const stock = this.getStock(item.id);
      if (stock <= 0) {
        showToast(`${item.name || 'An item'} is out of stock and was removed`, 'error', 4000);
        return;
      }
      restored.push({ ...item, qty: Math.max(1, Math.min(item.qty || 1, stock)) });
    });
    this.items = restored;
    this._requestToken = draft.requestToken || null;
    const nameEl = document.getElementById('pos-customer-name');
    if (nameEl) nameEl.value = draft.customerName || '';
    const orderTypeRadio = document.querySelector(`[name=pos-order-type][value="${draft.orderType || 'dine_in'}"]`);
    if (orderTypeRadio) orderTypeRadio.checked = true;

    if (restored.length) {
      showToast('Restored your current order from before the refresh', 'info', 4000);
    }
    this.render();
    this.updatePackagingFee();
  },

  _setSubmitting(loading) {
    const btn = document.getElementById('pos-submit-btn');
    if (!btn) return;
    btn.disabled = loading;
    btn.innerHTML = loading
      ? '<span class="spinner"></span> Placing Order…'
      : 'Place Order ✓';
  },

  addItem(id, name, price, size = 'none') {
    // Availability gate: an out-of-stock product can never be added -- even
    // through a stale card or a size modal that was opened earlier.
    const stock = this.getStock(id);
    if (stock <= 0) {
      toastOutOfStock(name);
      return;
    }
    const key = `${id}-${size}`;
    const existing = this.items.find(i => i.key === key);
    if (existing) {
      if (existing.qty + 1 > stock) {
        this._toastStockLimit(name, stock);
        return;
      }
      existing.qty++;
    } else {
      this.items.push({ key, id, name, price: parseFloat(price), size, qty: 1 });
    }
    // A changed cart is a different logical order -- the next submission
    // must get a fresh token.
    this._resetRequestToken();
    this.render();
    showToast(`${name} added`, 'success', 1500);
  },

  removeItem(key) {
    this.items = this.items.filter(i => i.key !== key);
    this._resetRequestToken();
    this.render();
  },

  _toastStockLimit(name, stock) {
    showToast(`Only ${stock} ${name} left in stock`, 'warning');
  },

  changeQty(key, delta) {
    const item = this.items.find(i => i.key === key);
    if (item) {
      if (delta > 0) {
        const stock = this.getStock(item.id);
        if (stock <= 0) {
          toastOutOfStock(item.name);
          return;
        }
        if (item.qty + delta > stock) {
          this._toastStockLimit(item.name, stock);
          item.qty = stock;
        } else {
          item.qty += delta;
        }
      } else {
        item.qty += delta;
      }
      if (item.qty <= 0) this.items = this.items.filter(i => i.key !== key);
    }
    this._resetRequestToken();
    this.render();
  },

  // ── Live stock updates (realtime inventory_changed events) ──────────────
  // A multi-item order emits one inventory_changed event per product within
  // milliseconds, so bursts are buffered and applied in a single DOM pass
  // instead of touching the page once per event. The live _stock snapshot is
  // updated immediately (availability checks stay current); only the card +
  // order reconciliation work is batched.
  _pendingStock: null,
  _stockFlushTimer: null,

  updateStock(data) {
    const id = String(data.product_id);
    this._stock[id] = data.stock_quantity;
    if (!this._pendingStock) this._pendingStock = {};
    this._pendingStock[id] = data;
    if (this._stockFlushTimer !== null) return;
    this._stockFlushTimer = setTimeout(() => this._flushStockUpdates(), 50);
  },

  _flushStockUpdates() {
    this._stockFlushTimer = null;
    // Testable handle for automated audits: counts how many batched DOM
    // passes ran (a multi-item burst should coalesce into very few).
    window.__kdmPosStockFlushes = (window.__kdmPosStockFlushes || 0) + 1;
    const pending = this._pendingStock;
    this._pendingStock = null;
    if (!pending) return;
    // Snapshot the keys: reconciling one product can re-render the cart, but
    // it never mutates the buffer, so a plain iteration is safe.
    Object.keys(pending).forEach(id => {
      const data = pending[id];
      const stock = data.stock_quantity;

      const orderable = !!(data.is_active && data.is_available && stock > 0);
      document.querySelectorAll(`.pos-item[data-product-id="${id}"]`).forEach(el => {
        el.dataset.stock = stock;
        el.classList.toggle('pos-item--out', !orderable);
        if (orderable) delete el.dataset.out;
        else el.dataset.out = '1';
        this._renderStockBadge(el, data);
        // Rewire the click target: out-of-stock cards toast, in-stock cards add.
        const name = el.querySelector('.item-name')
          ? el.querySelector('.item-name').textContent
          : data.product_name;
        el.setAttribute('onclick', orderable
          ? `handlePosAdd(${id}, ${JSON.stringify(name)}, ${el.dataset.basePrice}, this)`
          : `showToast(${JSON.stringify(`${name} is out of stock`)}, 'error')`);
      });

      // Reconcile the current order: drop items that can no longer be ordered,
      // cap quantities that now exceed the available stock.
      this._reconcileOrderForStock(id, stock, data.product_name, !orderable);
    });
  },

  _renderStockBadge(el, data) {
    const stock = data.stock_quantity;
    const threshold = data.low_stock_threshold || 10;
    let text = null, cls = null;
    if (stock <= 0) { text = 'Out of Stock'; cls = 'stock-badge--out'; }
    else if (stock <= 5) { text = `${stock} left`; cls = 'stock-badge--critical'; }
    else if (stock <= threshold) { text = `${stock} left`; cls = 'stock-badge--low'; }

    let badge = el.querySelector('.stock-badge');
    if (!text) {
      if (badge) badge.remove();
      return;
    }
    if (!badge) {
      badge = document.createElement('span');
      badge.className = 'stock-badge';
      const priceEl = el.querySelector('.item-price');
      el.insertBefore(badge, priceEl);
    }
    badge.textContent = text;
    badge.className = `stock-badge ${cls}`;
  },

  _reconcileOrderForStock(id, stock, name, forceRemove) {
    let changed = false;
    this.items = this.items.map(item => {
      if (String(item.id) !== String(id)) return item;
      if (forceRemove || stock <= 0) {
        changed = true;
        showToast(`${name} is no longer available and was removed from the order`, 'error', 4000);
        return null;
      }
      if (item.qty > stock) {
        changed = true;
        showToast(`Only ${stock} ${name} left — quantity adjusted to ${stock}`, 'warning', 4000);
        return { ...item, qty: stock };
      }
      return item;
    }).filter(Boolean);
    if (changed) {
      this._resetRequestToken();
      this.render();
    }
  },

  getTotal() {
    return this.items.reduce((sum, i) => sum + i.price * i.qty, 0) + this.packagingFee;
  },

  async updatePackagingFee() {
    const orderType = document.querySelector('[name=pos-order-type]:checked')?.value || 'dine_in';
    if (orderType !== 'takeout' || this.items.length === 0) {
      this.packagingFee = 0;
      this._renderFee();
      return;
    }
    const params = new URLSearchParams({
      order_type: orderType,
      items: JSON.stringify(this.items.map(i => ({ product_id: i.id, quantity: i.qty })))
    });
    try {
      const feeUrl = (window.KDM_URLS && window.KDM_URLS.packagingFee) || '/orders/api/packaging-fee/';
      const r = await fetch(feeUrl + '?' + params.toString());
      const d = await r.json();
      this.packagingFee = d.packaging_fee || 0;
    } catch(e) {
      this.packagingFee = 0;
    }
    this._renderFee();
  },

  // Debounced wrapper around updatePackagingFee so rapid consecutive item
  // adds (e.g. quickly tapping three items) coalesce into a single HTTP
  // request instead of firing one per change.
  _feeTimer: null,
  scheduleFeeUpdate() {
    if (this._feeTimer) clearTimeout(this._feeTimer);
    this._feeTimer = setTimeout(() => {
      this._feeTimer = null;
      this.updatePackagingFee();
    }, 200);
  },

  _renderFee() {
    const row = document.getElementById('pos-packaging-row');
    const disp = document.getElementById('pos-packaging-display');
    const tot = document.getElementById('pos-total');
    if (row && disp) {
      row.style.display = this.packagingFee > 0 ? 'flex' : 'none';
      disp.textContent = formatPeso(this.packagingFee);
    }
    // _renderFee is the single authoritative writer of pos-total so the
    // displayed total always includes the packaging fee, even on takeout
    // orders.  render() no longer writes pos-total directly.
    if (tot) tot.textContent = formatPeso(this.getTotal());
  },

  // Header preview total + item-count badge, kept in sync with every render.
  // (previewTotal is passed in by render() so the total is computed once.)
  _renderHeader(previewTotal) {
    const preview = document.getElementById('pos-total-preview');
    const badge = document.getElementById('pos-item-count-badge');
    if (preview) preview.textContent = formatPeso(previewTotal !== undefined ? previewTotal : this.getTotal());
    if (badge) {
      const count = this.items.reduce((sum, i) => sum + i.qty, 0);
      badge.textContent = count > 0
        ? ` (${count} item${count !== 1 ? 's' : ''})`
        : '';
    }
  },

  render() {
    const container = document.getElementById('pos-order-items');
    const totalEl = document.getElementById('pos-total');
    if (!container) return;

    if (this.items.length === 0) {
      container.innerHTML = this._EMPTY_STATE_HTML;
    } else {
      container.innerHTML = this.items.map(item => `
        <div class="pos-order-item" style="display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--cream)">
          <div class="pos-item-name" style="flex:1;min-width:0">
            <div style="font-weight:600;font-size:0.85rem">${item.name}</div>
            ${item.size !== 'none' ? `<div style="font-size:0.75rem;color:#aaa">${item.size}</div>` : ''}
          </div>
          <div class="pos-item-qty" style="display:flex;align-items:center;gap:4px">
            <button onclick="POS.changeQty('${item.key}',-1)" class="qty-btn" aria-label="Decrease quantity">-</button>
            <span style="min-width:24px;text-align:center;font-weight:700">${item.qty}</span>
            <button onclick="POS.changeQty('${item.key}',1)" class="qty-btn" aria-label="Increase quantity">+</button>
          </div>
          <div class="pos-item-price" style="font-weight:700;color:var(--caramel);min-width:60px;text-align:right">${formatPeso(item.price * item.qty)}</div>
          <button onclick="POS.removeItem('${item.key}')" class="pos-item-remove" aria-label="Remove item" style="color:var(--red-accent);background:none;border:none;cursor:pointer;font-size:1rem">×</button>
        </div>
      `).join('');
    }

    const total = this.getTotal();
    // pos-total is written exclusively by _renderFee() (called via
    // scheduleFeeUpdate below) so there is no dual-write flicker on
    // takeout orders.  _renderHeader still needs the pre-fee subtotal
    // total for the header preview badge.
    this._renderHeader(total);
    this.scheduleFeeUpdate();
    // Mirror every cart change to the temporary draft (skip when empty).
    this.saveDraft();
    // Extension hook: pos.html assigns POS._afterRender to sync the mobile
    // drawer without monkey-patching render() itself.
    this._afterRender();
  },

  // No-op hook called at the end of every render().  Assign a function to
  // POS._afterRender in page-specific scripts to react to cart changes
  // without patching render() via closure reassignment.
  _afterRender() {},

  async submitOrder() {
    // In-flight guard: while a request is pending, further clicks/taps are
    // ignored -- the button is disabled, and any stray call falls through
    // here without firing a second request.
    if (this.submitting) return;
    if (this.items.length === 0) { showToast('No items in order', 'error'); return; }
    const customerName = document.getElementById('pos-customer-name')?.value || 'Walk-in Customer';
    const orderType = document.querySelector('[name=pos-order-type]:checked')?.value || 'dine_in';

    // Capture the idempotency token and mirror the exact submission to the
    // draft BEFORE the request goes out: if the page refreshes mid-flight, the
    // restore path can ask the server whether this token already placed the
    // order instead of restoring a completed one.
    const requestToken = this._getRequestToken();
    this.saveDraft();
    // This terminal is about to place this order: mark its token so the
    // realtime broadcast echo of OUR OWN order is ignored (no chime/toast for
    // an order we just placed). Keyed by token because the broadcast can
    // beat the HTTP response back to this page.
    if (typeof RealtimeConnection !== 'undefined' && RealtimeConnection.ignoreToken) {
      RealtimeConnection.ignoreToken(requestToken);
    }

    this.submitting = true;
    this._setSubmitting(true);
    // Every unexpected outcome keeps the order fully recoverable:
    //   - the DRAFT still holds the items + customer info (refresh-safe, and
    //     the server guards every submission regardless);
    //   - the TOKEN is only reset when the server EXPLICITLY rejected the
    //     submission (success:false), because that response proves the order
    //     was rolled back. On a network error, an HTTP error or an
    //     unparseable response the request may or may not have reached the
    //     server, so the token is kept and a retry replays it -- the server's
    //     idempotency check turns a would-be duplicate into the original
    //     order instead of creating a second one.
    try {
      let failure = null;   // { kind, message }
      const createUrl = (window.KDM_URLS && window.KDM_URLS.posCreate) || '/orders/pos/create/';
      let response;
      try {
        response = await fetch(createUrl, {
          method: 'POST',
          headers: { 'X-CSRFToken': CSRF_TOKEN, 'Content-Type': 'application/json' },
          body: JSON.stringify({
            customer_name: customerName,
            order_type: orderType,
            request_token: requestToken,
            items: this.items.map(i => ({ product_id: i.id, size: i.size, quantity: i.qty })),
          }),
        });
      } catch (err) {
        // The request never produced a response (connection dropped, server
        // unreachable). It may still have reached the server, so keep the
        // token -- a retry replays it safely.
        failure = { message: 'Network error — your order was kept. Check the connection and try again.' };
      }

      if (!failure) {
        // A session that expired mid-order is redirected to the login page by
        // Django; fetch follows the redirect, so the final URL is the login
        // page and the body is HTML. Detect that instead of reporting a
        // baffling parse error.
        if (response.url && response.url.indexOf('/accounts/login/') !== -1) {
          failure = {
            kind: 'session',
            message: 'Your session expired. Log in again — your order was kept.',
          };
        } else if (!response.ok) {
          // The request reached the server and it answered with an error.
          // Read a JSON error body when there is one; otherwise fall back to
          // a status-aware message.
          let serverMsg = null;
          try { serverMsg = (await response.json()).error || null; } catch (e) {}
          failure = {
            message: serverMsg || (response.status === 403
              ? 'Access denied — your session may have expired. Reload the page — your order was kept.'
              : `Server error (${response.status}) — your order was not placed. Try again.`),
          };
        } else {
          let data = null;
          try { data = await response.json(); }
          catch (e) {
            failure = { message: 'Unexpected server response — your order was kept. Try again.' };
          }
          if (!failure && data && data.success) {
            showToast(data.order_number ? `Order ${data.order_number} created!` : 'Order created!', 'success');
            // This terminal placed the order: the realtime broadcast will
            // echo it back, so suppress the new-order chime/toast for our own
            // order. (Other terminals and the dashboard still get it.)
            if (typeof RealtimeConnection !== 'undefined' && RealtimeConnection.ignoreOrder) {
              RealtimeConnection.ignoreOrder(data.order_id);
            }
            this.items = [];
            this._resetRequestToken();
            this.render();
            // Disarm the beforeunload navigation guard: this is a successful
            // checkout, not an accidental leave. The flag must be set before
            // clearDraft() so any micro-task racing toward the unload event
            // already sees the correct state.
            this._checkoutSuccess = true;
            // The order is placed: the temporary cart served its purpose.
            this.clearDraft();
            document.getElementById('pos-customer-name').value = '';
            return;
          }
          if (!failure) {
            // The server rejected the submission and rolled everything back,
            // so the next attempt is a fresh logical submission -- a new
            // token is safe (and correct). The items stay in the draft so
            // the cashier can fix and retry.
            this._resetRequestToken();
            this.saveDraft();
            showToast((data && data.error) || 'Order could not be created. Please try again.', 'error', 6000);
            return;
          }
        }
      }

      // Any failure that leaves the outcome uncertain (network, HTTP error,
      // session redirect, unparseable body): keep the token and the draft so
      // the exact same logical order can be retried safely.
      if (failure) {
        this.saveDraft();
        showToast(failure.message, failure.kind === 'session' ? 'warning' : 'error', 6000);
      }
    } finally {
      this.submitting = false;
      this._setSubmitting(false);
    }
  }
};

// Browsers throttle timers in hidden/backgrounded tabs, so a stock-update
// flush that is waiting out its short coalescing window would be starved
// while the POS tab is covered. The moment the cashier comes back to the
// tab, apply any buffered updates immediately instead of waiting for the
// throttled timer.
document.addEventListener('visibilitychange', () => {
  if (document.hidden || !window.POS || !POS._pendingStock) return;
  if (POS._stockFlushTimer !== null) {
    clearTimeout(POS._stockFlushTimer);
    POS._stockFlushTimer = null;
  }
  POS._flushStockUpdates();
});

// ── Initialize ──
document.addEventListener('DOMContentLoaded', () => {
  // Animate stat cards on scroll
  const observer = new IntersectionObserver((entries) => {
    entries.forEach(e => { if (e.isIntersecting) e.target.style.opacity = '1'; });
  }, { threshold: 0.1 });
  document.querySelectorAll('.stat-card').forEach(el => {
    el.style.opacity = '0';
    el.style.transition = 'opacity 0.5s ease, transform 0.5s ease';
    observer.observe(el);
  });
  setTimeout(() => {
    document.querySelectorAll('.stat-card').forEach(el => el.style.opacity = '1');
  }, 100);
});
