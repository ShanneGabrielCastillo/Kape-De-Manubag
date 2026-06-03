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
    document.body.style.overflow = isOpen ? 'hidden' : '';
    // Update aria-expanded
    sidebarToggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
  });

  // Close on overlay click
  if (sidebarOverlay) {
    sidebarOverlay.addEventListener('click', () => {
      sidebar.classList.remove('open');
      sidebarOverlay.classList.remove('open');
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
      } else {
        showToast('Failed to add to cart', 'error');
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
      const response = await fetch(`/orders/cart/update/${itemId}/`, {
        method: 'POST',
        headers: { 'X-CSRFToken': CSRF_TOKEN, 'Content-Type': 'application/x-www-form-urlencoded' },
        body: `quantity=${qty}`,
      });
      const data = await response.json();
      if (data.success) {
        if (qty < 1) {
          this.closest('.cart-item-row').remove();
        } else {
          display.textContent = qty;
          const priceEl = this.closest('.cart-item-row').querySelector('.cart-item-price');
          if (priceEl) priceEl.textContent = `₱${data.item_subtotal.toFixed(2)}`;
        }
        // Update totals
        const totalEl = document.querySelector('.cart-grand-total');
        if (totalEl) totalEl.textContent = `₱${data.cart_total.toFixed(2)}`;
        const cartFabBadge = document.querySelector('.cart-fab .badge-count');
        if (cartFabBadge) cartFabBadge.textContent = data.cart_count;
      }
    } catch (err) {
      showToast('Error updating cart', 'error');
    }
  });
});

// ── Remove from Cart ──
document.querySelectorAll('.cart-remove').forEach(btn => {
  btn.addEventListener('click', async function() {
    if (!confirm('Remove this item?')) return;
    const itemId = this.dataset.itemId;
    try {
      const response = await fetch(`/orders/cart/remove/${itemId}/`, {
        method: 'POST',
        headers: { 'X-CSRFToken': CSRF_TOKEN },
      });
      const data = await response.json();
      if (data.success) {
        this.closest('.cart-item-row').remove();
        const totalEl = document.querySelector('.cart-grand-total');
        if (totalEl) totalEl.textContent = `₱${data.cart_total.toFixed(2)}`;
        const cartFabBadge = document.querySelector('.cart-fab .badge-count');
        if (cartFabBadge) cartFabBadge.textContent = data.cart_count;
        showToast('Item removed', 'info');
      }
    } catch (err) {
      showToast('Error removing item', 'error');
    }
  });
});

// ── Order Status Update ──
document.querySelectorAll('.status-update-form').forEach(form => {
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const orderId = form.dataset.orderId;
    const status = form.querySelector('[name=status]').value;

    try {
      const response = await fetch(`/orders/manage/${orderId}/status/`, {
        method: 'POST',
        headers: { 'X-CSRFToken': CSRF_TOKEN, 'Content-Type': 'application/x-www-form-urlencoded' },
        body: `status=${status}`,
      });
      const data = await response.json();
      if (data.success) {
        showToast(`Status updated to ${data.status}`, 'success');
        setTimeout(() => location.reload(), 1000);
      }
    } catch (err) {
      showToast('Error updating status', 'error');
    }
  });
});

