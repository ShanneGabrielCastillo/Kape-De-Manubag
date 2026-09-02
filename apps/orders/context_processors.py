from .models import Cart


def cart_count(request):
    """Add cart item count to all templates.

    The session cart is a customer feature (menu -> cart -> checkout); the
    staff templates (``base_admin.html``) never render the cart badge, so the
    database lookup is skipped for admin/cashier users. This removes one query
    from every staff page load (e.g. the dashboard) without changing what any
    staff page displays.
    """
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated \
            and (user.is_admin_user or user.is_cashier):
        return {'cart_count': 0}

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
