"""
Product image helpers — resolve a product image URL to either the uploaded
image or the shared placeholder, so no surface ever renders a broken icon.

The placeholder is used whenever the image field is empty, the underlying
file is missing from storage, or storage itself raises (e.g. a temporarily
unavailable remote store). ``product_image`` also emits an ``onerror``
fallback so a stale/missing file can never leave a broken-image icon.
"""
from django import template
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.utils.safestring import mark_safe

register = template.Library()

PLACEHOLDER_PATH = 'images/placeholder.svg'


def resolve_product_image_url(product):
    """Image URL for ``product``, or the shared placeholder when unavailable."""
    if product is not None and product.image:
        try:
            if product.image.storage.exists(product.image.name):
                return product.image.url
        except Exception:
            pass
    return static(PLACEHOLDER_PATH)


@register.simple_tag
def product_image(product, alt=''):
    """Render a product <img> with a consistent placeholder fallback.

    Implemented as simple_tag for Django 5.1+ compatibility — inclusion_tag
    with positional arguments raises TemplateSyntaxError in Django 5.1+.
    """
    context = {
        'image_url':       resolve_product_image_url(product),
        'placeholder_url': static(PLACEHOLDER_PATH),
        'alt':             alt,
    }
    return mark_safe(render_to_string('partials/product_image.html', context))
