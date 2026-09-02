"""
Custom template tags for the orders app.

status_select is implemented using register.tag() with a custom Node
rather than @inclusion_tag or @simple_tag because Django 5.1+ changed
how those decorators validate positional arguments at template compile
time, raising TemplateSyntaxError before the tag even runs.

The custom Node resolves the order expression at render time (not parse
time), which works identically across Django 4.2, 5.x, and 6.x.
"""
from django import template
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe
from apps.orders.services import VALID_TRANSITIONS
from apps.orders.models import Order

register = template.Library()


def _render_status_select(order, form_classes='', select_style=''):
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


class _StatusSelectNode(template.Node):
    def __init__(self, order_var, kwargs):
        self.order_var = order_var
        self.kwargs    = kwargs   # dict of {key: string_value}

    def render(self, context):
        try:
            order = self.order_var.resolve(context)
        except template.VariableDoesNotExist:
            return ''
        return _render_status_select(
            order,
            form_classes=self.kwargs.get('form_classes', ''),
            select_style=self.kwargs.get('select_style', ''),
        )


def do_status_select(parser, token):
    """
    Usage:
        {% status_select order %}
        {% status_select order form_classes="cls" select_style="s" %}
    """
    bits = token.split_contents()

    # bits[0] is the tag name itself ('status_select')
    # bits[1] should be the order variable
    # bits[2+] are optional key=value pairs
    if len(bits) < 2:
        raise template.TemplateSyntaxError(
            f"'{bits[0]}' requires at least one argument: the order variable. "
            f"(Received bits: {bits!r})"
        )

    order_var = parser.compile_filter(bits[1])

    kwargs = {}
    for bit in bits[2:]:
        if '=' not in bit:
            # Skip tokens that don't look like key=value (defensive)
            continue
        key, _, val = bit.partition('=')
        kwargs[key.strip()] = val.strip().strip('"\'')

    return _StatusSelectNode(order_var, kwargs)


register.tag('status_select', do_status_select)
