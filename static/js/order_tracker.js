/* =====================================================
   ORDER TRACKER — Kape De Manubag
   Primary:  Server-Sent Events (SSE) push updates.
   Fallback: HTTP polling every 5 s when SSE is not
             available or while SSE is reconnecting.
   ===================================================== */

(function () {
  const config = window.TRACKER_CONFIG;
  if (!config) return;

  const STATUS_ORDER = ['pending', 'preparing', 'ready'];

  let previousStatus  = config.initialStatus;
  let pollTimer       = null;
  let isPolling       = false;
  let sseSource       = null;
  let sseConnected    = false;
  // Backing-off poll interval while SSE is healthy (slower fallback),
  // or fast (5 s) when SSE is unavailable.
  const POLL_FAST_MS  = config.pollInterval || 5000;  // SSE unavailable
  const POLL_SLOW_MS  = 30000;                        // SSE connected (safety net)
  let currentPollMs   = POLL_FAST_MS;

  // ── DOM refs ──────────────────────────────────────────────────────────────
  const heroEl        = document.getElementById('tracker-hero');
  const emojiEl       = document.getElementById('tracker-status-emoji');
  const labelEl       = document.getElementById('tracker-status-label');
  const posNumEl      = document.getElementById('queue-position-display');
  const posSubEl      = document.getElementById('queue-position-sub');
  const waitEl        = document.getElementById('wait-display');
  const overlayEl     = document.getElementById('ready-overlay');
  const soundEl       = document.getElementById('ready-sound');
  const posCardEl     = document.getElementById('queue-position-card');
  const hintEl        = document.getElementById('tracker-connection-hint');

  // ── Hero class helper ────────────────────────────────────────────────────
  function updateHeroClass(status) {
    const classes = ['hero-pending','hero-preparing','hero-ready','hero-completed','hero-cancelled'];
    classes.forEach(c => heroEl && heroEl.classList.remove(c));
    if (heroEl) heroEl.classList.add('hero-' + status);
  }

  // ── Progress steps ───────────────────────────────────────────────────────
  function updateProgressSteps(status) {
    const steps = document.querySelectorAll('.tracker-step');
    const idx   = STATUS_ORDER.indexOf(status);
    steps.forEach((step, i) => {
      step.classList.remove('step-done', 'step-active');
      if (i < idx)        step.classList.add('step-done');
      else if (i === idx) step.classList.add('step-active');
    });
    if (status === 'completed' || status === 'ready') {
      steps[2] && steps[2].classList.add('step-active');
    }
  }

  // ── Ready transition (fires only once) ───────────────────────────────────
  function handleReadyTransition() {
    if (overlayEl) overlayEl.style.display = 'flex';
    if (soundEl) soundEl.play().catch(() => {});
    if (heroEl) {
      heroEl.classList.add('hero-ready-flash');
      setTimeout(() => heroEl.classList.remove('hero-ready-flash'), 3500);
    }
  }

  // ── Wait-time text ────────────────────────────────────────────────────────
  function formatWait(status, minutes) {
    if (status === 'ready')     return '✅ Ready now!';
    if (status === 'completed') return '🎉 Completed';
    if (status === 'cancelled') return '❌ Cancelled';
    if (minutes === null || minutes === undefined) return '—';
    if (minutes <= 1) return 'Less than 1 min';
    return `~${minutes} min`;
  }

  // ── Queue-position sub-text ───────────────────────────────────────────────
  function formatPositionSub(pos, status) {
    if (status === 'ready')     return '✅ Ready for pickup!';
    if (status === 'completed') return '🎉 Order completed';
    if (status === 'cancelled') return '❌ Order cancelled';
    if (pos === 0) return '—';
    if (pos === 1) return "You're next!";
    return `${pos - 1} order${pos - 1 !== 1 ? 's' : ''} ahead of you`;
  }

  // ── Connection-hint text ──────────────────────────────────────────────────
  function setHint(text) {
    if (hintEl) hintEl.textContent = text;
  }

  // ── Apply a full data payload from the poll API ───────────────────────────
  function applyData(data) {
    if (emojiEl) emojiEl.textContent = data.status_emoji;
    if (labelEl) labelEl.textContent = data.status_display;
    updateHeroClass(data.status);
    updateProgressSteps(data.status);
    if (posNumEl) posNumEl.textContent = data.queue_position || '—';
    if (posSubEl) posSubEl.textContent = formatPositionSub(data.queue_position, data.status);
    if (posCardEl) posCardEl.style.display = data.is_final ? 'none' : '';
    if (waitEl) waitEl.textContent = formatWait(data.status, data.estimated_minutes);

    if (data.status === 'ready' && previousStatus !== 'ready') {
      handleReadyTransition();
    }
    previousStatus = data.status;

    if (data.is_final) stopAll();
  }

  // ── Stop everything once the order reaches a terminal state ──────────────
  function stopAll() {
    stopPolling();
    closeSse();
    setHint('✓ Live updates stopped — order is complete.');
  }

  // ── Polling ───────────────────────────────────────────────────────────────
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function startPolling(intervalMs) {
    stopPolling();
    currentPollMs = intervalMs;
    pollTimer = setInterval(poll, intervalMs);
  }

  // Count consecutive poll failures so we can show a hint after a sustained
  // connection problem rather than leaving the customer with a frozen display.
  let consecutivePollFailures = 0;
  const POLL_FAILURE_HINT_THRESHOLD = 3; // show hint after 3 consecutive failures

  async function poll() {
    if (isPolling) return;
    isPolling = true;
    try {
      const res  = await fetch(config.apiUrl);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      consecutivePollFailures = 0;
      // Clear any connection-problem hint now that we have a good response.
      if (!sseConnected) {
        setHint('🔄 Reconnecting… auto-refreshing every 5 seconds');
      }
      applyData(data);
    } catch (err) {
      consecutivePollFailures++;
      console.warn('[Tracker] Poll error:', err);
      // After a few consecutive failures, tell the customer so they know
      // the display may be stale — don't leave them in the dark.
      if (consecutivePollFailures >= POLL_FAILURE_HINT_THRESHOLD) {
        setHint('⚠️ Connection issue — retrying… your order status may be delayed');
      }
    } finally {
      isPolling = false;
    }
  }

  // ── SSE ───────────────────────────────────────────────────────────────────
  function closeSse() {
    if (sseSource) { sseSource.close(); sseSource = null; }
    sseConnected = false;
  }

  function openSse() {
    if (!config.sseUrl || !window.EventSource) {
      // Browser does not support SSE — stay on fast polling.
      setHint('🔄 Auto-refreshes every 5 seconds');
      return;
    }

    sseSource = new EventSource(config.sseUrl);

    sseSource.onopen = function () {
      sseConnected = true;
      // SSE is live — slow the polling down to a safety-net rate.
      startPolling(POLL_SLOW_MS);
      setHint('🟢 Live — updates as they happen');
    };

    sseSource.onerror = function () {
      // EventSource auto-reconnects; switch back to fast polling while it
      // does so the tracker does not go stale during the reconnection window.
      sseConnected = false;
      startPolling(POLL_FAST_MS);
      setHint('🔄 Reconnecting… auto-refreshing every 5 seconds');
    };

    sseSource.addEventListener('status_changed', function (e) {
      try {
        const data = JSON.parse(e.data);
        // The server already filters by order_number, but double-check
        // client-side to guard against any proxy that might fan out events.
        if (data.order_number !== config.orderNumber) return;
        // De-duplicate: ignore if we already reflect this status.
        if (data.new_status === previousStatus) return;

        // Fetch the full API payload (queue position, estimated wait, etc.)
        // rather than applying a partial SSE payload — the SSE event only
        // carries the status change itself for minimal payload size.
        poll();
      } catch (err) {
        console.warn('[Tracker] SSE parse error:', err);
      }
    });

    sseSource.addEventListener('heartbeat', function () {
      // Server is alive; nothing to do.
    });
  }

  // ── Bootstrap ─────────────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', function () {
    const isFinal = config.initialStatus === 'completed' ||
                    config.initialStatus === 'cancelled';

    updateHeroClass(config.initialStatus);
    updateProgressSteps(config.initialStatus);

    if (isFinal) {
      // Order is already in a terminal state; show it statically.
      setHint('✓ This order is complete.');
      return;
    }

    // Run one immediate poll to get fresh data (queue position, wait time).
    poll();

    // Open SSE for push updates; start fast polling as the fallback.
    startPolling(POLL_FAST_MS);
    openSse();
  });

  // Cleanly close the SSE connection when the customer navigates away.
  window.addEventListener('pagehide', closeSse);
  window.addEventListener('beforeunload', closeSse);

})();
