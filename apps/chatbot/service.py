"""
Kape De Manubag — Chatbot service module.

Architecture:
  1. Intent detection (deterministic, no API call for simple questions).
  2. Gemini AI (if API key configured and intent needs AI).
  3. Fallback response (if AI unavailable or quota exceeded).

The AI NEVER receives database credentials, raw SQL, or sensitive data.
Django retrieves and sanitises all business data before passing it to the AI.
The AI is treated as a response-generation component only — READ-ONLY.
"""
import logging
import os
from decimal import Decimal

from django.conf import settings

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
MAX_MESSAGE_LENGTH = 500          # characters — reject longer messages
MAX_HISTORY_TURNS = 6             # pairs kept in session to bound prompt size
AI_TIMEOUT_SECONDS = 10           # Gemini request timeout

# ── Intent keywords ───────────────────────────────────────────────────────────
_MENU_KEYWORDS = [
    'menu', 'food', 'meal', 'drink', 'eat', 'available', 'offer',
    'have', 'sell', 'product', 'item', 'coffee', 'tea', 'snack',
    'burger', 'rice', 'pastil', 'combo', 'category', 'categories',
    'dishes', 'beverages', 'what do you', 'what can i order',
]
_PAYMENT_KEYWORDS = [
    'pay', 'payment', 'gcash', 'cash', 'method', 'how to pay',
    'accept', 'card', 'debit', 'credit',
]
_TAKEOUT_KEYWORDS = [
    'takeout', 'take-out', 'take out', 'dine in', 'dine-in', 'dinein',
    'delivery', 'packaging', 'fee', 'charge', 'bring out',
]
_ORDER_KEYWORDS = [
    'order', 'place', 'how to order', 'checkout', 'process', 'buy',
    'purchase', 'cart', 'basket',
]
_STATUS_KEYWORDS = [
    'status', 'track', 'where is', 'my order', 'order status',
    'kdm-', 'kdm', 'tracking', 'ready', 'preparing',
]
_HOURS_KEYWORDS = [
    'open', 'close', 'hours', 'time', 'schedule', 'operating',
    'when', 'store hours',
]
_PRICE_KEYWORDS = [
    'price', 'cost', 'how much', 'cheapest', 'expensive', 'affordable',
    'cheap', 'promo', 'discount',
]
_GREETING_KEYWORDS = [
    'hi', 'hello', 'hey', 'good morning', 'good afternoon', 'good evening',
    'kumusta', 'musta', 'hola',
]


def detect_intent(message: str) -> str:
    """Return a coarse intent label for the message. Fast, no DB, no AI."""
    m = message.lower()
    if any(k in m for k in _GREETING_KEYWORDS) and len(m) < 40:
        return 'greeting'
    if any(k in m for k in _STATUS_KEYWORDS):
        return 'order_status'
    if any(k in m for k in _PAYMENT_KEYWORDS):
        return 'payment'
    if any(k in m for k in _TAKEOUT_KEYWORDS):
        return 'takeout'
    if any(k in m for k in _PRICE_KEYWORDS):
        return 'price'
    if any(k in m for k in _MENU_KEYWORDS):
        return 'menu'
    if any(k in m for k in _ORDER_KEYWORDS):
        return 'ordering'
    if any(k in m for k in _HOURS_KEYWORDS):
        return 'hours'
    return 'general'


# ── Database helpers ──────────────────────────────────────────────────────────

def get_menu_context() -> str:
    """
    Return a compact, safe text summary of available products/categories.
    Only active + available products are included.
    No sensitive data. No prices for out-of-stock items.
    Keeps the string short to avoid huge AI prompts.
    """
    from apps.menu.models import Category, Product
    try:
        categories = Category.objects.filter(is_active=True).prefetch_related(
            'products'
        ).order_by('order', 'name')

        lines = []
        for cat in categories:
            prods = [
                p for p in cat.products.all()
                if p.is_active and p.is_available
            ]
            if not prods:
                continue
            items = []
            for p in prods[:12]:   # cap per category to keep prompt small
                price_str = f'₱{p.price}'
                if p.has_sizes and p.price_medium:
                    price_str += f' / Medium ₱{p.price_medium}'
                if p.has_sizes and p.price_large:
                    price_str += f' / Large ₱{p.price_large}'
                stock = '(out of stock)' if p.stock_quantity == 0 else ''
                items.append(f'  - {p.name}: {price_str} {stock}'.strip())
            if items:
                lines.append(f'{cat.name}:')
                lines.extend(items)
        return '\n'.join(lines) if lines else 'Menu is currently unavailable.'
    except Exception:
        logger.exception('get_menu_context failed')
        return 'Menu data could not be retrieved at this time.'


def get_packaging_fee_display() -> str:
    """Return the current packaging fee as a string for display."""
    try:
        from apps.orders.services import get_packaging_fee_per_item
        fee = get_packaging_fee_per_item()
        return f'₱{fee:.2f}'
    except Exception:
        return '₱6.00'


