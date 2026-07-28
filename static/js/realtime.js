/**
 * Real-time event connection manager for staff pages.
 * Uses native browser EventSource API (Server-Sent Events).
 */
window.RealtimeConnection = (function () {
  let eventSource = null;
  const listeners = {};

  function connect() {
    if (eventSource) return;
    eventSource = new EventSource('/realtime/stream/');

    eventSource.addEventListener('new_order', (e) => {
      dispatch('new_order', JSON.parse(e.data));
    });
    eventSource.addEventListener('status_changed', (e) => {
      dispatch('status_changed', JSON.parse(e.data));
    });
    eventSource.addEventListener('inventory_low', (e) => {
      dispatch('inventory_low', JSON.parse(e.data));
    });
    eventSource.addEventListener('heartbeat', () => {
      // keep-alive — no action needed
    });

    eventSource.onerror = function () {
      console.warn('Realtime connection lost — reconnecting...');
    };
  }

  function on(eventType, callback) {
    if (!listeners[eventType]) listeners[eventType] = [];
    listeners[eventType].push(callback);
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

  return { connect, on, disconnect };
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
