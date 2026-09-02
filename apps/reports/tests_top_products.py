"""
Top Products verification tests — Kape De Manubag Sales Reports.

Verifies every aspect of the Top Products calculation:
  - Ranked by revenue (highest first)  ← existing intended behaviour
  - Secondary alphabetical sort for deterministic tie-breaking (new fix)
  - Grouped by product_name snapshot (not the live product FK)
  - Only OrderItems from completed+paid orders are included
  - Cancelled order items are excluded
  - Quantities and revenues aggregated correctly across multiple orders
  - Top-10 limit enforced
  - Historical product names preserved even after deactivation

Scenarios:
  1.  One product, one order
  2.  Multiple products — revenue ranking verified
  3.  Same product across multiple orders — totals aggregated
  4.  Different quantities — qty and revenue both correct
  5.  Cancelled orders excluded from Top Products
  6.  Pending / preparing / ready excluded
  7.  Products with identical revenue — alphabetical tie-break
  8.  Zero-sales period — empty queryset handled correctly
  9.  Product name snapshot preserved after product deactivated
  10. Revenue uses OrderItem.subtotal (price at order time)
  11. Top-10 limit: >10 products — only top 10 returned
  12. Same product, multiple orders — aggregated as one entry
"""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.menu.models import Category, Product
from apps.orders.models import Order, OrderItem

User = get_user_model()

TODAY = timezone.localdate()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user(username, role='admin'):
    u = User.objects.create_user(username=username, password='pass123')
    u.role = role
    u.save()
    return u


def _category(name='Coffee'):
    return Category.objects.create(name=name, slug=name.lower().replace(' ', '-'))


def _product(name, price=Decimal('100.00'), category=None):
    if category is None:
        category = _category()
    return Product.objects.create(
        category=category, name=name, price=price, stock_quantity=100,
    )


def _completed_order(date=TODAY):
    """Create a bare completed+paid order shell."""
    dt = timezone.make_aware(
        datetime.datetime.combine(date, datetime.time(10, 0))
    )
    o = Order.objects.create(
        customer_name='Test',
        status='completed', is_paid=True,
        payment_method='cash',
        total=Decimal('0.00'), subtotal=Decimal('0.00'),
    )
    Order.objects.filter(pk=o.pk).update(created_at=dt)
    o.refresh_from_db()
    return o


def _add_item(order, product, quantity=1, unit_price=None, product_name=None):
    """Add an OrderItem to an order."""
    price    = unit_price or product.price
    subtotal = price * quantity
    item = OrderItem.objects.create(
        order=order,
        product=product,
        product_name=product_name or product.name,
        size='none',
        quantity=quantity,
        unit_price=price,
        subtotal=subtotal,
    )
    # Update order totals
    order.total    += subtotal
    order.subtotal += subtotal
    order.save(update_fields=['total', 'subtotal'])
    return item


def _cancelled_order(date=TODAY):
    dt = timezone.make_aware(
        datetime.datetime.combine(date, datetime.time(11, 0))
    )
    o = Order.objects.create(
        customer_name='Cancelled',
        status='cancelled', is_paid=False,
        payment_method='cash',
        total=Decimal('0.00'), subtotal=Decimal('0.00'),
    )
    Order.objects.filter(pk=o.pk).update(created_at=dt)
    o.refresh_from_db()
    return o


def _reports(client, start=None, end=None):
    return client.get(
        reverse('reports:index'),
        {'start': str(start or TODAY), 'end': str(end or TODAY)},
    )


def _top(resp):
    """Return top_products from response context as a plain list."""
    return list(resp.context['top_products'])


# ── 1. One product, one order ─────────────────────────────────────────────────

class OneProductOneOrderTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_one')
        self.client = Client()
        self.client.login(username='admin_one', password='pass123')
        cat  = _category()
        prod = _product('Latte', Decimal('120.00'), cat)
        o    = _completed_order()
        _add_item(o, prod, quantity=2)

    def test_single_product_appears(self):
        top = _top(_reports(self.client))
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]['product_name'], 'Latte')

    def test_qty_correct(self):
        top = _top(_reports(self.client))
        self.assertEqual(top[0]['total_qty'], 2)

    def test_revenue_correct(self):
        # 2 × 120 = 240
        top = _top(_reports(self.client))
        self.assertEqual(top[0]['total_revenue'], Decimal('240.00'))

    def test_report_total_revenue_equals_item_subtotal(self):
        resp = _reports(self.client)
        top  = _top(resp)
        self.assertEqual(
            resp.context['total_revenue'],
            top[0]['total_revenue'],
        )