def lookup_order_status(order_number: str) -> dict:
    """
    Safely look up public order status by order number.

    Returns only safe, non-sensitive fields:
      - status_display
      - order_type
      - queue_position (if active)
      - is_final

    Never returns: customer name, phone, payment details,
    notes, cashier info, internal fields.
    """
    from apps.orders.models import Order
    try:
        order = Order.objects.only(
            'order_number', 'status', 'order_type',
            'queue_number', 'created_at',
        ).get(order_number=order_number.upper().strip())

        is_final = order.status in ('completed', 'cancelled')
        status_map = {
            'pending':   'Received — waiting to be prepared',
            'preparing': 'Being prepared by the kitchen',
            'ready':     'Ready for pickup!',
            'completed': 'Completed',
            'cancelled': 'Cancelled',
        }
        result = {
            'found': True,
            'order_number': order.order_number,
            'status': order.status,
            'status_display': status_map.get(order.status, order.get_status_display()),
            'order_type': order.get_order_type_display(),
            'is_final': is_final,
        }
        if not is_final:
            result['queue_position'] = order.get_queue_position()
        return result
    except Order.DoesNotExist:
        return {'found': False}
    except Exception:
        logger.exception('lookup_order_status failed for %s', order_number)
        return {'found': False, 'error': True}


# ── Fallback responses ────────────────────────────────────────────────────────

def fallback_response(intent: str, message: str) -> str:
    """
    Deterministic fallback used when:
      - AI API key not configured
      - AI quota exceeded
      - AI request timed out
      - Any other AI failure
    """
    fee = get_packaging_fee_display()

    if intent == 'greeting':
        return (
            "Hello! 👋 Welcome to Kape De Manubag. "
            "I can help you with menu questions, payment methods, "
            "ordering, or tracking your order. What can I help you with?"
        )
    if intent == 'payment':
        return (
            "We currently accept **Cash** and **GCash** as payment methods. "
            "Payment is made at the counter after your order is ready. "
            "We do not accept credit cards or other payment methods at this time."
        )
    if intent == 'takeout':
        return (
            f"Yes, takeout is available! 🥡 "
            f"A packaging fee of {fee} applies to each eligible meal item for takeout orders. "
            f"Drinks (coffee, milk tea, etc.) are not charged the packaging fee. "
            f"Select 'Take-Out' when placing your order."
        )
    if intent == 'ordering':
        return (
            "Here's how to order:\n"
            "1. Browse the menu and tap **+** to add items to your cart 🛒\n"
            "2. Review your cart and go to checkout\n"
            "3. Enter your name and choose Dine-In or Take-Out\n"
            "4. Submit your order\n"
            "5. Pay at the counter when your order is ready\n\n"
            "You can track your order status using your order number."
        )
    if intent == 'menu':
        try:
            ctx = get_menu_context()
            return f"Here's what we currently have available:\n\n{ctx}"
        except Exception:
            return (
                "We have a variety of coffee, milk tea, meals, and snacks. "
                "Please browse the menu above to see all available items and prices."
            )
    if intent == 'price':
        return (
            "You can see all current prices by browsing the menu above. "
            "Prices vary by item and size. "
            "Is there a specific item you'd like to know the price of?"
        )
    if intent == 'hours':
        return (
            "For store hours and location, please contact us directly or "
            "ask our staff. You can place orders anytime the menu is available."
        )
    if intent == 'order_status':
        return (
            "To check your order status, please provide your order number "
            "(e.g. KDM-20260901-0001). "
            "You can also track your order at the order tracker page."
        )
    # general
    return (
        "I'm here to help with questions about our menu, payment methods, "
        "ordering, and order status. "
        "You can also browse the menu above to see all available items. "
        "What would you like to know?"
    )


# ── Gemini AI integration ─────────────────────────────────────────────────────

def _get_api_key() -> str | None:
    """Return the Gemini API key from settings/env. Never None if not configured."""
    return getattr(settings, 'GEMINI_API_KEY', None) or os.environ.get('GEMINI_API_KEY')


def _build_system_prompt(intent: str) -> str:
    """
    Build the system instruction for Gemini.
    Inject only the data relevant to the question intent to keep prompts small.
    """
    fee = get_packaging_fee_display()
    base = (
        "You are the Kape De Manubag Customer Assistant — a friendly, helpful chatbot "
        "for a café called Kape De Manubag in the Philippines. "
        "You help customers with menu questions, payment, ordering, and order status. "
        "Be concise, warm, and accurate. "
        "Do NOT invent products, prices, or information not provided to you. "
        "If you don't know something, say so honestly and direct the customer to staff. "
        "Do NOT expose any staff, admin, finance, inventory, or internal system information. "
        "Do NOT modify or create orders. "
        "Payment methods accepted: Cash and GCash only — no credit/debit cards. "
        f"Takeout packaging fee: {fee} per eligible meal item. Drinks are not charged this fee. "
        "All prices are in Philippine Peso (₱). "
        "Keep responses under 200 words. Use plain text — no Markdown headers. "
        "Bullet points and bold are fine. "
    )

    if intent in ('menu', 'price'):
        menu_ctx = get_menu_context()
        base += (
            f"\n\nCURRENT MENU (authoritative — use ONLY this data for product/price questions):\n"
            f"{menu_ctx}\n"
            "Only recommend items listed above. Do not invent items."
        )

    return base


