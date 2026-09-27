/**
 * Real-time event connection manager for staff pages.
 * Uses native browser EventSource API (Server-Sent Events).
 */
window.RealtimeConnection = (function () {
  let eventSource = null;
  const listeners = {};

  // Orders that this terminal itself created (placed from THIS page).
  // Broadcasts echo every order back to every connected client including the
  // originating terminal, so a cashier would otherwise get a chime + toast
  // about the order they just placed. Suppression is keyed two ways:
  //   - request token: known BEFORE the submission response arrives, so the
  //     echo of the in-flight order is ignored even when the broadcast beats
  //     the HTTP response back to this page (no race);
  //   - order id: known only after the response, kept for any order that was
  //     created without going through a token (defense in depth).
  const selfOrderIds = new Set();
  const selfRequestTokens = new Set();

  function connect() {
    if (eventSource) return;
    const streamUrl = (window.KDM_URLS && window.KDM_URLS.realtimeStream) || '/realtime/stream/';
    eventSource = new EventSource(streamUrl);
    // Testable handle for automated audits: reflects whether the SSE stream
    // is currently open (auto-reconnect flips it back to true on onopen).
    window.__kdmSSEConnected = false;
    eventSource.onopen = () => {
      window.__kdmSSEConnected = true;
      // Notify any listeners that the connection (re-)opened.  Used by the
      // topbar badge to re-sync the count after a reconnect gap.
      dispatch('_connected', {});
    };
    eventSource.onerror = () => {
      window.__kdmSSEConnected = false;
      console.warn('Realtime connection lost — reconnecting...');
    };

    eventSource.addEventListener('new_order', (e) => {
      dispatch('new_order', JSON.parse(e.data));
    });
    eventSource.addEventListener('status_changed', (e) => {
      dispatch('status_changed', JSON.parse(e.data));
    });
    eventSource.addEventListener('inventory_low', (e) => {
      dispatch('inventory_low', JSON.parse(e.data));
    });
    eventSource.addEventListener('inventory_changed', (e) => {
      dispatch('inventory_changed', JSON.parse(e.data));
    });
    eventSource.addEventListener('gcash_submitted', (e) => {
      dispatch('gcash_submitted', JSON.parse(e.data));
    });
    eventSource.addEventListener('heartbeat', () => {
      // keep-alive — no action needed
    });

  }

  function on(eventType, callback) {
    if (!listeners[eventType]) listeners[eventType] = [];
    listeners[eventType].push(callback);
  }

  // Mark an order as created by this terminal so its broadcast echo is
  // ignored (no toast, no chime, no badge pulse for your own order).
  function ignoreOrder(orderId) {
    if (orderId === undefined || orderId === null) return;
    selfOrderIds.add(String(orderId));
  }

  function isSelfOrder(orderId) {
    return orderId !== undefined && orderId !== null && selfOrderIds.has(String(orderId));
  }

  // Pre-emptively mark the idempotency token about to be submitted, so the
  // broadcast echo of the order it creates is ignored from the start.
  function ignoreToken(token) {
    if (!token) return;
    selfRequestTokens.add(String(token));
  }

  function isSelfToken(token) {
    return !!token && selfRequestTokens.has(String(token));
  }

  function dispatch(eventType, data) {
    (listeners[eventType] || []).forEach((cb) => cb(data));
  }

  function disconnect() {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
  }

  return { connect, on, disconnect, ignoreOrder, isSelfOrder, ignoreToken, isSelfToken };
})();

document.addEventListener('DOMContentLoaded', () => {
  // Open the SSE stream when:
  //  (a) the page explicitly opts in via data-realtime="true" on <body>
  //      (Dashboard, Order Management, POS — existing behaviour), OR
  //  (b) either notification badge element is present — mobile topbar or
  //      desktop sidebar — meaning this is a staff page that needs live
  //      awaiting-payment updates.
  // Both conditions use the same single stream — no second connection is opened.
  const hasBadge = !!(
    document.getElementById('topbar-awaiting-badge') ||
    document.getElementById('sidebar-awaiting-badge')
  );
  if (document.body.dataset.realtime === 'true' || hasBadge) {
    RealtimeConnection.connect();
  }
});

window.addEventListener('beforeunload', () => {
  RealtimeConnection.disconnect();
});

// ── Notification helpers ──────────────────────────────────────────────────────

function playNotificationSound() {
  const audio = document.getElementById('new-order-sound');
  if (audio) {
    audio.play().catch(() => {});
  }
}

function showNewOrderNotification(order) {
  // Never notify a terminal about an order it just placed itself (matched by
  // request token pre-submission, or by order id after the response).
  if (order && (RealtimeConnection.isSelfToken(order.request_token) || RealtimeConnection.isSelfOrder(order.order_id))) return;
  if (typeof showToast === 'function') {
    showToast(
      `🔔 New Order #${order.queue_number} — ${order.customer_name}`,
      'success',
      6000
    );
  }
  playNotificationSound();
  // Pulse the awaiting-payment badge in the sidebar if present
  const badge = document.querySelector('.sidebar-link .badge-pending, .sidebar-link .badge-awaiting-payment');
  if (badge) {
    badge.classList.add('badge-pulse');
    setTimeout(() => badge.classList.remove('badge-pulse'), 1000);
  }
}

RealtimeConnection.on('new_order', showNewOrderNotification);

