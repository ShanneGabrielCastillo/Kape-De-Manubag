/* =====================================================
   QUEUE BOARD — Kape De Manubag
   Polls the API every 5 seconds to update the board.
   ===================================================== */

(function () {
  const config = window.BOARD_CONFIG;
  if (!config) return;

  let isPolling       = false;
  let knownReadyNums  = new Set();

  // ── Clock ──
  function updateClock() {
    const el = document.getElementById('board-clock');
    if (!el) return;
    const now  = new Date();
    let h      = now.getHours();
    const m    = String(now.getMinutes()).padStart(2, '0');
    const s    = String(now.getSeconds()).padStart(2, '0');
    const ampm = h >= 12 ? 'PM' : 'AM';
    h = h % 12 || 12;
    el.textContent = `${h}:${m}:${s} ${ampm}`;
  }
  setInterval(updateClock, 1000);
  updateClock();

  // ── Build a tile element ──
  function makeTile(item, tileClass, isNew) {
    const div = document.createElement('div');
    div.className = 'order-tile ' + tileClass + (isNew ? ' tile-new-ready' : '');
    div.innerHTML = `
      <div class="tile-number">#${item.queue_number}</div>
      <div class="tile-name">${(item.customer_name || '').substring(0, 12)}</div>
      <div class="tile-type">${item.order_type === 'dine_in' ? 'Dine-In' : 'Take-Out'}</div>
    `;
    if (isNew) {
      setTimeout(() => div.classList.remove('tile-new-ready'), 4000);
    }
    return div;
  }

  // ── Empty state ──
  function emptyState(icon, text) {
    const div = document.createElement('div');
    div.className = 'empty-state';
    div.innerHTML = `<div class="empty-icon">${icon}</div><p>${text}</p>`;
    return div;
  }

  // ── Format time ──
  function formatTime(isoStr) {
    if (!isoStr) return '';
    const d = new Date(isoStr);
    let h = d.getHours(), m = String(d.getMinutes()).padStart(2, '0');
    const ampm = h >= 12 ? 'PM' : 'AM';
    h = h % 12 || 12;
    return `${h}:${m} ${ampm}`;
  }

  // ── Main poll ──
  async function poll() {
    if (isPolling) return;
    isPolling = true;

    try {
      const res  = await fetch(config.apiUrl);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();

      // ── Preparing tiles ──
      const prepTiles = document.getElementById('preparing-tiles');
      const prepCount = document.getElementById('preparing-count');
      if (prepTiles) {
        prepTiles.innerHTML = '';
        if (!data.preparing || data.preparing.length === 0) {
          prepTiles.appendChild(emptyState('🍳', 'No orders preparing'));
        } else {
          data.preparing.forEach(item => {
            prepTiles.appendChild(makeTile(item, 'preparing-tile', false));
          });
        }
      }
      if (prepCount) prepCount.textContent = data.preparing ? data.preparing.length : 0;

      // ── Ready tiles ──
      const readyTiles = document.getElementById('ready-tiles');
      const readyCount = document.getElementById('ready-count');
      const newReadyNums = new Set((data.ready || []).map(i => i.queue_number));

      if (readyTiles) {
        readyTiles.innerHTML = '';
        if (!data.ready || data.ready.length === 0) {
          readyTiles.appendChild(emptyState('✅', 'No orders ready yet'));
        } else {
          data.ready.forEach(item => {
            const isNew = !knownReadyNums.has(item.queue_number) && knownReadyNums.size > 0;
            readyTiles.appendChild(makeTile(item, 'ready-tile', isNew));
          });
        }
      }
      if (readyCount) readyCount.textContent = data.ready ? data.ready.length : 0;
      knownReadyNums = newReadyNums;

      // ── Waiting badge ──
      const badge = document.getElementById('waiting-count-badge');
      if (badge) {
        if (data.waiting_count > 0) {
          badge.textContent = `⏳ ${data.waiting_count} waiting`;
        } else {
          badge.textContent = 'No orders waiting';
        }
      }

      // ── Last updated ──
      const lastUpdated = document.getElementById('last-updated-text');
      if (lastUpdated) {
        lastUpdated.textContent = 'Last updated: ' + formatTime(data.last_updated);
      }

    } catch (err) {
      console.warn('[QueueBoard] Poll error:', err);
    } finally {
      isPolling = false;
    }
  }

  // ── Bootstrap ──
  document.addEventListener('DOMContentLoaded', function () {
    // Seed known ready numbers from initial server-rendered tiles
    document.querySelectorAll('#ready-tiles .order-tile').forEach(tile => {
      const numEl = tile.querySelector('.tile-number');
      if (numEl) {
        const n = parseInt(numEl.textContent.replace('#', ''));
        if (!isNaN(n)) knownReadyNums.add(n);
      }
    });

    poll();
    setInterval(poll, config.pollInterval);
  });

})();