# ── 2. Multiple products — revenue ranking ────────────────────────────────────

class MultipleProductsRankingTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_ranking')
        self.client = Client()
        self.client.login(username='admin_ranking', password='pass123')
        cat = _category()
        # Deliberately out-of-order so ranking is what matters
        prod_a = _product('Americano', Decimal('100.00'), cat)
        prod_b = _product('Cappuccino', Decimal('200.00'), cat)
        prod_c = _product('Espresso',   Decimal('80.00'),  cat)
        o = _completed_order()
        _add_item(o, prod_a, quantity=1)   # 100
        _add_item(o, prod_b, quantity=1)   # 200
        _add_item(o, prod_c, quantity=1)   # 80

    def test_ranked_by_revenue_descending(self):
        top = _top(_reports(self.client))
        self.assertEqual(top[0]['product_name'], 'Cappuccino')   # 200
        self.assertEqual(top[1]['product_name'], 'Americano')    # 100
        self.assertEqual(top[2]['product_name'], 'Espresso')     # 80

    def test_all_three_present(self):
        top = _top(_reports(self.client))
        self.assertEqual(len(top), 3)

    def test_revenue_values_correct(self):
        top = _top(_reports(self.client))
        revenues = {p['product_name']: p['total_revenue'] for p in top}
        self.assertEqual(revenues['Cappuccino'], Decimal('200.00'))
        self.assertEqual(revenues['Americano'],  Decimal('100.00'))
        self.assertEqual(revenues['Espresso'],   Decimal('80.00'))


# ── 3. Same product across multiple orders — totals aggregated ────────────────

class SameProductMultipleOrdersTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_multi_order')
        self.client = Client()
        self.client.login(username='admin_multi_order', password='pass123')
        cat  = _category()
        prod = _product('Latte', Decimal('120.00'), cat)
        # Three separate orders, each with 2 lattes
        for _ in range(3):
            o = _completed_order()
            _add_item(o, prod, quantity=2)

    def test_aggregated_as_single_entry(self):
        top = _top(_reports(self.client))
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]['product_name'], 'Latte')

    def test_qty_summed_across_orders(self):
        # 3 orders × 2 qty = 6
        top = _top(_reports(self.client))
        self.assertEqual(top[0]['total_qty'], 6)

    def test_revenue_summed_across_orders(self):
        # 3 × 2 × 120 = 720
        top = _top(_reports(self.client))
        self.assertEqual(top[0]['total_revenue'], Decimal('720.00'))


# ── 4. Different quantities ───────────────────────────────────────────────────

class DifferentQuantitiesTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_qty')
        self.client = Client()
        self.client.login(username='admin_qty', password='pass123')
        cat  = _category()
        prod = _product('Latte', Decimal('100.00'), cat)
        o    = _completed_order()
        _add_item(o, prod, quantity=5)

    def test_qty_correct(self):
        top = _top(_reports(self.client))
        self.assertEqual(top[0]['total_qty'], 5)

    def test_revenue_correct(self):
        # 5 × 100 = 500
        top = _top(_reports(self.client))
        self.assertEqual(top[0]['total_revenue'], Decimal('500.00'))

    def test_mixed_quantities_across_orders(self):
        """qty=3 in one order + qty=7 in another = total qty 8."""
        cat  = _category('Tea')
        prod = _product('Green Tea', Decimal('50.00'), cat)
        o1   = _completed_order()
        o2   = _completed_order()
        _add_item(o1, prod, quantity=3)
        _add_item(o2, prod, quantity=7)
        top   = _top(_reports(self.client))
        tea   = next(p for p in top if p['product_name'] == 'Green Tea')
        self.assertEqual(tea['total_qty'], 10)
        self.assertEqual(tea['total_revenue'], Decimal('500.00'))  # 10 × 50


# ── 5. Cancelled orders excluded ─────────────────────────────────────────────

class CancelledOrdersExcludedTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_cancel_top')
        self.client = Client()
        self.client.login(username='admin_cancel_top', password='pass123')
        cat  = _category()
        prod = _product('Latte', Decimal('120.00'), cat)
        # Completed order: should appear
        o_good = _completed_order()
        _add_item(o_good, prod, quantity=1)
        # Cancelled order: must NOT appear
        o_bad = _cancelled_order()
        _add_item(o_bad, prod, quantity=10)

    def test_cancelled_items_excluded(self):
        top = _top(_reports(self.client))
        self.assertEqual(len(top), 1)
        # Only the 1-unit completed order counts
        self.assertEqual(top[0]['total_qty'], 1)
        self.assertEqual(top[0]['total_revenue'], Decimal('120.00'))

    def test_cancelled_does_not_inflate_revenue(self):
        top = _top(_reports(self.client))
        self.assertNotEqual(top[0]['total_qty'], 11)  # would be 11 if cancelled counted
        self.assertNotEqual(top[0]['total_revenue'], Decimal('1320.00'))


