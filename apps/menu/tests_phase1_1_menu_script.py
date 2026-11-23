"""Phase 1.1 — customer menu inline availability script regression.

The Phase 1 browser verification found a duplicated closing brace in the
menu's inline availability script: the browser raised
``Unexpected token '}'`` and neither the per-size availability gating nor the
live ``product_stock`` polling ever ran. These tests lock the corrected
structure in place:

  * the menu renders,
  * every inline script parses (checked with ``node --check`` when available),
  * the size-selector and polling behaviour is still present,
  * the ``product_stock`` API contract is unchanged,
  * the server-side availability guard is still enforced (a broken client
    script must never become an ordering loophole).

All tests run on isolated test databases.
"""
import os
import re
import shutil
import subprocess
import tempfile

from django.test import TestCase
from django.urls import reverse

from apps.menu.models import Category, Product
from apps.orders.models import CartItem

INLINE_SCRIPT_RE = re.compile(
    r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', re.S | re.I)


def _uid():
    import time
    return str(time.monotonic_ns())


def _category(name=None):
    name = name or f'P11 Cat {_uid()}'
    return Category.objects.create(name=name, slug=name.lower().replace(' ', '-'))


def _ingredient_product(category, name=None):
    """Ingredient-tracked product with no recipe/packaging configuration.

    With no approved plan the product is not sellable, which lets the tests
    assert the server-side out-of-stock gate independently of the JS layer.
    """
    return Product.objects.create(
        category=category,
        name=name or f'P11 Ingredient {_uid()}',
        price='50.00',
        inventory_tracking_mode='ingredient',
    )


class CustomerMenuScriptTests(TestCase):
    def setUp(self):
        self.category = _category()
        self.product = _ingredient_product(self.category)
        self.response = self.client.get(reverse('menu:index'))

    def _inline_scripts(self):
        return INLINE_SCRIPT_RE.findall(self.response.content.decode('utf-8'))

    def test_menu_returns_200(self):
        self.assertEqual(self.response.status_code, 200)
        self.assertTemplateUsed(self.response, 'menu/index.html')

    def test_inline_scripts_are_syntactically_valid(self):
        scripts = self._inline_scripts()
        self.assertTrue(scripts, 'menu must render inline scripts')
        node = shutil.which('node')
        if not node:
            self.skipTest('node executable not available')
        for source in scripts:
            handle, path = tempfile.mkstemp(suffix='.js', text=True)
            os.close(handle)
            try:
                with open(path, 'w', encoding='utf-8') as fh:
                    fh.write(source)
                proc = subprocess.run(
                    [node, '--check', path],
                    capture_output=True, text=True, timeout=60,
                )
            finally:
                os.unlink(path)
            self.assertEqual(
                proc.returncode, 0,
                'inline menu script has a syntax error:\n' + proc.stderr,
            )

    def test_size_selector_and_availability_script_present(self):
        content = self.response.content.decode('utf-8')
        self.assertIn("document.querySelectorAll('.size-selector')", content)
        self.assertIn('kdmStockInfo', content)
        self.assertIn('size-availability-label', content)

    def test_live_stock_polling_preserved(self):
        content = self.response.content.decode('utf-8')
        self.assertIn(reverse('menu:product_stock'), content)
        self.assertIn('setInterval(refresh', content)
        self.assertIn("headers['If-None-Match'] = lastEtag", content)
        self.assertIn('applyStock', content)

    def test_product_stock_api_contract_unchanged(self):
        response = self.client.get(reverse('menu:product_stock'))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        entry = payload['products'][str(self.product.pk)]
        # Legacy fields preserved verbatim.
        for field in ('stock_quantity', 'is_available', 'is_active'):
            self.assertIn(field, entry)
        # Phase 8B additive fields the polling script depends on.
        self.assertEqual(entry['inventory_tracking_mode'], 'ingredient')
        self.assertTrue(entry['out_of_stock'])
        self.assertFalse(entry['sellable'])
        self.assertIn('sizes', entry)

    def test_server_side_out_of_stock_gate_preserved(self):
        content = self.response.content.decode('utf-8')
        self.assertIn(self.product.name, content)
        self.assertIn('out-of-stock-label', content)
        # No add-to-cart form is rendered for an unsellable product.
        self.assertNotIn(
            reverse('orders:add_to_cart', args=[self.product.pk]), content)

    def test_add_to_cart_still_rejects_unsellable_ingredient_product(self):
        response = self.client.post(
            reverse('orders:add_to_cart', args=[self.product.pk]),
            {'quantity': 1, 'size': 'none'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CartItem.objects.count(), 0)
