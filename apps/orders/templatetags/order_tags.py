"""
Custom template tags for the orders app.
"""
from django import template
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe
from apps.orders.services import VALID_TRANSITIONS
from apps.orders.models import Order

register = template.Library()


def _render_status_select(order, form_classes='', select_style=''):
    """Core logic shared by the template tag — separated so it can be called
    directly from Python and tested without a template context."""
    status_label = dict(Order.STATUS_CHOICES)
    allowed_next = VALID_TRANSITIONS.get(order.status, set())

    options = [{'value': order.status, 'label': status_label[order.status], 'selected': True}]
    for value, label in Order.STATUS_CHOICES:
        if value in allowed_next:
            options.append({'value': value, 'label': label, 'selected': False})

    return mark_safe(render_to_string('orders/partials/status_select.html', {
        'order':        order,
        'options':      options,
        'is_terminal':  not allowed_next,
        'form_classes': form_classes,
        'select_style': select_style,
    }))


# Register as a plain function tag using register.tag() so the argument
# is passed as a FilterExpression resolved at render time, not parsed at
# compile time.  This avoids the Django 5.1+ change that broke
# @inclusion_tag/@simple_tag with positional object arguments.
def _status_select_tag(parser, token):
    bits = token.split_contents()
    tag_name = bits[0]
    if len(bits) < 2:
        raise template.TemplateSyntaxError(
            f"'{tag_name}' requires at least one argument (the order object)."
        )
    order_expr       = parser.compile_filter(bits[1])
    form_classes_val = ''
    select_style_val = ''
    for bit in bits[2:]:
        if bit.startswith('form_classes='):
            form_classes_val = bit.split('=', 1)[1].strip('"\'')
        elif bit.startswith('select_style='):
            select_style_val = bit.split('=', 1)[1].strip('"\'')
    return _StatusSelectNode(order_expr, form_classes_val, select_style_val)


class _StatusSelectNode(template.Node):
    def __init__(self, order_expr, form_classes, select_style):
        self.order_expr   = order_expr
        self.form_classes = form_classes
        self.select_style = select_style

    def render(self, context):
        order = self.order_expr.resolve(context)
        return _render_status_select(order, self.form_classes, self.select_style)


register.tag('status_select', _status_select_tag)