// ── Product Toggle (Available/Unavailable) ──
document.querySelectorAll('.toggle-product-btn').forEach(btn => {
  btn.addEventListener('click', async function() {
    const productId = this.dataset.productId;
    try {
      const response = await fetch(`/manage/products/${productId}/toggle/`, {
        method: 'POST',
        headers: { 'X-CSRFToken': CSRF_TOKEN },
      });
      const data = await response.json();
      if (data.success) {
        const label = data.is_available ? 'Available' : 'Unavailable';
        this.textContent = label;
        this.className = `badge ${data.is_available ? 'badge-ready' : 'badge-cancelled'}`;
        showToast(`Product marked as ${label}`, 'success');
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

// ── Category Filter on Menu Page ──
const catBtns = document.querySelectorAll('.menu-cat-btn');
catBtns.forEach(btn => {
  btn.addEventListener('click', function() {
    catBtns.forEach(b => b.classList.remove('active'));
    this.classList.add('active');
    const cat = this.dataset.cat;
    document.querySelectorAll('.menu-section').forEach(section => {
      if (cat === 'all' || section.dataset.cat === cat) {
        section.style.display = '';
      } else {
        section.style.display = 'none';
      }
    });
  });
});

// ── Search Filter ──
const searchInput = document.querySelector('#menu-search');
if (searchInput) {
  searchInput.addEventListener('input', function() {
    const q = this.value.toLowerCase().trim();
    document.querySelectorAll('.product-card').forEach(card => {
      const name = card.querySelector('.product-card-name')?.textContent.toLowerCase() || '';
      card.closest('.product-col').style.display = (!q || name.includes(q)) ? '' : 'none';
    });
    // Hide empty sections
    document.querySelectorAll('.menu-section').forEach(section => {
      const visible = section.querySelectorAll('.product-col:not([style*="none"])').length;
      section.style.display = visible > 0 ? '' : 'none';
    });
  });
}

// ── Payment Modal ──
window.openPaymentModal = function(orderId, orderTotal) {
  document.getElementById('payment-order-id').value = orderId;
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

    try {
      const response = await fetch(`/orders/manage/${orderId}/payment/`, {
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

    fetch(`/api/product/${productId}/price/?size=${size}`)
      .then(r => r.json())
      .then(d => { priceEl.textContent = d.formatted; });
  });
});

// ── Sales Chart (Dashboard) ──
window.initSalesChart = function(labels, data) {
  const canvas = document.getElementById('sales-chart');
  if (!canvas || typeof Chart === 'undefined') return;

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
          ticks: { callback: v => `₱${v}` },
        },
        x: { grid: { display: false } },
      },
    },
  });
};

// ── POS System ──
window.POS = {
  items: [],
  packagingFee: 0,

  addItem(id, name, price, size = 'none') {
    const key = `${id}-${size}`;
    const existing = this.items.find(i => i.key === key);
    if (existing) {
      existing.qty++;
    } else {
      this.items.push({ key, id, name, price: parseFloat(price), size, qty: 1 });
    }
    this.render();
    showToast(`${name} added`, 'success', 1500);
  },

  removeItem(key) {
    this.items = this.items.filter(i => i.key !== key);
    this.render();
  },

  changeQty(key, delta) {
    const item = this.items.find(i => i.key === key);
    if (item) {
      item.qty += delta;
      if (item.qty <= 0) this.items = this.items.filter(i => i.key !== key);
    }
    this.render();
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
      const r = await fetch('/orders/api/packaging-fee/?' + params.toString());
      const d = await r.json();
      this.packagingFee = d.packaging_fee || 0;
    } catch(e) {
      this.packagingFee = 0;
    }
    this._renderFee();
  },

  _renderFee() {
    const row = document.getElementById('pos-packaging-row');
    const disp = document.getElementById('pos-packaging-display');
    const tot = document.getElementById('pos-total');
    if (row && disp) {
      row.style.display = this.packagingFee > 0 ? 'flex' : 'none';
      disp.textContent = '₱' + this.packagingFee.toFixed(2);
    }
    if (tot) tot.textContent = '₱' + this.getTotal().toFixed(2);
  },

  render() {
    const container = document.getElementById('pos-order-items');
    const totalEl = document.getElementById('pos-total');
    if (!container) return;

    if (this.items.length === 0) {
      container.innerHTML = '<div class="empty-state" style="padding:20px"><div class="empty-icon">🛒</div><p>No items yet</p></div>';
    } else {
      container.innerHTML = this.items.map(item => `
        <div class="pos-order-item" style="display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--cream)">
          <div style="flex:1">
            <div style="font-weight:600;font-size:0.85rem">${item.name}</div>
            ${item.size !== 'none' ? `<div style="font-size:0.75rem;color:#aaa">${item.size}</div>` : ''}
          </div>
          <div style="display:flex;align-items:center;gap:4px">
            <button onclick="POS.changeQty('${item.key}',-1)" class="qty-btn" style="width:24px;height:24px;font-size:0.8rem">-</button>
            <span style="min-width:24px;text-align:center;font-weight:700">${item.qty}</span>
            <button onclick="POS.changeQty('${item.key}',1)" class="qty-btn" style="width:24px;height:24px;font-size:0.8rem">+</button>
          </div>
          <div style="font-weight:700;color:var(--caramel);min-width:60px;text-align:right">₱${(item.price*item.qty).toFixed(2)}</div>
          <button onclick="POS.removeItem('${item.key}')" style="color:var(--red-accent);background:none;border:none;cursor:pointer;font-size:1rem">×</button>
        </div>
      `).join('');
    }

    if (totalEl) totalEl.textContent = `₱${this.getTotal().toFixed(2)}`;
    this.updatePackagingFee();
  },

  async submitOrder() {
    if (this.items.length === 0) { showToast('No items in order', 'error'); return; }
    const customerName = document.getElementById('pos-customer-name')?.value || 'Walk-in Customer';
    const tableNumber = document.getElementById('pos-table')?.value || '';
    const orderType = document.querySelector('[name=pos-order-type]:checked')?.value || 'dine_in';

    try {
      const response = await fetch('/orders/pos/create/', {
        method: 'POST',
        headers: { 'X-CSRFToken': CSRF_TOKEN, 'Content-Type': 'application/json' },
        body: JSON.stringify({
          customer_name: customerName,
          table_number: tableNumber,
          order_type: orderType,
          items: this.items.map(i => ({ product_id: i.id, size: i.size, quantity: i.qty })),
        }),
      });
      const data = await response.json();
      if (data.success) {
        showToast(`Order ${data.order_number} created!`, 'success');
        this.items = [];
        this.render();
        document.getElementById('pos-customer-name').value = '';
        document.getElementById('pos-table').value = '';
      } else {
        showToast(data.error || 'Error creating order', 'error', 6000);
      }
    } catch (err) {
      showToast('Network error', 'error');
    }
  }
};

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
