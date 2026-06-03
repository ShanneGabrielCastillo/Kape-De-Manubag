/* =====================================================
   ORDER TRACKER — Kape De Manubag
   Polls the API every 5 seconds to update order status.
   ===================================================== */

(function () {
  const config = window.TRACKER_CONFIG;
  if (!config) return;

  const STATUS_ORDER = ['pending', 'preparing', 'ready'];

  let previousStatus = config.initialStatus;
  let pollTimer      = null;
  let isPolling      = false;

  // ── DOM refs ──
  const heroEl        = document.getElementById('tracker-hero');
  const emojiEl       = document.getElementById('tracker-status-emoji');
  const labelEl       = document.getElementById('tracker-status-label');
  const posNumEl      = document.getElementById('queue-position-display');
  const posSubEl      = document.getElementById('queue-position-sub');
  const waitEl        = document.getElementById('wait-display');
  const overlayEl     = document.getElementById('ready-overlay');
  const soundEl       = document.getElementById('ready-sound');
  const posCardEl     = document.getElementById('queue-position-card');

  // ── Hero class helper ──
  function updateHeroClass(status) {
    const classes = ['hero-pending','hero-preparing','hero-ready','hero-completed','hero-cancelled'];
    classes.forEach(c => heroEl.classList.remove(c));
    heroEl.classList.add('hero-' + status);
  }

  // ── Progress steps ──
  function updateProgressSteps(status) {
    const steps = document.querySelectorAll('.tracker-step');
    const idx   = STATUS_ORDER.indexOf(status);

    steps.forEach((step, i) => {
      step.classList.remove('step-done', 'step-active');
      if (i < idx)        step.classList.add('step-done');
      else if (i === idx) step.classList.add('step-active');
    });

    // "ready" and "completed" both light up the last step (index 2)
    if (status === 'completed' || status === 'ready') {
      steps[2] && steps[2].classList.add('step-active');
    }
  }

  // ── Ready transition ──
  function handleReadyTransition() {
    if (overlayEl) overlayEl.style.display = 'flex';
    if (soundEl) {
      soundEl.play().catch(() => {/* audio blocked — ignore */});
    }
    if (heroEl) {
      heroEl.classList.add('hero-ready-flash');
      setTimeout(() => heroEl.classList.remove('hero-ready-flash'), 3500);
    }
  }

  // ── Wait time text ──
  function formatWait(status, minutes) {
    if (status === 'ready')     return '✅ Ready now!';
    if (status === 'completed') return '🎉 Completed';
    if (status === 'cancelled') return '❌ Cancelled';
    if (minutes === null || minutes === undefined) return '—';
    if (minutes <= 1) return 'Less than 1 min';
    return `~${minutes} min`;
  }

  // ── Queue position text ──
  function formatPositionSub(pos, status) {
    if (status === 'ready')     return '✅ Ready for pickup!';
    if (status === 'completed') return '🎉 Order completed';
    if (status === 'cancelled') return '❌ Order cancelled';
    if (pos === 0) return '—';
    if (pos === 1) return "You're next!";
    return `${pos - 1} order${pos - 1 !== 1 ? 's' : ''} ahead of you`;
  }

  // ── Main poll ──
  async function poll() {
    if (isPolling) return;
    isPolling = true;

    try {
      const res  = await fetch(config.apiUrl);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();

      // Update emoji & label
      if (emojiEl) emojiEl.textContent = data.status_emoji;
      if (labelEl) labelEl.textContent = data.status_display;

      // Update hero class
      updateHeroClass(data.status);

      // Update progress steps
      updateProgressSteps(data.status);

      // Queue position
      if (posNumEl) posNumEl.textContent = data.queue_position || '—';
      if (posSubEl) posSubEl.textContent = formatPositionSub(data.queue_position, data.status);

      // Hide position card for final states
      if (posCardEl) {
        posCardEl.style.display = data.is_final ? 'none' : '';
      }

      // Wait time
      if (waitEl) waitEl.textContent = formatWait(data.status, data.estimated_minutes);

      // Ready transition — fire only once
      if (data.status === 'ready' && previousStatus !== 'ready') {
        handleReadyTransition();
      }

      previousStatus = data.status;

      // Stop polling when final
      if (data.is_final && pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }

    } catch (err) {
      console.warn('[Tracker] Poll error:', err);
    } finally {
      isPolling = false;
    }
  }

  // ── Bootstrap ──
  document.addEventListener('DOMContentLoaded', function () {
    // Initial state
    updateHeroClass(config.initialStatus);
    updateProgressSteps(config.initialStatus);

    // First poll immediately
    poll();

    // Then on interval (unless already final)
    const isFinal = config.initialStatus === 'completed' || config.initialStatus === 'cancelled';
    if (!isFinal) {
      pollTimer = setInterval(poll, config.pollInterval);
    }
  });

})();
