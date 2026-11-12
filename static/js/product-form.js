/**
 * product-form.js
 * Scoped behaviour for the Add / Edit Product page.
 *
 * Responsibilities:
 *   1. Has-Sizes toggle — show/hide the medium and large price field groups.
 *   2. Product image upload — custom drag-and-drop area with live preview,
 *      image replacement, and removal.
 *   3. Form submission guard — disable the submit button on first submit to
 *      prevent duplicate POSTs; show a spinner; re-enable on validation fail.
 *
 * None of these touch server-side validation, model fields, or POS logic.
 * The real <input type="checkbox"> and <input type="file"> elements remain
 * fully functional — this code only enhances their visual presentation.
 */
(function () {
  'use strict';

  /* ── Wait for DOM ready ─────────────────────────────────────────────── */
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  function init () {
    initHasSizesToggle();
    initImageUpload();
    initSubmitGuard();
    // Re-initialise Lucide icons if the library is already loaded
    // (base.html loads lucide after DOMContentLoaded, so this may be a no-op
    // on first load — that's fine, lucide.createIcons() runs globally too).
    if (window.lucide && typeof window.lucide.createIcons === 'function') {
      window.lucide.createIcons();
    }
  }

  /* ══════════════════════════════════════════════════════════════════════
     1. HAS-SIZES TOGGLE
     Shows/hides #pf-price-medium-group and #pf-price-large-group based on
     the state of the has_sizes checkbox. The hidden attribute keeps the
     elements out of both visual rendering AND the accessibility tree while
     not visible, matching the server-rendered initial state (hidden when
     has_sizes is False on page load).
     ══════════════════════════════════════════════════════════════════════ */
  function initHasSizesToggle () {
    var cbx = document.getElementById('id_has_sizes');
    var medGroup = document.getElementById('pf-price-medium-group');
    var lrgGroup = document.getElementById('pf-price-large-group');

    if (!cbx || !medGroup || !lrgGroup) return;

    function applyVisibility () {
      var on = cbx.checked;
      // hidden attribute hides from layout AND screen readers
      medGroup.hidden = !on;
      medGroup.setAttribute('aria-hidden', String(!on));
      lrgGroup.hidden = !on;
      lrgGroup.setAttribute('aria-hidden', String(!on));
    }

    // Apply immediately in case JS ran after the checkbox was rendered checked
    applyVisibility();
    cbx.addEventListener('change', applyVisibility);
  }

  /* ══════════════════════════════════════════════════════════════════════
     2. IMAGE UPLOAD / PREVIEW
     Clicking or pressing Enter/Space on the upload area opens the native
     file picker. Drag-and-drop is supported. After a file is selected a
     preview is shown inside the upload area. A "Remove image" button clears
     the selection (revokes the object URL to avoid memory leaks).
     ══════════════════════════════════════════════════════════════════════ */
  function initImageUpload () {
    var fileInput    = document.getElementById('id_image');
    var uploadArea   = document.getElementById('pf-upload-area');
    var placeholder  = document.getElementById('pf-upload-empty');    // right-panel placeholder
    var previewState = document.getElementById('pf-upload-preview');  // right-panel preview
    var previewImg   = document.getElementById('pf-preview-img');
    var removeBtn    = document.getElementById('pf-upload-remove-btn');

    if (!fileInput || !uploadArea) return;

    // If an existing image is already set (edit page) we don't auto-preview
    // it from the server — the "Current image:" note below the upload area
    // handles that case. We only show the live preview for newly-selected files.

    var currentObjectURL = null;

    /* ── Open file picker ─────────────────────────────────────────────── */
    function openPicker () {
      fileInput.click();
    }

    uploadArea.addEventListener('click', openPicker);

    // Keyboard: Enter or Space activate the upload area like a button
    uploadArea.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        openPicker();
      }
    });

    /* ── Drag and drop ────────────────────────────────────────────────── */
    uploadArea.addEventListener('dragover', function (e) {
      e.preventDefault();
      uploadArea.classList.add('is-dragging');
    });

    uploadArea.addEventListener('dragleave', function (e) {
      // Only remove if we're truly leaving the upload area (not a child)
      if (!uploadArea.contains(e.relatedTarget)) {
        uploadArea.classList.remove('is-dragging');
      }
    });

    uploadArea.addEventListener('drop', function (e) {
      e.preventDefault();
      uploadArea.classList.remove('is-dragging');
      var files = e.dataTransfer && e.dataTransfer.files;
      if (files && files.length > 0) {
        // Assign to the real input so the form POSTs it correctly
        // (DataTransfer is not directly assignable; use a new DataTransfer)
        try {
          var dt = new DataTransfer();
          dt.items.add(files[0]);
          fileInput.files = dt.files;
          handleFileSelected(files[0]);
        } catch (_) {
          // DataTransfer assignment not supported (rare); degrade silently —
          // the user can still click to select the file.
        }
      }
    });

    /* ── File input change ────────────────────────────────────────────── */
    fileInput.addEventListener('change', function () {
      if (fileInput.files && fileInput.files.length > 0) {
        handleFileSelected(fileInput.files[0]);
      }
    });

    /* ── Show preview ─────────────────────────────────────────────────── */
    function handleFileSelected (file) {
      if (!file.type.startsWith('image/')) {
        // Not an image — let server validation catch the error; just reset UI
        showEmpty();
        return;
      }

      // Revoke previous object URL to free memory
      if (currentObjectURL) {
        URL.revokeObjectURL(currentObjectURL);
      }

      currentObjectURL = URL.createObjectURL(file);
      // Set src and clear alt BEFORE showing the preview so the browser
      // never renders a broken-image icon for even a single frame.
      previewImg.src = currentObjectURL;
      previewImg.alt = 'Preview of ' + file.name;

      showPreview();

      // Re-run Lucide so the refresh-cw icon inside the overlay renders
      if (window.lucide && typeof window.lucide.createIcons === 'function') {
        window.lucide.createIcons();
      }
    }

    /* ── Remove / clear ───────────────────────────────────────────────── */
    if (removeBtn) {
      removeBtn.addEventListener('click', function (e) {
        e.stopPropagation(); // don't re-open the file picker
        clearImage();
      });
    }

    function clearImage () {
      // Clear the file input
      fileInput.value = '';

      // Revoke object URL to free memory
      if (currentObjectURL) {
        URL.revokeObjectURL(currentObjectURL);
        currentObjectURL = null;
      }

      // Clear src immediately before hiding so no broken-image flash occurs
      previewImg.src = '';
      previewImg.alt = '';

      showEmpty();
    }

    /* ── State helpers ────────────────────────────────────────────────── */
    function showPreview () {
      // Hide placeholder, show preview
      if (placeholder) {
        placeholder.hidden = true;
        placeholder.setAttribute('aria-hidden', 'true');
      }
      previewState.hidden = false;
      previewState.removeAttribute('aria-hidden');
      if (removeBtn) {
        removeBtn.hidden = false;
      }
    }

    function showEmpty () {
      // Hide preview, show placeholder
      previewState.hidden = true;
      previewState.setAttribute('aria-hidden', 'true');
      if (placeholder) {
        placeholder.hidden = false;
        placeholder.removeAttribute('aria-hidden');
      }
      if (removeBtn) {
        removeBtn.hidden = true;
      }
    }
  }

  /* ══════════════════════════════════════════════════════════════════════
     3. SUBMIT GUARD
     Disables the submit button after the first click to prevent accidental
     double-submission. If HTML5 constraint validation catches an error the
     form is not submitted and we immediately re-enable the button. Server-
     side Django validation handles the rest — on a failed POST the page
     reloads with errors and the button is naturally re-enabled.
     ══════════════════════════════════════════════════════════════════════ */
  function initSubmitGuard () {
    var form      = document.getElementById('product-form');
    var submitBtn = document.getElementById('pf-submit-btn');
    var label     = document.getElementById('pf-submit-label');
    var spinner   = document.getElementById('pf-submit-spinner');

    if (!form || !submitBtn) return;

    form.addEventListener('submit', function (e) {
      // checkValidity() is a native API that returns false if any required
      // field is empty or a pattern/min/max/type constraint is violated.
      // When it returns false the browser shows its own validation popups and
      // the submit event fires but navigation doesn't happen, so we must NOT
      // disable the button in that case.
      if (!form.checkValidity()) {
        // Let the browser handle constraint validation UI; do nothing.
        return;
      }

      // All client-side checks passed — disable to prevent duplicate POST.
      submitBtn.disabled = true;
      if (label) label.textContent = 'Saving…';
      if (spinner) spinner.hidden = false;

      // Safety net: re-enable after 15 s in case something goes wrong (e.g.
      // network error before the page unloads). This avoids a permanently
      // stuck button on client-only errors while not interfering with normal
      // server responses.
      setTimeout(function () {
        submitBtn.disabled = false;
        if (label) label.textContent = 'Save Product';
        if (spinner) spinner.hidden = true;
      }, 15000);
    });
  }

})();
