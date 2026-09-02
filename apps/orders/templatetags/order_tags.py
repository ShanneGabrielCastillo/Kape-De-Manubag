"""
Custom template tags for the orders app.
"""
from django import template
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe
from apps.orders.services import VALID_TRANSITIONS
from apps.orders.models import Order

register = template.Library()


@register.simple_tag
def status_select(order, form_classes='', select_style=''):
    """Render a status <select> that only shows valid next transitions.

    Implemented as a simple_tag (returning a rendered string) rather than
    inclusion_tag because Django 5.1+ changed how inclusion_tag validates
    positional arguments at template compile time, breaking the old signature.
    simple_tag works identically across Django 4.2, 5.x, and 6.x.

    Usage in templates:
        {% load order_tags %}
        {% status_select order %}
        {% status_select order form_classes="form-select" select_style="font-size:0.8rem" %}
    """
    status_label = dict(Order.STATUS_CHOICES)
    allowed_next = VALID_TRANSITIONS.get(order.status, set())

    options = [{'value': order.status, 'label': status_label[order.status], 'selected': True}]
    for value, label in Order.STATUS_CHOICES:
        if value in allowed_next:
            options.append({'value': value, 'label': label, 'selected': False})

    is_terminal = not allowed_next

    context = {
        'order':        order,
        'options':      options,
        'is_terminal':  is_terminal,
        'form_classes': form_classes,
        'select_style': select_style,
    }
    return mark_safe(render_to_string('orders/partials/status_select.html', context))
