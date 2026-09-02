/**
 * Responsive Tables — Kape De Manubag System
 * Converts table rows to mobile cards on small screens.
 * Tables with data-mobile-cards="true" are processed.
 */
(function () {
  'use strict';

  /**
   * Builds one mobile card from one <tr> element.
   * @param {HTMLTableRowElement} row
   * @param {Array<{text: string, role: string}>} headers
   * @returns {HTMLElement|null}
   */
  function buildCard(row, headers) {
    const cells = row.querySelectorAll('td');
    if (!cells.length) return null;

    const card       = document.createElement('div');
    card.className   = 'table-card';

    const cardHeader = document.createElement('div');
    cardHeader.className = 'table-card-header';

    const cardBody   = document.createElement('div');
    cardBody.className = 'table-card-body';

    const cardFooter = document.createElement('div');
    cardFooter.className = 'table-card-footer';

    const actionsDiv = document.createElement('div');
    actionsDiv.className = 'table-card-actions';
    cardFooter.appendChild(actionsDiv);

    // Copy data-order-id or similar row-level attributes
    if (row.dataset.orderId) {
      card.dataset.orderId = row.dataset.orderId;
    }

    // Empty-state rows are a single <td colspan="N"> (e.g. "No categories
    // yet."). Without this guard the colspan cell is matched against the
    // first column's role -- often a hidden icon/status column -- and the
    // card comes out blank on mobile.
    if (cells.length === 1 && cells[0].colSpan > 1) {
      const firstChild = cells[0].firstElementChild;
      if (firstChild && firstChild.classList.contains('empty-state')) {
        // Rich empty-state block (e.g. dashboard widget placeholders): keep
        // its markup, cloned so the hidden table row stays intact.
        card.appendChild(firstChild.cloneNode(true));
      } else {
        const note = document.createElement('div');
        note.className = 'table-card-empty';
        note.textContent = cells[0].textContent.trim() || 'No records found';
        card.appendChild(note);
      }
      return card;
    }

    cells.forEach(function (td, i) {
      const header  = headers[i] || { text: '', role: 'secondary' };
      const role    = header.role;
      const content = td.innerHTML;

      switch (role) {
        case 'primary': {
          const el = document.createElement('div');
          el.className = 'table-card-title';
          el.innerHTML = content;
          cardHeader.appendChild(el);
          break;
        }
        case 'status': {
          const el = document.createElement('div');
          el.className = 'table-card-status';
          el.innerHTML = content;
          cardHeader.appendChild(el);
          break;
        }
        case 'amount': {
          const el = document.createElement('div');
          el.className = 'table-card-amount';
          el.innerHTML = content;
          cardHeader.appendChild(el);
          break;
        }
        case 'date': {
          const el = document.createElement('div');
          el.className = 'table-card-date';
          el.innerHTML = content;
          cardBody.appendChild(el);
          break;
        }
        case 'actions': {
          actionsDiv.innerHTML += content;
          break;
        }
        case 'hide':
          // Intentionally skip
          break;
        default: {
          // 'secondary': label-value pair
          if (!content.trim() || content.trim() === '—') break;
          const row2  = document.createElement('div');
          row2.className = 'table-card-row';
          row2.innerHTML =
            '<span class="table-card-label">' + header.text + ':</span>' +
            '<span class="table-card-value">' + content + '</span>';
          cardBody.appendChild(row2);
          break;
        }
      }
    });

    card.appendChild(cardHeader);
    if (cardBody.children.length) card.appendChild(cardBody);
    if (actionsDiv.children.length || actionsDiv.innerHTML.trim()) {
      card.appendChild(cardFooter);
    }

    return card;
  }

  /**
   * Processes one table: reads headers, builds cards container,
   * inserts it after the table-wrapper.
   * @param {HTMLTableElement} table
   */
  function processTable(table) {
    const wrapper = table.closest('.table-wrapper') || table.parentElement;

    // Remove any previously generated cards (idempotency)
    // Cards are inserted AFTER the nearest .card ancestor, so look there too
    const cardAncestor = wrapper.closest('.card') || wrapper.parentElement;
    const existing = cardAncestor.parentElement
      ? cardAncestor.parentElement.querySelector(':scope > .table-mobile-cards')
      : null;
    if (existing) existing.remove();

    // Read headers
    const thElements = table.querySelectorAll('thead th');
    const headers = Array.from(thElements).map(function (th) {
      return {
        text: th.textContent.trim(),
        role: th.dataset.role || 'secondary',
      };
    });

    const container = document.createElement('div');
    container.className = 'table-mobile-cards';

    const tbody = table.querySelector('tbody');
    if (tbody) {
      const rows = tbody.querySelectorAll('tr');
      rows.forEach(function (row) {
        const card = buildCard(row, headers);
        if (card) container.appendChild(card);
      });
    }

    // If no cards were built, show empty state
    if (!container.children.length) {
      const emptyEl = document.createElement('div');
      emptyEl.className = 'empty-state';
      emptyEl.style.padding = '32px';
      emptyEl.style.textAlign = 'center';
      emptyEl.style.color = '#aaa';
      emptyEl.textContent = 'No records found';
      container.appendChild(emptyEl);
    }

    // Insert AFTER the .card ancestor (outside overflow:hidden) so cards are visible
    const insertTarget = wrapper.closest('.card') || wrapper;
    insertTarget.insertAdjacentElement('afterend', container);
  }

  /**
   * Main entry point — processes all responsive tables on the page.
   * @returns {number} count of tables processed
   */
  function initResponsiveTables() {
    const tables = document.querySelectorAll('table[data-mobile-cards="true"]');
    tables.forEach(processTable);
    return tables.length;
  }

  // Initialize on DOM ready
  document.addEventListener('DOMContentLoaded', function () {
    initResponsiveTables();
  });

  // Expose for external calls (post-AJAX updates, realtime inserts)
  window.initResponsiveTables = initResponsiveTables;

  // Watch for DOM mutations in table wrappers so AJAX-updated tables re-render
  document.addEventListener('DOMContentLoaded', function () {
    const observer = new MutationObserver(function (mutations) {
      let needsRebuild = false;
      mutations.forEach(function (m) {
        if (m.target.tagName === 'TBODY' ||
            m.target.closest && m.target.closest('.table-wrapper')) {
          needsRebuild = true;
        }
      });
      if (needsRebuild) initResponsiveTables();
    });

    document.querySelectorAll('.table-wrapper').forEach(function (wrapper) {
      const tbody = wrapper.querySelector('tbody');
      if (tbody) {
        observer.observe(tbody, { childList: true, subtree: false });
      }
    });
  });

})();
