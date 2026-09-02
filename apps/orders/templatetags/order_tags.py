"""
Custom template tags for the orders app.
"""
from django import template
from apps.orders.services import VALID_TRANSITIONS
from apps.orders.models import Order

register = template.Library()


@register.inclusion_tag('orders/partials/status_select.html')
def status_select(order, form_classes='', select_style=''):
    """Render a status <select> that only shows valid next transitions.

    The current status is always included as the pre-selected option so the
    cashier can see where the order is right now.  Only the transitions that
    are valid from the current status are offered as alternatives, so the
    dropdown itself communicates what is allowed.

    For terminal states (completed, cancelled) the select is rendered as
    disabled with only the current status visible — the "Update Status" button
    is also hidden so there is no ambiguity about whether a submission will do
    anything.

    Usage in templates:
        {% load order_tags %}
        {% status_select order %}
        {% status_select order form_classes="form-select" select_style="font-size:0.8rem" %}
    """
    status_label = dict(Order.STATUS_CHOICES)
    allowed_next = VALID_TRANSITIONS.get(order.status, set())

    # Build the option list: current status first (selected), then each valid
    # next state in the canonical STATUS_CHOICES order so the order is stable.
    options = [{'value': order.status, 'label': status_label[order.status], 'selected': True}]
    for value, label in Order.STATUS_CHOICES:
        if value in allowed_next:
            options.append({'value': value, 'label': label, 'selected': False})

    is_terminal = not allowed_next  # completed or cancelled

    return {
        'order':        order,
        'options':      options,
        'is_terminal':  is_terminal,
        'form_classes': form_classes,
        'select_style': select_style,
    }