# ── 6. Non-completed statuses excluded ───────────────────────────────────────

class NonCompletedExcludedTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_noncomplete')
        self.client = Client()
        self.client.login(username='admin_noncomplete', password='pass123')
        cat  = _category()
        prod = _product('Americano', Decimal('90.00'), cat)
        # One valid completed order
        o_good = _completed_order()
        _add_item(o_good, prod, quantity=1)
        # Non-completed statuses
        for status in ['pending', 'preparing', 'ready']:
            dt = timezone.make_aware(
                datetime.datetime.combine(TODAY, datetime.time(9, 0))
            )
            o = Order.objects.create(
                customer_name='Test', status=status, is_paid=False,
                payment_method='cash', total=Decimal('0.00'), subtotal=Decimal('0.00'),
            )
            Order.objects.filter(pk=o.pk).update(created_at=dt)
            o.refresh_from_db()
            _add_item(o, prod, quantity=100)

    def test_only_completed_counted(self):
        top = _top(_reports(self.client))
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]['total_qty'], 1)
        self.assertEqual(top[0]['total_revenue'], Decimal('90.00'))


# ── 7. Tie-breaking: same revenue → alphabetical ──────────────────────────────

class TieBreakingTest(TestCase):
    """
    Two products with identical total_revenue must appear in alphabetical
    order by product_name (the secondary sort added as FIX-1).
    """

    def setUp(self):
        self.admin = _user('admin_tie')
        self.client = Client()
        self.client.login(username='admin_tie', password='pass123')
        cat    = _category()
        prod_z = _product('Zucchini Cake',  Decimal('100.00'), cat)
        prod_a = _product('Apple Pie',      Decimal('100.00'), cat)
        prod_m = _product('Mango Smoothie', Decimal('100.00'), cat)
        o = _completed_order()
        _add_item(o, prod_z, quantity=1)   # 100 each — all tied
        _add_item(o, prod_a, quantity=1)
        _add_item(o, prod_m, quantity=1)

    def test_tied_revenue_sorted_alphabetically(self):
        top   = _top(_reports(self.client))
        names = [p['product_name'] for p in top]
        self.assertEqual(names, sorted(names),
            f"Tied products not in alphabetical order: {names}")

    def test_first_is_apple_pie(self):
        top = _top(_reports(self.client))
        self.assertEqual(top[0]['product_name'], 'Apple Pie')

    def test_last_is_zucchini_cake(self):
        top = _top(_reports(self.client))
        self.assertEqual(top[-1]['product_name'], 'Zucchini Cake')


# ── 8. Zero-sales period ──────────────────────────────────────────────────────

class ZeroSalesTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_zero_top')
        self.client = Client()
        self.client.login(username='admin_zero_top', password='pass123')

    def test_empty_top_products_when_no_sales(self):
        top = _top(_reports(self.client))
        self.assertEqual(len(top), 0)

    def test_page_still_renders(self):
        resp = _reports(self.client)
        self.assertEqual(resp.status_code, 200)


# ── 9. Product name snapshot preserved ───────────────────────────────────────

class ProductNameSnapshotTest(TestCase):
    """
    OrderItem.product_name is captured at order time.
    Even if the product is later deactivated or its FK nulled,
    the Top Products report must still show the original name.
    """

    def setUp(self):
        self.admin = _user('admin_snapshot')
        self.client = Client()
        self.client.login(username='admin_snapshot', password='pass123')
        cat  = _category()
        prod = _product('Classic Latte', Decimal('130.00'), cat)
        o    = _completed_order()
        # Store snapshot name explicitly
        _add_item(o, prod, quantity=2, product_name='Classic Latte')
        # Deactivate the product after the order
        prod.deactivate()

    def test_deactivated_product_still_in_top_products(self):
        top = _top(_reports(self.client))
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]['product_name'], 'Classic Latte')

    def test_revenue_preserved_after_deactivation(self):
        top = _top(_reports(self.client))
        self.assertEqual(top[0]['total_revenue'], Decimal('260.00'))  # 2 × 130

    def test_snapshot_survives_product_name_change(self):
        """
        Simulate a name change: new orders use the new name, old orders keep
        the snapshot. Both appear as separate entries.
        """
        cat  = Category.objects.first()
        prod = _product('New Name Latte', Decimal('130.00'), cat)
        o    = _completed_order()
        _add_item(o, prod, quantity=1, product_name='New Name Latte')

        top   = _top(_reports(self.client))
        names = {p['product_name'] for p in top}
        self.assertIn('Classic Latte',  names)
        self.assertIn('New Name Latte', names)
        self.assertEqual(len(top), 2)


