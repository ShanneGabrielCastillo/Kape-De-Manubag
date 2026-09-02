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
    Parse {% status_select order %} or
          {% status_select order select_style="..." form_classes="..." %}

    Uses token.contents (the raw text after the tag name) instead of
    split_contents() because Django 5.2 changed how split_contents()
    tokenizes tags with keyword arguments containing spaces inside quotes.
    """
    import shlex

    # token.contents is everything after {% and before %}, e.g.:
    # "status_select order select_style=\"font-size:0.8rem\""
    contents = token.contents.strip()

    # Use shlex to split respecting quoted strings
    try:
        parts = shlex.split(contents)
    except ValueError:
        parts = contents.split()

    # parts[0] is the tag name, parts[1] is the order var, parts[2+] are kwargs
    if len(parts) < 2:
        raise template.TemplateSyntaxError(
            f"'{parts[0]}' requires at least one argument: the order variable."
        )

    order_var = parser.compile_filter(parts[1])

    kwargs = {}
    for part in parts[2:]:
        if '=' in part:
            key, _, val = part.partition('=')
            kwargs[key.strip()] = val.strip().strip('"\'')

    return _StatusSelectNode(order_var, kwargs)


register.tag('status_select', do_status_select)
