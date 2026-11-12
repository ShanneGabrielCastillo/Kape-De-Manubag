/**
 * category-form.js
 * Scoped behaviour for the Add / Edit Category page.
 *
 * Responsibilities:
 *   1. Icon preview — update the emoji badge live as the user types.
 *   2. Category image upload — custom drag-and-drop area with live preview,
 *      replacement, and removal (mirrors product-form.js behaviour with
 *      category-specific element IDs).
 *   3. Form submission guard — prevent accidental double-POSTs.
 *
 * Element ID mapping (distinct from product-form.js):
 *   form          →  #category-form
 *   submit btn    →  #cf-submit-btn
 *   submit label  →  #cf-submit-label
 *   spinner       →  #cf-submit-spinner
 *   icon input    →  #id_icon
 *   icon preview  →  #cf-icon-preview
 *   file input    →  #id_image
 *   upload area   →  #cf-upload-area
 *   empty state   →  #cf-upload-empty
 *   preview state →  #cf-upload-preview
 *   preview img   →  #cf-preview-img
 *   remove btn    →  #cf-upload-remove-btn
 */
(function () {
  'use strict';

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  function init () {
    initIconPreview();
    initImageUpload();
    initSubmitGuard();
    if (window.lucide && typeof window.lucide.createIcons === 'function') {
      window.lucide.createIcons();
    }
  }

  /* ══════════════════════════════════════════════════════════════════════
     1. ICON PREVIEW
     Mirrors the typed emoji into the circular badge in real time.
     The badge is initialised server-side with the current icon value
     (defaulting to 🍽️ for the create page), so there is no flash.
     ══════════════════════════════════════════════════════════════════════ */
  function initIconPreview () {
    var input   = document.getElementById('id_icon');
    var preview = document.getElementById('cf-icon-preview');

    if (!input || !preview) return;

    function updatePreview () {
      var val = input.value.trim();
      // Show whatever the user typed; fall back to the default icon
      preview.textContent = val || '🍽️';
    }

    input.addEventListener('input', updatePreview);
    // Apply immediately so the badge reflects a pre-filled edit value
    updatePreview();
  }

  /* ══════════════════════════════════════════════════════════════════════
     2. IMAGE UPLOAD / PREVIEW
     Identical logic to product-form.js initImageUpload() but targeting
     the category form's element IDs (cf-* prefix).
     ══════════════════════════════════════════════════════════════════════ */
  function initImageUpload () {
    var fileInput    = document.getElementById('id_image');
    var uploadArea   = document.getElementById('cf-upload-area');
    var emptyState   = document.getElementById('cf-upload-empty');
    var previewState = document.getElementById('cf-upload-preview');
    var previewImg   = document.getElementById('cf-preview-img');
    var removeBtn    = document.getElementById('cf-upload-remove-btn');

    if (!fileInput || !uploadArea) return;

    var currentObjectURL = null;

    /* Open picker on click */
    uploadArea.addEventListener('click', function () {
      fileInput.click();
    });

    /* Keyboard activation (Enter / Space) */
    uploadArea.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        fileInput.click();
      }
    });

    /* Drag and drop */
    uploadArea.addEventListener('dragover', function (e) {
      e.preventDefault();
      uploadArea.classList.add('is-dragging');
    });

    uploadArea.addEventListener('dragleave', function (e) {
      if (!uploadArea.contains(e.relatedTarget)) {
        uploadArea.classList.remove('is-dragging');
      }
    });

    uploadArea.addEventListener('drop', function (e) {
      e.preventDefault();
      uploadArea.classList.remove('is-dragging');
      var files = e.dataTransfer && e.dataTransfer.files;
      if (files && files.length > 0) {
        try {
          var dt = new DataTransfer();
          dt.items.add(files[0]);
          fileInput.files = dt.files;
          handleFileSelected(files[0]);
        } catch (_) {
          /* DataTransfer not supported — user can still click to select */
        }
      }
    });

    /* File input change */
    fileInput.addEventListener('change', function () {
      if (fileInput.files && fileInput.files.length > 0) {
        handleFileSelected(fileInput.files[0]);
      }
    });

    /* Show preview */
    function handleFileSelected (file) {
      if (!file.type.startsWith('image/')) {
        showEmpty();
        return;
      }
      if (currentObjectURL) {
        URL.revokeObjectURL(currentObjectURL);
      }
      currentObjectURL = URL.createObjectURL(file);
      previewImg.src   = currentObjectURL;
      previewImg.alt   = 'Preview of ' + file.name;
      showPreview();
      if (window.lucide && typeof window.lucide.createIcons === 'function') {
        window.lucide.createIcons();
      }
    }

    /* Remove button */
    if (removeBtn) {
      removeBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        fileInput.value = '';
        if (currentObjectURL) {
          URL.revokeObjectURL(currentObjectURL);
          currentObjectURL = null;
        }
        previewImg.src = '';
        previewImg.alt = '';
        showEmpty();
      });
    }

    function showPreview () {
      emptyState.hidden = true;
      emptyState.setAttribute('aria-hidden', 'true');
      previewState.hidden = false;
      previewState.removeAttribute('aria-hidden');
      if (removeBtn) removeBtn.hidden = false;
    }

    function showEmpty () {
      previewState.hidden = true;
      previewState.setAttribute('aria-hidden', 'true');
      emptyState.hidden = false;
      emptyState.removeAttribute('aria-hidden');
      if (removeBtn) removeBtn.hidden = true;
    }
  }

  /* ══════════════════════════════════════════════════════════════════════
     3. SUBMIT GUARD
     Disables the submit button after the first valid submission to prevent
     accidental duplicate POSTs. Re-enables after 15 s as a safety net.
     ══════════════════════════════════════════════════════════════════════ */
  function initSubmitGuard () {
    var form      = document.getElementById('category-form');
    var submitBtn = document.getElementById('cf-submit-btn');
    var label     = document.getElementById('cf-submit-label');
    var spinner   = document.getElementById('cf-submit-spinner');

    if (!form || !submitBtn) return;

    form.addEventListener('submit', function () {
      if (!form.checkValidity()) return;

      submitBtn.disabled = true;
      if (label)   label.textContent = 'Saving…';
      if (spinner) spinner.hidden    = false;

      setTimeout(function () {
        submitBtn.disabled = false;
        if (label)   label.textContent = 'Save Category';
        if (spinner) spinner.hidden    = true;
      }, 15000);
    });
  }

})();