# ── 10. Revenue uses stored subtotal (price at order time) ────────────────────

class RevenueUsesStoredPriceTest(TestCase):
    """
    OrderItem.subtotal = unit_price × quantity (captured at order time).
    Changing the product price after the order must NOT affect the report.
    """

    def setUp(self):
        self.admin = _user('admin_price')
        self.client = Client()
        self.client.login(username='admin_price', password='pass123')
        cat      = _category()
        self.prod = _product('Mocha', Decimal('150.00'), cat)
        o        = _completed_order()
        _add_item(o, self.prod, quantity=2, unit_price=Decimal('150.00'))

    def test_revenue_correct_before_price_change(self):
        top = _top(_reports(self.client))
        self.assertEqual(top[0]['total_revenue'], Decimal('300.00'))

    def test_revenue_unchanged_after_price_change(self):
        """Changing the live product price must not alter historical revenue."""
        self.prod.price = Decimal('999.00')
        self.prod.save(update_fields=['price'])
        top = _top(_reports(self.client))
        # Still 2 × 150 = 300 from the stored subtotal snapshot
        self.assertEqual(top[0]['total_revenue'], Decimal('300.00'))


# ── 11. Top-10 limit ──────────────────────────────────────────────────────────

class TopTenLimitTest(TestCase):
    """More than 10 distinct products — only the top 10 by revenue returned."""

    def setUp(self):
        self.admin = _user('admin_top10')
        self.client = Client()
        self.client.login(username='admin_top10', password='pass123')
        cat = _category()
        o   = _completed_order()
        # Create 12 products with descending revenue (100, 95, 90, ... 45)
        for i in range(12):
            prod = _product(f'Product {i:02d}', Decimal('100.00'), cat)
            qty  = 12 - i   # Product 00 has qty=12 (highest), Product 11 has qty=1
            _add_item(o, prod, quantity=qty)

    def test_only_ten_returned(self):
        top = _top(_reports(self.client))
        self.assertEqual(len(top), 10)

    def test_top_ten_are_highest_revenue(self):
        """The 10 products returned must all have higher revenue than the 11th."""
        top = _top(_reports(self.client))
        revenues = [p['total_revenue'] for p in top]
        # All revenue values must be >= the 11th-highest revenue
        # Product 10 has qty=2 → revenue=200; Product 11 has qty=1 → revenue=100
        self.assertGreaterEqual(min(revenues), Decimal('200.00'))

    def test_ranked_by_revenue_descending(self):
        top      = _top(_reports(self.client))
        revenues = [p['total_revenue'] for p in top]
        self.assertEqual(revenues, sorted(revenues, reverse=True))


# ── 12. Aggregation: same product different sizes ─────────────────────────────

class SameProductDifferentNamesTest(TestCase):
    """
    'Latte' and 'Latte (Large)' are different product_name values.
    They appear as separate entries in Top Products.
    """

    def setUp(self):
        self.admin = _user('admin_names')
        self.client = Client()
        self.client.login(username='admin_names', password='pass123')
        cat   = _category()
        prod  = _product('Latte', Decimal('120.00'), cat)
        o     = _completed_order()
        # Same product, different name snapshots (simulating size variants
        # stored with different product_name labels in historical orders)
        _add_item(o, prod, quantity=2, product_name='Latte')
        # A second order with a slightly different name snapshot
        o2 = _completed_order()
        _add_item(o2, prod, quantity=1, product_name='Latte (Large)')

    def test_two_separate_entries(self):
        """Different product_name values → two rows in Top Products."""
        top   = _top(_reports(self.client))
        names = {p['product_name'] for p in top}
        self.assertIn('Latte',         names)
        self.assertIn('Latte (Large)', names)

    def test_each_entry_correct_qty(self):
        top = _top(_reports(self.client))
        by_name = {p['product_name']: p for p in top}
        self.assertEqual(by_name['Latte']['total_qty'],         2)
        self.assertEqual(by_name['Latte (Large)']['total_qty'], 1)
