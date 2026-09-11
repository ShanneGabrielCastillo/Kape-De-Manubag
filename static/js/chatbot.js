/* =====================================================================
   Kape De Manubag — Customer Chatbot JS
   Loaded only on the customer menu page (menu/index.html).
   API key is NEVER present in this file — all AI calls go through Django.
   ===================================================================== */
(function () {
  'use strict';

  // ── DOM refs ──────────────────────────────────────────────────────────────
  const chatBtn      = document.getElementById('kdm-chat-btn');
  const chatPanel    = document.getElementById('kdm-chat-panel');
  const closeBtn     = document.getElementById('kdm-chat-close');
  const messagesEl   = document.getElementById('kdm-chat-messages');
  const inputEl      = document.getElementById('kdm-chat-input');
  const sendBtn      = document.getElementById('kdm-chat-send');
  const quickReplies = document.getElementById('kdm-quick-replies');
  const dotEl        = document.getElementById('kdm-chat-dot');

  if (!chatBtn || !chatPanel) return;   // guard: only runs when UI is present

  // ── State ─────────────────────────────────────────────────────────────────
  let isOpen     = false;
  let isSending  = false;
  let hasOpened  = false;   // whether the user has ever opened the chat

  const CSRF_TOKEN = (function () {
    // Read the CSRF token from the cookie — same approach as main.js
    const name = 'csrftoken';
    const cookies = document.cookie.split(';');
    for (const c of cookies) {
      const [k, v] = c.trim().split('=');
      if (k === name) return decodeURIComponent(v);
    }
    return '';
  })();

  const CHAT_API_URL = '/chatbot/message/';
  const MAX_MSG_LEN  = 500;

  // ── Open / Close ──────────────────────────────────────────────────────────
  function openChat() {
    isOpen = true;
    chatPanel.classList.add('open');
    chatBtn.setAttribute('aria-expanded', 'true');
    // Hide the unread dot once opened
    if (dotEl) dotEl.classList.remove('visible');
    // Show welcome message on first open
    if (!hasOpened) {
      hasOpened = true;
      appendBotMessage(
        "Hi! 👋 I'm the Kape De Manubag Assistant. " +
        "How can I help you today?"
      );
      showQuickReplies();
    }
    // Focus the input after animation
    setTimeout(() => inputEl && inputEl.focus(), 250);
  }

  function closeChat() {
    isOpen = false;
    chatPanel.classList.remove('open');
    chatBtn.setAttribute('aria-expanded', 'false');
  }

  chatBtn.addEventListener('click', function () {
    if (isOpen) closeChat(); else openChat();
  });
  closeBtn.addEventListener('click', closeChat);

  // Close on Escape key
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && isOpen) closeChat();
  });

  // ── Quick reply buttons ───────────────────────────────────────────────────
  const QUICK_QUESTIONS = [
    { label: '🍽️ What food do you have?',        msg: 'What food do you have?' },
    { label: '💳 Payment methods?',               msg: 'What payment methods do you accept?' },
    { label: '🥡 Is takeout available?',          msg: 'Is takeout available? Tell me about the packaging fee.' },
    { label: '🛒 How do I place an order?',       msg: 'How do I place an order?' },
    { label: '📦 Track my order',                 msg: 'I want to track my order' },
  ];

  function showQuickReplies() {
    if (!quickReplies) return;
    quickReplies.innerHTML = '';
    QUICK_QUESTIONS.forEach(function (q) {
      const btn = document.createElement('button');
      btn.className = 'kdm-quick-btn';
      btn.textContent = q.label;
      btn.setAttribute('aria-label', q.msg);
      btn.addEventListener('click', function () {
        quickReplies.innerHTML = '';    // hide after first use
        sendMessage(q.msg);
      });
      quickReplies.appendChild(btn);
    });
  }

  // ── Message rendering ─────────────────────────────────────────────────────
  function appendMessage(text, role) {
    const wrapper = document.createElement('div');
    wrapper.className = 'kdm-msg kdm-msg-' + role;

    const bubble = document.createElement('div');
    bubble.className = 'kdm-msg-bubble';

    // Safely render limited Markdown: **bold** only, no HTML injection
    // Use textContent for user messages (untrusted), innerHTML for bot (controlled)
    if (role === 'user') {
      bubble.textContent = text;
    } else {
      // Escape HTML first, then apply **bold**
      const safe = text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
      bubble.innerHTML = safe;
    }

    wrapper.appendChild(bubble);
    messagesEl.appendChild(wrapper);
    scrollToBottom();
    return wrapper;
  }

  function appendBotMessage(text) {
    return appendMessage(text, 'bot');
  }

  function appendUserMessage(text) {
    return appendMessage(text, 'user');
  }

  function showTyping() {
    const wrapper = document.createElement('div');
    wrapper.className = 'kdm-msg kdm-typing';
    wrapper.id = 'kdm-typing-indicator';
    wrapper.innerHTML = '<div class="kdm-typing-dots"><span></span><span></span><span></span></div>';
    messagesEl.appendChild(wrapper);
    scrollToBottom();
  }

  function removeTyping() {
    const el = document.getElementById('kdm-typing-indicator');
    if (el) el.remove();
  }

  function scrollToBottom() {
    if (messagesEl) {
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
  }

  // ── Send message ──────────────────────────────────────────────────────────
  function sendMessage(text) {
    text = (text || '').trim();
    if (!text || isSending) return;

    if (text.length > MAX_MSG_LEN) {
      appendBotMessage(
        `Please keep your message under ${MAX_MSG_LEN} characters.`
      );
      return;
    }

    isSending = true;
    sendBtn.disabled = true;

    appendUserMessage(text);
    if (inputEl) inputEl.value = '';
    resizeInput();
    showTyping();
    scrollToBottom();

    fetch(CHAT_API_URL, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': CSRF_TOKEN,
      },
      body: JSON.stringify({ message: text }),
    })
      .then(function (res) {
        // Handle rate limit, server errors etc.
        if (res.status === 429) {
          return { response: "You're sending messages too quickly. Please wait a moment." };
        }
        if (!res.ok) {
          return {
            response:
              "I'm having trouble connecting right now. " +
              "You can still browse the menu and place your order normally.",
          };
        }
        return res.json();
      })
      .then(function (data) {
        removeTyping();
        const reply = data.response || data.error ||
          "Sorry, I couldn't get a response. Please try again.";
        appendBotMessage(reply);
      })
      .catch(function () {
        removeTyping();
        appendBotMessage(
          "I'm having trouble connecting right now. " +
          "You can still browse the menu and place your order normally."
        );
      })
      .finally(function () {
        isSending = false;
        sendBtn.disabled = false;
        if (inputEl) inputEl.focus();
      });
  }

  // ── Input handling ────────────────────────────────────────────────────────
  function resizeInput() {
    if (!inputEl) return;
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 80) + 'px';
  }

  if (inputEl) {
    inputEl.addEventListener('input', resizeInput);

    inputEl.addEventListener('keydown', function (e) {
      // Enter sends, Shift+Enter adds newline
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage(inputEl.value);
      }
    });
  }

  if (sendBtn) {
    sendBtn.addEventListener('click', function () {
      sendMessage(inputEl ? inputEl.value : '');
    });
  }

  // ── Show unread dot after a short delay (draw attention) ──────────────────
  setTimeout(function () {
    if (!hasOpened && dotEl) dotEl.classList.add('visible');
  }, 3000);

})();
