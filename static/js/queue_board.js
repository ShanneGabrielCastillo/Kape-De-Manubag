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

  // ── Column sync (diff-based: only affected tiles are touched) ───────────
  // Instead of wiping and rebuilding a column on every poll (which flashes
  // the whole board and forces a reflow of every tile), each poll diffs the
  // incoming orders against the tiles already on screen:
  //   - unchanged tiles keep their DOM element untouched;
  //   - new orders get a tile appended;
  //   - only orders that left the list are removed.
  function syncColumn(container, countEl, items, tileClass, opts) {
    if (!container) return;
    const emptyIcon  = (opts && opts.emptyIcon)  || '🍳';
    const emptyText  = (opts && opts.emptyText)  || 'No orders';
    const markNew    = !!(opts && opts.markNew);

    const existing = new Map();
    container.querySelectorAll('.order-tile').forEach(tile => {
      const numEl = tile.querySelector('.tile-number');
      if (!numEl) return;
      const n = parseInt(numEl.textContent.replace('#', ''), 10);
      if (!isNaN(n)) existing.set(n, tile);
    });

    if (!items || items.length === 0) {
      // Empty column: drop any tiles and show the empty state.
      existing.forEach(tile => tile.remove());
      if (!container.querySelector('.empty-state')) {
        container.appendChild(emptyState(emptyIcon, emptyText));
      }
      if (countEl) countEl.textContent = '0';
      return;
    }

    // Non-empty: the empty state (if any) goes away.
    container.querySelectorAll('.empty-state').forEach(el => el.remove());

    const seen = new Set();
    items.forEach(item => {
      const qn = item.queue_number;
      seen.add(qn);
      const tile = existing.get(qn);
      if (tile) {
        // Keep the existing element; refresh its text only if the underlying
        // data changed (customer name / order type).
        const prev = tile._data || {};
        if (prev.customer_name !== item.customer_name || prev.order_type !== item.order_type) {
          const nameEl  = tile.querySelector('.tile-name');
          const typeEl  = tile.querySelector('.tile-type');
          if (nameEl) nameEl.textContent = (item.customer_name || '').substring(0, 12);
          if (typeEl) typeEl.textContent = item.order_type === 'dine_in' ? 'Dine-In' : 'Take-Out';
        }
        tile._data = item;
      } else {
        // A brand-new tile (newly arrived order, or one that just moved into
        // this column) — highlight newly-ready orders as before.
        const isNew = markNew && !knownReadyNums.has(qn) && knownReadyNums.size > 0;
        const tileEl = makeTile(item, tileClass, isNew);
        tileEl._data = item;
        container.appendChild(tileEl);
      }
    });

    // Remove only the tiles whose queue number left the list.
    existing.forEach((tile, qn) => {
      if (!seen.has(qn)) tile.remove();
    });

    if (countEl) countEl.textContent = items.length;
  }

  // ── Main poll ──
  async function poll() {
    if (isPolling) return;
    isPolling = true;

    try {
      const res  = await fetch(config.apiUrl);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();

      // ── Preparing column ──
      syncColumn(
        document.getElementById('preparing-tiles'),
        document.getElementById('preparing-count'),
        data.preparing,
        'preparing-tile',
        { emptyIcon: '🍳', emptyText: 'No orders preparing' }
      );

      // ── Ready column ──
      syncColumn(
        document.getElementById('ready-tiles'),
        document.getElementById('ready-count'),
        data.ready,
        'ready-tile',
        { emptyIcon: '✅', emptyText: 'No orders ready yet', markNew: true }
      );
      knownReadyNums = new Set((data.ready || []).map(i => i.queue_number));

      // ── Waiting badge (only when the text actually changed) ──
      const badge = document.getElementById('waiting-count-badge');
      if (badge) {
        const text = data.waiting_count > 0 ? `⏳ ${data.waiting_count} waiting` : 'No orders waiting';
        if (badge.textContent !== text) badge.textContent = text;
      }

      // ── Last updated (only when it changed) ──
      const lastUpdated = document.getElementById('last-updated-text');
      if (lastUpdated) {
        const text = 'Last updated: ' + formatTime(data.last_updated);
        if (lastUpdated.textContent !== text) lastUpdated.textContent = text;
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

    // Pause polling while the tab is hidden (a kitchen screen covered by
    // another window shouldn't hammer the API), and refresh immediately on
    // return so the board is never stale when it becomes visible again.
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) poll();
    });

    poll();
    setInterval(() => {
      if (!document.hidden) poll();
    }, config.pollInterval);
  });

})();
