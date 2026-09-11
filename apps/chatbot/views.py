"""
Chatbot API view — customer-facing only, no authentication required.

Security:
  - CSRF protected (Django middleware + csrf_token in JS)
  - Input length capped at MAX_MESSAGE_LENGTH
  - Rate limiting: max RATE_LIMIT_MAX requests per RATE_LIMIT_WINDOW_SECONDS per session
  - No sensitive data returned
  - AI key never exposed to frontend
  - Order lookup restricted to public-safe fields only
"""
import json
import logging
import time

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from .service import get_chatbot_response, MAX_MESSAGE_LENGTH, MAX_HISTORY_TURNS

logger = logging.getLogger(__name__)

# Simple in-memory rate limiting per session
# Resets per worker restart — lightweight, Render-compatible, no Redis needed.
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX = 15   # max messages per minute per session

SESSION_CHAT_HISTORY_KEY = 'chatbot_history'
SESSION_RATE_KEY = 'chatbot_rate'


def _check_rate_limit(request) -> bool:
    """Return True if the request is allowed, False if rate-limited."""
    now = time.time()
    rate_data = request.session.get(SESSION_RATE_KEY, {'count': 0, 'window_start': now})

    if now - rate_data.get('window_start', now) > RATE_LIMIT_WINDOW_SECONDS:
        # New window
        rate_data = {'count': 1, 'window_start': now}
    else:
        rate_data['count'] = rate_data.get('count', 0) + 1

    request.session[SESSION_RATE_KEY] = rate_data
    return rate_data['count'] <= RATE_LIMIT_MAX


@csrf_protect
@require_POST
def chatbot_message(request):
    """
    POST /chatbot/message/

    Request body (JSON):
      { "message": "...", "reset": false }

    Response (JSON):
      { "response": "...", "intent": "..." }
    """
    # Ensure session exists (anonymous customers)
    if not request.session.session_key:
        request.session.create()

    # Rate limiting
    if not _check_rate_limit(request):
        return JsonResponse({
            'response': (
                "You're sending messages too quickly. "
                "Please wait a moment before asking another question."
            ),
            'intent': 'rate_limited',
        }, status=429)

    # Parse body
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Invalid request'}, status=400)

    message = str(body.get('message', '')).strip()
    reset = bool(body.get('reset', False))

    # Manage history
    if reset:
        request.session[SESSION_CHAT_HISTORY_KEY] = []

    history = request.session.get(SESSION_CHAT_HISTORY_KEY, [])

    # Validate message
    if not message:
        return JsonResponse({
            'response': "Please type a message and I'll do my best to help! 😊",
            'intent': 'empty',
        })

    if len(message) > MAX_MESSAGE_LENGTH:
        return JsonResponse({
            'response': (
                f"Your message is too long. "
                f"Please keep it under {MAX_MESSAGE_LENGTH} characters."
            ),
            'intent': 'too_long',
        })

    # Get response
    try:
        response_text, intent = get_chatbot_response(message, history)
    except Exception:
        logger.exception('Unexpected chatbot error')
        return JsonResponse({
            'response': (
                "I'm having trouble right now. "
                "You can still browse the menu and place your order normally. "
                "Sorry for the inconvenience!"
            ),
            'intent': 'error',
        })

    # Append to history (capped)
    history.append({'role': 'user',  'parts': [{'text': message}]})
    history.append({'role': 'model', 'parts': [{'text': response_text}]})
    # Keep only recent turns to bound session size
    history = history[-(MAX_HISTORY_TURNS * 2):]
    request.session[SESSION_CHAT_HISTORY_KEY] = history

    return JsonResponse({
        'response': response_text,
        'intent': intent,
    })
