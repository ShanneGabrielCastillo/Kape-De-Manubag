/* =============================================================
   PAYMENT WAITING PAGE — Kape De Manubag
   Handles real-time updates from SSE + HTTP polling fallback.

   States:
     awaiting_payment  → show "Payment Required" / cashier banner
     is_paid=true but still awaiting_payment → "Payment Confirmed"
     order_accepted (status moves to pending/etc.) → show overlay → redirect

   The server is authoritative.  The page never relies only on
   client-side state — every SSE event triggers an API poll to
   get the full, authoritative server payload.
   ============================================================= */
(function () {
  const config = window.WAITING_CONFIG;
  if (!config) return;

  let isPaid         = config.initialIsPaid;
  let currentStatus  = config.initialStatus;
  let pollTimer      = null;
  let isPolling      = false;
  let sseSource      = null;
  let sseConnected   = false;
  let redirecting    = false;

  const POLL_FAST_MS = config.pollInterval || 5000;
  const POLL_SLOW_MS = 30000;

  // ── DOM refs ────────────────────────────────────────────────
  const heroEl         = document.getElementById('waiting-hero');
  const iconEl         = document.getElementById('waiting-icon');
  const titleEl        = document.getElementById('waiting-title');
  const subtitleEl     = document.getElementById('waiting-subtitle');
  const cashierBanner  = document.getElementById('cashier-banner');
  const payPillEl      = document.getElementById('pay-status-pill');
  const payDotEl       = document.getElementById('pay-dot');
  const payTextEl      = document.getElementById('pay-status-text');
  const payHintEl      = document.getElementById('pay-status-hint');
  const overlayEl      = document.getElementById('accepted-overlay');
  const trackerLinkEl  = document.getElementById('accepted-tracker-link');
  const hintEl         = document.getElementById('waiting-connection-hint');

  // ── Transition helpers ───────────────────────────────────────

  function setHint(text) {
    if (hintEl) hintEl.textContent = text;
  }

  function applyPaymentConfirmed() {
    if (isPaid) return; // already applied
    isPaid = true;

    // Hero: yellow → green
    if (heroEl) {
      heroEl.classList.remove('state-awaiting');
      heroEl.classList.add('state-paid');
    }
    if (iconEl)     iconEl.textContent    = '💳✅';
    if (titleEl)    titleEl.textContent   = 'Payment Confirmed';
    if (subtitleEl) subtitleEl.textContent = 'Your payment has been verified. Please wait while our staff confirms your order.';

    // Hide cashier instruction banner
    if (cashierBanner) cashierBanner.style.display = 'none';

    // Payment pill: yellow → green
    if (payPillEl) {
      payPillEl.classList.remove('pay-status-unpaid');
      payPillEl.classList.add('pay-status-paid');
    }
    if (payDotEl) {
      payDotEl.classList.remove('pay-dot-unpaid');
      payDotEl.classList.add('pay-dot-paid');
    }
    if (payTextEl)  payTextEl.textContent  = 'Payment Confirmed ✓';
    if (payHintEl)  payHintEl.textContent  = 'Waiting for staff to confirm your order…';
  }

  function applyOrderAccepted(trackerUrl) {
    if (redirecting) return;
    redirecting = true;

    // Show the "Order Received!" overlay, then redirect after a short
    // celebration moment so the customer sees the confirmation.
    applyPaymentConfirmed(); // ensure payment state is shown correctly first
    if (heroEl) {
      heroEl.classList.remove('state-awaiting', 'state-paid');
      heroEl.classList.add('state-accepted');
    }
    if (iconEl)     iconEl.textContent    = '🎉';
    if (titleEl)    titleEl.textContent   = 'Order Received!';
    if (subtitleEl) subtitleEl.textContent = 'Your order has been confirmed by our staff.';

    if (overlayEl) overlayEl.style.display = 'flex';
    if (trackerLinkEl && trackerUrl) trackerLinkEl.href = trackerUrl;

    stopAll();

    // Redirect to the order tracker after the customer has seen the overlay.
    setTimeout(function () {
      window.location.href = trackerUrl || config.trackerUrl;
    }, 4000);
  }

  function applyOrderCancelled() {
    stopAll();
    if (heroEl) {
      heroEl.classList.remove('state-awaiting', 'state-paid', 'state-accepted');
      heroEl.classList.add('state-cancelled');
    }
    if (iconEl)     iconEl.textContent    = '❌';
    if (titleEl)    titleEl.textContent   = 'Order Cancelled';
    if (subtitleEl) subtitleEl.textContent = 'This order has been cancelled. Please place a new order if you wish.';
    if (cashierBanner) cashierBanner.style.display = 'none';
    setHint('✓ This order is cancelled.');
  }

  // ── Apply full server payload (from polling API) ─────────────
  function applyData(data) {
    // Cancelled — terminal state
    if (data.is_cancelled) {
      applyOrderCancelled();
      return;
    }

    // Payment confirmed
    if (data.is_paid && !isPaid) {
      applyPaymentConfirmed();
    }

    // Order accepted (moved past awaiting_payment)
    if (data.order_accepted && !redirecting) {
      applyOrderAccepted(data.tracker_url || config.trackerUrl);
    }

    currentStatus = data.status;
  }

  // ── Stop all connections ─────────────────────────────────────
  function stopAll() {
    stopPolling();
    closeSse();
    if (!redirecting) setHint('✓ Order confirmed — redirecting…');
  }

  // ── Polling ──────────────────────────────────────────────────
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function startPolling(intervalMs) {
    stopPolling();
    pollTimer = setInterval(poll, intervalMs);
  }

  let pollFailures = 0;
  async function poll() {
    if (isPolling || redirecting) return;
    isPolling = true;
    try {
      const res = await fetch(config.apiUrl);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      pollFailures = 0;
      if (!sseConnected) setHint('🔄 Auto-refreshing every 5 seconds');
      applyData(data);
    } catch (err) {
      pollFailures++;
      if (pollFailures >= 3) {
        setHint('⚠️ Connection issue — retrying…');
      }
    } finally {
      isPolling = false;
    }
  }

  // ── SSE ───────────────────────────────────────────────────────
  function closeSse() {
    if (sseSource) { sseSource.close(); sseSource = null; }
    sseConnected = false;
  }

  function openSse() {
    if (!config.sseUrl || !window.EventSource) {
      setHint('🔄 Auto-refreshes every 5 seconds');
      return;
    }

    sseSource = new EventSource(config.sseUrl);

    sseSource.onopen = function () {
      sseConnected = true;
      startPolling(POLL_SLOW_MS); // slow safety-net poll while SSE is live
      setHint('🟢 Live — updates as they happen');
    };

    sseSource.onerror = function () {
      sseConnected = false;
      startPolling(POLL_FAST_MS);
      setHint('🔄 Reconnecting…');
    };

    // Payment confirmed by cashier (is_paid=true, still awaiting acceptance)
    sseSource.addEventListener('payment_confirmed', function (e) {
      try {
        const data = JSON.parse(e.data);
        if (data.order_number !== config.orderNumber) return;
        applyPaymentConfirmed();
        // Poll to get the full server state
        poll();
      } catch (err) { /* ignore parse errors */ }
    });

    // Order accepted by cashier (awaiting_payment → pending)
    sseSource.addEventListener('order_accepted', function (e) {
      try {
        const data = JSON.parse(e.data);
        if (data.order_number !== config.orderNumber) return;
        applyOrderAccepted(config.trackerUrl);
      } catch (err) { /* ignore parse errors */ }
    });

    // status_changed also covers the awaiting_payment → pending transition
    // (emitted by the post_save signal).  Use it as a fallback in case the
    // dedicated order_accepted event was missed.
    sseSource.addEventListener('status_changed', function (e) {
      try {
        const data = JSON.parse(e.data);
        if (data.order_number !== config.orderNumber) return;
        // If status moved past awaiting_payment, the order was accepted.
        if (data.new_status !== 'awaiting_payment' && data.new_status !== 'cancelled') {
          applyOrderAccepted(config.trackerUrl);
        } else if (data.new_status === 'cancelled') {
          applyOrderCancelled();
        } else {
          // Still awaiting_payment — just poll for full data (e.g. is_paid changed)
          poll();
        }
      } catch (err) { /* ignore parse errors */ }
    });

    sseSource.addEventListener('heartbeat', function () {
      // Server alive; nothing to do.
    });
  }

  // ── Bootstrap ─────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', function () {
    // If the order is already past awaiting_payment on page load (e.g. the
    // customer refreshed after the order was already accepted), redirect to
    // the tracker immediately.
    if (currentStatus !== 'awaiting_payment' && currentStatus !== 'cancelled') {
      applyOrderAccepted(config.trackerUrl);
      return;
    }
    if (currentStatus === 'cancelled') {
      applyOrderCancelled();
      return;
    }

    // Immediate poll for fresh data
    poll();
    startPolling(POLL_FAST_MS);
    openSse();
  });

  window.addEventListener('pagehide', closeSse);
  window.addEventListener('beforeunload', closeSse);

})();
