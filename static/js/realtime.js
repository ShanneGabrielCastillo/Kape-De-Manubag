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
    eventSource.onopen = () => { window.__kdmSSEConnected = true; };
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
  RealtimeConnection.connect();
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
  // Pulse the pending badge in the sidebar if present
  const badge = document.querySelector('.sidebar-link .badge-pending');
  if (badge) {
    badge.classList.add('badge-pulse');
    setTimeout(() => badge.classList.remove('badge-pulse'), 1000);
  }
}

RealtimeConnection.on('new_order', showNewOrderNotification);
