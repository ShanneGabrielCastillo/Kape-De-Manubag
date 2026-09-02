"""
Product image helpers — resolve a product image URL to either the uploaded
image or the shared placeholder, so no surface ever renders a broken icon.

The placeholder is used whenever the image field is empty, the underlying
file is missing from storage, or storage itself raises (e.g. a temporarily
unavailable remote store). ``product_image`` also emits an ``onerror``
fallback so a stale/missing file can never leave a broken-image icon.
"""
from django import template
from django.templatetags.static import static

register = template.Library()

PLACEHOLDER_PATH = 'images/placeholder.svg'


def resolve_product_image_url(product):
    """Image URL for ``product``, or the shared placeholder when unavailable."""
    if product is not None and product.image:
        try:
            if product.image.storage.exists(product.image.name):
                return product.image.url
        except Exception:
            # Never let a storage failure break the page — fall back to the
            # placeholder instead of a broken image.
            pass
    return static(PLACEHOLDER_PATH)


@register.inclusion_tag('partials/product_image.html')
def product_image(product, alt=''):
    """Render a product <img> with a consistent placeholder fallback.

    The surrounding container controls the display size; ``object-fit:
    cover`` (see .product-image in main.css) keeps the aspect ratio of the
    source image without distortion.
    """
    return {
        'image_url': resolve_product_image_url(product),
        'placeholder_url': static(PLACEHOLDER_PATH),
        'alt': alt,
    }