// ── Order Management notification badge — mobile topbar + desktop sidebar ────
//
// A single module (TopbarOrderBadge) drives both the mobile top-bar badge and
// the desktop sidebar badge from one shared state.  There is no second fetch,
// no second SSE connection, and no second event listener.
//
// STATE-BASED APPROACH — prevents duplicate counting from reconnections,
// replayed events, or double-clicks.
//
// A client-side Set (_awaitingIds) tracks which order IDs are currently in
// the awaiting_payment state as known to this browser tab.  Every badge
// mutation goes through the Set, making the operations idempotent:
//
//   new_order        → always enters awaiting_payment (default status).
//                      Add to Set and increment only if not already present.
//
//   status_changed   → new_status == 'awaiting_payment'   → add + increment (if new)
//                      new_status != 'awaiting_payment'   → remove + decrement (if present)
//
//   gcash_submitted  → order status does NOT change; badge is unaffected.
//   payment_confirmed / order_accepted → no direct badge action; status_changed
//                      from the post_save signal carries the transition.
//
// On page load and on every SSE reconnect (_connected event) the badge
// re-fetches the authoritative DB count so any events missed during a
// connection gap are recovered.  The Set is cleared on re-fetch so it stays
// in sync with the fresh count.

window.TopbarOrderBadge = (function () {

  // IDs (strings) of orders currently in awaiting_payment, tracked locally.
  const _awaitingIds = new Set();

  // Current displayed count.  Seeded by _fetchCount().
  let _count = 0;

  // DOM references resolved once on init().
  // Mobile top-bar badge + its anchor link.
  let _badge        = null;
  let _link         = null;
  // Desktop sidebar badge + its anchor link.
  let _sidebarBadge = null;
  let _sidebarLink  = null;

  // ── helpers ───────────────────────────────────────────────────────────────

  function _renderOne(badgeEl, linkEl, count) {
    if (!badgeEl) return;
    if (count > 0) {
      badgeEl.textContent   = count > 99 ? '99+' : String(count);
      badgeEl.style.display = 'flex';
    } else {
      badgeEl.style.display = 'none';
    }
    if (linkEl) {
      if (count === 0) {
        linkEl.setAttribute('aria-label', 'Order Management — no orders awaiting payment');
      } else {
        const display = count > 99 ? '99+' : count;
        linkEl.setAttribute(
          'aria-label',
          `Order Management — ${display} order${count === 1 ? '' : 's'} awaiting payment`
        );
      }
    }
  }

  function _render() {
    _renderOne(_badge,        _link,        _count);
    _renderOne(_sidebarBadge, _sidebarLink, _count);
  }

  function _pulse() {
    [_badge, _sidebarBadge].forEach((el) => {
      if (!el || el.style.display === 'none') return;
      el.classList.add('badge-pulse');
      setTimeout(() => el.classList.remove('badge-pulse'), 1000);
    });
  }

  // ── initial / reconnect fetch ─────────────────────────────────────────────
  // Calls the staff-only /orders/api/awaiting-count/ endpoint for the
  // authoritative DB count.  Resets the local Set so stale entries from
  // before a reconnect do not persist.

  function _fetchCount() {
    const url = window.KDM_URLS && window.KDM_URLS.awaitingCount;
    if (!url) return;
    fetch(url, { credentials: 'same-origin' })
      .then((r) => { if (r.ok) return r.json(); })
      .then((data) => {
        if (!data || typeof data.count !== 'number') return;
        // Reset local tracking — the server is the source of truth.
        _awaitingIds.clear();
        _count = data.count;
        _render();
      })
      .catch(() => {
        // Silently ignore network errors; badge will self-correct on next
        // SSE event or page reload.
      });
  }

  // ── SSE event handlers ────────────────────────────────────────────────────

  function _onConnected() {
    // SSE stream (re-)opened: re-fetch so we recover any orders that arrived
    // during a connection gap.  Also clears _awaitingIds so the Set stays
    // consistent with the fresh count.
    _fetchCount();
  }

  function _onNewOrder(order) {
    // Every new order starts at awaiting_payment (the model default).
    // The Set guards against duplicate events for the same order_id.
    if (!order || !order.order_id) return;
    const id = String(order.order_id);
    if (_awaitingIds.has(id)) return;  // already counted — idempotent
    _awaitingIds.add(id);
    _count += 1;
    _render();
    _pulse();
  }

  function _onStatusChanged(data) {
    // Payload (from apps/realtime/signals.py post_save):
    //   order_id, order_number, queue_number, new_status, new_status_display, is_paid
    if (!data || !data.order_id) return;
    const id        = String(data.order_id);
    const newStatus = data.new_status;

    if (newStatus === 'awaiting_payment') {
      // Order entered awaiting_payment (edge case: manual rollback).
      if (!_awaitingIds.has(id)) {
        _awaitingIds.add(id);
        _count += 1;
        _render();
      }
    } else {
      // Order left awaiting_payment (preparing / ready / completed / cancelled).
      if (_awaitingIds.has(id)) {
        _awaitingIds.delete(id);
        _count = Math.max(0, _count - 1);
        _render();
      }
    }
  }

  // ── init ──────────────────────────────────────────────────────────────────

  function init() {
    // Mobile top-bar elements
    _badge = document.getElementById('topbar-awaiting-badge');
    _link  = document.getElementById('topbar-orders-link');

    // Desktop sidebar elements
    _sidebarBadge = document.getElementById('sidebar-awaiting-badge');
    _sidebarLink  = document.getElementById('sidebar-orders-link');

    // Bail out if neither badge element is present (non-staff page).
    if (!_badge && !_sidebarBadge) return;

    // Fetch the authoritative count immediately on page load.
    _fetchCount();

    // Wire up SSE event handlers.  Registered once — both badges update
    // from the same single set of listeners.
    RealtimeConnection.on('_connected',     _onConnected);
    RealtimeConnection.on('new_order',      _onNewOrder);
    RealtimeConnection.on('status_changed', _onStatusChanged);
  }

  return { init };
})();

document.addEventListener('DOMContentLoaded', () => {
  TopbarOrderBadge.init();
});
