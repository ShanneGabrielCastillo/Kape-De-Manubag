/* =============================================================
   PAYMENT WAITING PAGE — Kape De Manubag
   Handles real-time updates from SSE + HTTP polling fallback.

   States:
     PENDING + is_paid=false  → "Payment Required" / cashier banner visible
     PENDING + is_paid=true   → "Payment Confirmed" / waiting for acceptance
     Order accepted (status → preparing/ready/completed) → show overlay → redirect

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

    // GCash rejected — show re-submission form
    if (data.gcash_status === 'rejected' && currentStatus === 'awaiting_payment' && !data.is_paid) {
      applyGcashRejected(data.gcash_notes || '');
    }

    // Payment confirmed (is_paid became true, order still awaiting_payment)
    if (data.is_paid && !isPaid) {
      applyPaymentConfirmed();
    }

    // Order accepted — cashier moved order past awaiting_payment (to preparing or beyond)
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

    // Payment confirmed by cashier (is_paid=true, order still PENDING)
    sseSource.addEventListener('payment_confirmed', function (e) {
      try {
        const data = JSON.parse(e.data);
        if (data.order_number !== config.orderNumber) return;
        applyPaymentConfirmed();
        // Poll to get the full server state
        poll();
      } catch (err) { /* ignore parse errors */ }
    });

    // Order accepted by cashier (PENDING → PREPARING)
    sseSource.addEventListener('order_accepted', function (e) {
      try {
        const data = JSON.parse(e.data);
        if (data.order_number !== config.orderNumber) return;
        applyOrderAccepted(config.trackerUrl);
      } catch (err) { /* ignore parse errors */ }
    });

    // status_changed covers the awaiting_payment → preparing transition
    // emitted by the post_save signal. Use it as a fallback in case the
    // dedicated order_accepted event was missed.
    sseSource.addEventListener('status_changed', function (e) {
      try {
        const data = JSON.parse(e.data);
        if (data.order_number !== config.orderNumber) return;
        const newStatus = data.new_status;
        if (newStatus === 'cancelled') {
          applyOrderCancelled();
        } else if (newStatus === 'awaiting_payment') {
          // Still awaiting payment — poll for full data (is_paid may have changed)
          poll();
        } else {
          // Status moved past awaiting_payment → order accepted, go to tracker
          applyOrderAccepted(config.trackerUrl);
        }
      } catch (err) { /* ignore parse errors */ }
    });

    sseSource.addEventListener('heartbeat', function () {
      // Server alive; nothing to do.
    });

    // GCash payment rejected by staff — customer can re-submit.
    sseSource.addEventListener('gcash_rejected', function (e) {
      try {
        const data = JSON.parse(e.data);
        if (data.order_number !== config.orderNumber) return;
        applyGcashRejected(data.rejection_note || '');
        poll(); // refresh full state
      } catch (err) { /* ignore */ }
    });
  }

  // ── GCash rejected ────────────────────────────────────────────
  function applyGcashRejected(note) {
    const heroEl    = document.getElementById('waiting-hero');
    const iconEl    = document.getElementById('waiting-icon');
    const titleEl   = document.getElementById('waiting-title');
    const subEl     = document.getElementById('waiting-subtitle');

    if (heroEl) { heroEl.className = 'waiting-hero state-rejected'; }
    if (iconEl)  iconEl.textContent  = '❌';
    if (titleEl) titleEl.textContent = 'Payment Not Verified';
    if (subEl)   subEl.textContent   = 'Your payment could not be verified. Please check the details and re-submit.';

    // Hide the pending card and show the form again if present
    const pendingCard = document.getElementById('gcash-pending-card');
    if (pendingCard) pendingCard.style.display = 'none';
    const formCard = document.getElementById('gcash-form-card');
    if (formCard) {
      formCard.style.display = 'block';
      if (note) {
        let rejNote = formCard.querySelector('.gcash-reject-runtime-note');
        if (!rejNote) {
          rejNote = document.createElement('div');
          rejNote.className = 'gcash-reject-runtime-note';
          rejNote.style.cssText = 'background:#fee2e2;border:1px solid #fca5a5;border-radius:8px;padding:10px 14px;margin-bottom:12px;font-size:0.85rem;color:#991b1b';
          formCard.insertBefore(rejNote, formCard.firstChild);
        }
        rejNote.textContent = '❌ Rejected: ' + note;
      }
      // Re-enable submit button
      const btn = document.getElementById('gcash-submit-btn');
      if (btn) { btn.disabled = false; btn.textContent = '📨 Submit Payment for Verification'; }
    }
  }

  // ── Bootstrap ─────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', function () {
    // If the order is no longer awaiting_payment on page load
    // (customer refreshed after payment was already confirmed), go
    // straight to the tracker.
    if (currentStatus === 'cancelled') {
      applyOrderCancelled();
      return;
    }
    if (currentStatus !== 'awaiting_payment') {
      applyOrderAccepted(config.trackerUrl);
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
