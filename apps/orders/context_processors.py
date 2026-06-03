from .models import Cart


def cart_count(request):
    """Add cart item count to all templates"""
    count = 0
    if hasattr(request, 'session'):
        session_key = request.session.session_key
        if session_key:
            try:
                cart = Cart.objects.get(session_key=session_key)
                count = cart.item_count
            except Cart.DoesNotExist:
                pass
    return {'cart_count': count}