def get_ai_response(
    message: str,
    intent: str,
    history: list[dict],
) -> str:
    """
    Call Gemini API. Return the text response.
    Raises on failure — caller handles fallback.

    history: list of {'role': 'user'|'model', 'parts': [{'text': '...'}]}
    """
    api_key = _get_api_key()
    if not api_key:
        raise ValueError('GEMINI_API_KEY not configured')

    try:
        import google.generativeai as genai
    except ImportError:
        raise ImportError('google-generativeai not installed')

    genai.configure(api_key=api_key)

    system_prompt = _build_system_prompt(intent)

    model = genai.GenerativeModel(
        model_name='gemini-1.5-flash',  # free-tier friendly, fast
        system_instruction=system_prompt,
        generation_config={
            'max_output_tokens': 300,
            'temperature': 0.4,
        },
        safety_settings=[
            {'category': 'HARM_CATEGORY_HARASSMENT', 'threshold': 'BLOCK_MEDIUM_AND_ABOVE'},
            {'category': 'HARM_CATEGORY_HATE_SPEECH', 'threshold': 'BLOCK_MEDIUM_AND_ABOVE'},
            {'category': 'HARM_CATEGORY_SEXUALLY_EXPLICIT', 'threshold': 'BLOCK_MEDIUM_AND_ABOVE'},
            {'category': 'HARM_CATEGORY_DANGEROUS_CONTENT', 'threshold': 'BLOCK_MEDIUM_AND_ABOVE'},
        ],
    )

    # Build conversation history (capped to MAX_HISTORY_TURNS pairs)
    chat_history = history[-(MAX_HISTORY_TURNS * 2):]  # each turn = 2 entries
    chat = model.start_chat(history=chat_history)

    import signal

    # Timeout via signal (Unix only — Render is Linux so this works)
    def _timeout_handler(signum, frame):
        raise TimeoutError('Gemini API timed out')

    try:
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(AI_TIMEOUT_SECONDS)
        response = chat.send_message(message)
        signal.alarm(0)
    except TimeoutError:
        signal.alarm(0)
        raise
    except Exception:
        signal.alarm(0)
        raise

    text = response.text.strip()
    if not text:
        raise ValueError('Empty response from Gemini')
    return text


# ── Main entry point ──────────────────────────────────────────────────────────

def get_chatbot_response(
    message: str,
    history: list[dict],
) -> tuple[str, str]:
    """
    Main chatbot response function.

    Returns (response_text, intent) tuple.

    Flow:
    1. Detect intent (deterministic, instant)
    2. Handle order-status intent directly (no AI needed — DB lookup)
    3. Try AI response
    4. On any AI failure: return fallback response
    """
    intent = detect_intent(message)

    # Order status: handle deterministically — no AI call
    if intent == 'order_status':
        # Extract order number if present in the message
        import re
        match = re.search(r'KDM[-\s]?\d{8}[-\s]?\d{4}', message, re.IGNORECASE)
        if match:
            order_num = re.sub(r'[\s]', '-', match.group()).upper()
            result = lookup_order_status(order_num)
            if result.get('error'):
                return (
                    "Sorry, I couldn't look up that order right now. "
                    "Please try again or ask our staff for assistance.",
                    intent,
                )
            if not result['found']:
                return (
                    f"I couldn't find an order with number **{order_num}**. "
                    "Please double-check the order number (format: KDM-YYYYMMDD-XXXX). "
                    "You can find it on your order confirmation page.",
                    intent,
                )
            status = result['status_display']
            order_type = result['order_type']
            resp = f"Your order **{result['order_number']}** ({order_type}) is currently: **{status}**."
            if not result['is_final'] and result.get('queue_position'):
                pos = result['queue_position']
                if pos == 1:
                    resp += " You're next in the queue! 🎉"
                elif pos > 1:
                    resp += f" There are {pos - 1} order(s) ahead of you."
            if result['status'] == 'ready':
                resp += " Please proceed to the counter to pick up your order. ✅"
            return resp, intent
        else:
            return (
                "Sure! To check your order status, please provide your order number. "
                "It looks like **KDM-YYYYMMDD-XXXX** and can be found on your "
                "order confirmation page.",
                intent,
            )

    # Try AI for other intents
    try:
        response = get_ai_response(message, intent, history)
        return response, intent
    except ImportError:
        logger.warning('google-generativeai not installed — using fallback')
    except ValueError as e:
        if 'not configured' in str(e):
            logger.debug('Gemini API key not configured — using fallback')
        else:
            logger.warning('Gemini ValueError: %s', e)
    except TimeoutError:
        logger.warning('Gemini API timed out')
    except Exception as e:
        logger.exception('Gemini API error: %s', e)

    return fallback_response(intent, message), intent
