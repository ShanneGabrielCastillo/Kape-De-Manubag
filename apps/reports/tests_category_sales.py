"""
Sales by Category verification tests — Kape De Manubag Sales Reports.

Verifies every aspect of the Sales by Category calculation after the
fix that switches from a live FK join to the category_name snapshot:

  BEFORE (buggy):
    .values('product__category__name')   ← live join — wrong after changes
  AFTER (fixed):
    .values('category_name')             ← stored snapshot — always correct

Key invariants:
  - Revenue comes from OrderItem.subtotal (price × qty at order time)
  - Grouping uses category_name snapshot (not the live product FK chain)
  - Only completed+paid orders included (same filter as rest of Reports)
  - Revenue ranked descending within category_sales
  - Blank category_name items are excluded cleanly

Scenarios:
  1.  One category, one product
  2.  One category, multiple products
  3.  Multiple categories — aggregated separately
  4.  Products across different categories
  5.  Cancelled orders excluded
  6.  Non-completed statuses excluded
  7.  Snapshot preserved after product deactivated (FK → NULL)
  8.  Snapshot preserved after category change (historical accuracy fix)
  9.  No sales — empty category_sales
  10. Revenue ranked descending
  11. Quantity aggregated correctly
  12. Revenue uses stored subtotal (price at order time)
  13. Zero-sales category absent (not shown with 0)
  14. Blank category_name excluded cleanly
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


def _category(name):
    return Category.objects.create(name=name, slug=name.lower().replace(' ', '-'))


def _product(name, price=Decimal('100.00'), category=None):
    if category is None:
        category = _category('Default')
    return Product.objects.create(
        category=category, name=name, price=price, stock_quantity=100,
    )


def _completed_order(date=TODAY):
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


def _add_item(order, product, quantity=1, unit_price=None,
              category_name=None):
    """
    Add an OrderItem with explicit category_name snapshot.
    If category_name is None, uses the product's current category name
    (matching the real save() behaviour).
    When a specific category_name is given (including ''), it is set via
    update() after creation to bypass the auto-populate in save() — this
    lets tests exercise the blank-category exclusion path.
    """
    price    = unit_price or product.price
    subtotal = price * quantity
    cat_name = category_name if category_name is not None else product.category.name
    item = OrderItem.objects.create(
        order=order,
        product=product,
        product_name=product.name,
        category_name=cat_name,   # initial value (may be overwritten by save)
        size='none',
        quantity=quantity,
        unit_price=price,
        subtotal=subtotal,
    )
    # When caller explicitly passes category_name (including '') use update()
    # to bypass the auto-populate in OrderItem.save() which would overwrite
    # a blank value with the product's current category name.
    if category_name is not None:
        OrderItem.objects.filter(pk=item.pk).update(category_name=cat_name)
        item.refresh_from_db()
    order.total    += subtotal
    order.subtotal += subtotal
    order.save(update_fields=['total', 'subtotal'])
    return item


def _reports(client, start=None, end=None):
    return client.get(
        reverse('reports:index'),
        {'start': str(start or TODAY), 'end': str(end or TODAY)},
    )


def _cat_sales(resp):
    """Return category_sales from context as a list of dicts."""
    return list(resp.context['category_sales'])


def _cat(cat_sales, name):
    """Return the category_sales entry for the given name, or None."""
    return next((c for c in cat_sales if c['name'] == name), None)


# ── 1. One category, one product ──────────────────────────────────────────────

class OneCategoryOneProductTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_cat1')
        self.client = Client()
        self.client.login(username='admin_cat1', password='pass123')
        cat  = _category('Coffee')
        prod = _product('Latte', Decimal('120.00'), cat)
        o    = _completed_order()
        _add_item(o, prod, quantity=2)   # 2 × 120 = 240

    def test_category_appears(self):
        cats = _cat_sales(_reports(self.client))
        self.assertEqual(len(cats), 1)
        self.assertEqual(cats[0]['name'], 'Coffee')

    def test_revenue_correct(self):
        cats = _cat_sales(_reports(self.client))
        self.assertAlmostEqual(cats[0]['total'], 240.0, places=2)

    def test_qty_correct(self):
        cats = _cat_sales(_reports(self.client))
        self.assertEqual(cats[0]['qty'], 2)


# ── 2. One category, multiple products ───────────────────────────────────────

class OneCategoryMultipleProductsTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_cat2')
        self.client = Client()
        self.client.login(username='admin_cat2', password='pass123')
        cat   = _category('Coffee')
        prod1 = _product('Latte',      Decimal('120.00'), cat)
        prod2 = _product('Americano',  Decimal('100.00'), cat)
        prod3 = _product('Cappuccino', Decimal('90.00'),  cat)
        o = _completed_order()
        _add_item(o, prod1, quantity=1)   # 120
        _add_item(o, prod2, quantity=2)   # 200
        _add_item(o, prod3, quantity=3)   # 270

    def test_one_entry_for_the_category(self):
        cats = _cat_sales(_reports(self.client))
        self.assertEqual(len(cats), 1)

    def test_revenue_aggregated(self):
        # 120 + 200 + 270 = 590
        cats = _cat_sales(_reports(self.client))
        self.assertAlmostEqual(cats[0]['total'], 590.0, places=2)

    def test_qty_aggregated(self):
        # 1 + 2 + 3 = 6
        cats = _cat_sales(_reports(self.client))
        self.assertEqual(cats[0]['qty'], 6)


# ── 3. Multiple categories — aggregated separately ────────────────────────────

class MultipleCategoriesTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_cat3')
        self.client = Client()
        self.client.login(username='admin_cat3', password='pass123')
        coffee_cat = _category('Coffee')
        food_cat   = _category('Food')
        latte      = _product('Latte',  Decimal('120.00'), coffee_cat)
        cake       = _product('Cake',   Decimal('80.00'),  food_cat)
        o = _completed_order()
        _add_item(o, latte, quantity=2)   # Coffee: 240
        _add_item(o, cake,  quantity=3)   # Food: 240

    def test_two_categories(self):
        cats = _cat_sales(_reports(self.client))
        names = {c['name'] for c in cats}
        self.assertIn('Coffee', names)
        self.assertIn('Food',   names)

    def test_revenue_per_category_correct(self):
        cats = _cat_sales(_reports(self.client))
        by_name = {c['name']: c for c in cats}
        self.assertAlmostEqual(by_name['Coffee']['total'], 240.0, places=2)
        self.assertAlmostEqual(by_name['Food']['total'],   240.0, places=2)

    def test_qty_per_category_correct(self):
        cats = _cat_sales(_reports(self.client))
        by_name = {c['name']: c for c in cats}
        self.assertEqual(by_name['Coffee']['qty'], 2)
        self.assertEqual(by_name['Food']['qty'],   3)


# ── 4. Revenue ranked descending ─────────────────────────────────────────────

class RevenueSortingTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_cat_sort')
        self.client = Client()
        self.client.login(username='admin_cat_sort', password='pass123')
        cat_a = _category('Coffee')    # will have lower revenue
        cat_b = _category('Food')      # will have higher revenue
        prod_a = _product('Latte',  Decimal('50.00'),  cat_a)
        prod_b = _product('Pizza',  Decimal('200.00'), cat_b)
        o = _completed_order()
        _add_item(o, prod_a, quantity=1)   # Coffee: 50
        _add_item(o, prod_b, quantity=1)   # Food: 200

    def test_ranked_by_revenue_descending(self):
        cats = _cat_sales(_reports(self.client))
        revenues = [c['total'] for c in cats]
        self.assertEqual(revenues, sorted(revenues, reverse=True))

    def test_food_first(self):
        cats = _cat_sales(_reports(self.client))
        self.assertEqual(cats[0]['name'], 'Food')


# ── 5. Cancelled orders excluded ─────────────────────────────────────────────

class CancelledOrdersExcludedTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_cat_cancel')
        self.client = Client()
        self.client.login(username='admin_cat_cancel', password='pass123')
        cat  = _category('Coffee')
        prod = _product('Latte', Decimal('120.00'), cat)
        # One good completed order
        o_good = _completed_order()
        _add_item(o_good, prod, quantity=1)   # 120
        # One cancelled order — must not count
        o_bad = _cancelled_order()
        _add_item(o_bad, prod, quantity=10)   # 1200 — must not appear

    def test_cancelled_items_excluded(self):
        cats = _cat_sales(_reports(self.client))
        self.assertEqual(len(cats), 1)
        self.assertAlmostEqual(cats[0]['total'], 120.0, places=2)
        self.assertEqual(cats[0]['qty'], 1)

    def test_revenue_not_inflated(self):
        cats = _cat_sales(_reports(self.client))
        self.assertNotAlmostEqual(cats[0]['total'], 1320.0, places=0)


# ── 6. Non-completed statuses excluded ───────────────────────────────────────

class NonCompletedExcludedTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_cat_status')
        self.client = Client()
        self.client.login(username='admin_cat_status', password='pass123')
        cat  = _category('Coffee')
        prod = _product('Latte', Decimal('100.00'), cat)
        # One completed order
        o_good = _completed_order()
        _add_item(o_good, prod, quantity=1)
        # Non-completed
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
        cats = _cat_sales(_reports(self.client))
        self.assertEqual(len(cats), 1)
        self.assertAlmostEqual(cats[0]['total'], 100.0, places=2)
        self.assertEqual(cats[0]['qty'], 1)


# ── 7. Snapshot preserved after product deactivated (FK → NULL) ──────────────

class ProductDeactivatedSnapshotTest(TestCase):
    """
    After deactivation, OrderItem.product FK becomes NULL (SET_NULL).
    The old code (product__category__name) would return NULL and LOSE
    the revenue. The new code (category_name snapshot) preserves it.

    This is the regression test for the critical fix.
    """

    def setUp(self):
        self.admin = _user('admin_cat_deact')
        self.client = Client()
        self.client.login(username='admin_cat_deact', password='pass123')
        cat       = _category('Coffee')
        self.prod = _product('Latte', Decimal('130.00'), cat)
        o         = _completed_order()
        _add_item(o, self.prod, quantity=2, category_name='Coffee')  # snapshot: Coffee

    def test_revenue_before_deactivation(self):
        cats = _cat_sales(_reports(self.client))
        coffee = _cat(cats, 'Coffee')
        self.assertIsNotNone(coffee)
        self.assertAlmostEqual(coffee['total'], 260.0, places=2)

    def test_revenue_preserved_after_deactivation(self):
        """
        After product is deactivated, the FK on existing OrderItems goes NULL
        (SET_NULL). The snapshot-based query must still return the correct total.
        """
        self.prod.deactivate()
        # Verify FK is NOT nulled by deactivate() — deactivate() only sets
        # is_active=False, it does not null the FK.  FK becomes NULL only if
        # the product row is hard-deleted (which is blocked).
        # Either way, the snapshot is independent of the FK.
        cats   = _cat_sales(_reports(self.client))
        coffee = _cat(cats, 'Coffee')
        self.assertIsNotNone(coffee,
            "Coffee category must still appear after product deactivation")
        self.assertAlmostEqual(coffee['total'], 260.0, places=2,
            msg="Revenue must be preserved after product deactivation")

    def test_null_product_fk_does_not_lose_revenue(self):
        """
        Explicitly null the product FK on existing OrderItems
        (simulating what SET_NULL would do on hard delete).
        Revenue must still appear under the correct category.
        """
        OrderItem.objects.filter(
            product=self.prod
        ).update(product=None)

        cats   = _cat_sales(_reports(self.client))
        coffee = _cat(cats, 'Coffee')
        self.assertIsNotNone(coffee,
            "Coffee revenue must survive FK nullification (snapshot fix)")
        self.assertAlmostEqual(coffee['total'], 260.0, places=2)


# ── 8. Snapshot preserved after category change (historical accuracy) ─────────

class CategoryChangeSnapshotTest(TestCase):
    """
    After moving a product to a different category, historical OrderItems
    must stay in the original category (snapshot), not the new one.

    BEFORE fix (live join): Revenue would shift to the new category.
    AFTER fix (snapshot):   Revenue stays in the original category.

    This is the primary regression test for the critical bug.
    """

    def setUp(self):
        self.admin  = _user('admin_cat_change')
        self.client = Client()
        self.client.login(username='admin_cat_change', password='pass123')
        self.coffee = _category('Coffee')
        self.food   = _category('Food')
        self.prod   = _product('Latte', Decimal('150.00'), self.coffee)
        o = _completed_order()
        # Snapshot captured at order time: category_name='Coffee'
        _add_item(o, self.prod, quantity=2, category_name='Coffee')

    def test_revenue_in_coffee_before_change(self):
        cats = _cat_sales(_reports(self.client))
        coffee = _cat(cats, 'Coffee')
        food   = _cat(cats, 'Food')
        self.assertIsNotNone(coffee)
        self.assertAlmostEqual(coffee['total'], 300.0, places=2)
        self.assertIsNone(food, "Food must have no revenue before category change")

    def test_revenue_stays_in_coffee_after_moving_product_to_food(self):
        """
        Move Latte to the Food category.
        Historical orders must still count under Coffee (snapshot).
        """
        self.prod.category = self.food
        self.prod.save(update_fields=['category'])

        cats   = _cat_sales(_reports(self.client))
        coffee = _cat(cats, 'Coffee')
        food   = _cat(cats, 'Food')

        self.assertIsNotNone(coffee,
            "Coffee must still show historical revenue after product moved to Food")
        self.assertAlmostEqual(coffee['total'], 300.0, places=2,
            msg="Revenue must not shift to Food when product category changes")
        self.assertIsNone(food,
            "Food must NOT gain revenue from historical Coffee orders")

    def test_new_orders_after_category_change_go_to_food(self):
        """
        New orders placed AFTER the product is moved to Food capture the
        new category snapshot, so they correctly appear under Food.
        """
        self.prod.category = self.food
        self.prod.save(update_fields=['category'])
        # New order after the category change — snapshot should now say 'Food'
        o2 = _completed_order()
        _add_item(o2, self.prod, quantity=1, category_name='Food')

        cats   = _cat_sales(_reports(self.client))
        coffee = _cat(cats, 'Coffee')
        food   = _cat(cats, 'Food')

        # Old order still in Coffee (300), new order in Food (150)
        self.assertIsNotNone(coffee)
        self.assertAlmostEqual(coffee['total'], 300.0, places=2)
        self.assertIsNotNone(food)
        self.assertAlmostEqual(food['total'], 150.0, places=2)


# ── 9. No sales — empty result ────────────────────────────────────────────────

class NoSalesCategoryTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_cat_zero')
        self.client = Client()
        self.client.login(username='admin_cat_zero', password='pass123')

    def test_empty_category_sales_when_no_orders(self):
        cats = _cat_sales(_reports(self.client))
        self.assertEqual(cats, [])

    def test_page_still_renders(self):
        resp = _reports(self.client)
        self.assertEqual(resp.status_code, 200)


# ── 10. Zero-sales category absent ───────────────────────────────────────────

class ZeroSalesCategoryAbsentTest(TestCase):
    """A category that exists in the DB but has no matching orders
    must NOT appear in category_sales (not shown as 0 revenue)."""

    def setUp(self):
        self.admin = _user('admin_cat_absent')
        self.client = Client()
        self.client.login(username='admin_cat_absent', password='pass123')
        _category('EmptyCategory')   # category exists but no orders
        cat  = _category('Coffee')
        prod = _product('Latte', Decimal('100.00'), cat)
        o    = _completed_order()
        _add_item(o, prod, quantity=1)

    def test_empty_category_not_shown(self):
        cats  = _cat_sales(_reports(self.client))
        names = {c['name'] for c in cats}
        self.assertNotIn('EmptyCategory', names)

    def test_coffee_still_shown(self):
        cats  = _cat_sales(_reports(self.client))
        names = {c['name'] for c in cats}
        self.assertIn('Coffee', names)


# ── 11. Revenue uses stored subtotal (price at order time) ────────────────────

class RevenueUsesStoredSubtotalTest(TestCase):
    """
    Changing the product price after the order must NOT change
    the category revenue (which uses the stored subtotal snapshot).
    """

    def setUp(self):
        self.admin  = _user('admin_cat_price')
        self.client = Client()
        self.client.login(username='admin_cat_price', password='pass123')
        cat       = _category('Coffee')
        self.prod = _product('Latte', Decimal('100.00'), cat)
        o         = _completed_order()
        _add_item(o, self.prod, quantity=3, unit_price=Decimal('100.00'))

    def test_revenue_correct_at_order_time(self):
        cats   = _cat_sales(_reports(self.client))
        coffee = _cat(cats, 'Coffee')
        self.assertAlmostEqual(coffee['total'], 300.0, places=2)

    def test_revenue_unchanged_after_price_increase(self):
        self.prod.price = Decimal('999.00')
        self.prod.save(update_fields=['price'])
        cats   = _cat_sales(_reports(self.client))
        coffee = _cat(cats, 'Coffee')
        # Still 3 × 100 = 300 from the stored subtotal
        self.assertAlmostEqual(coffee['total'], 300.0, places=2)


# ── 12. Blank category_name excluded cleanly ─────────────────────────────────

class BlankCategoryNameExcludedTest(TestCase):
    """
    OrderItems with category_name='' (legacy data or products without a
    category snapshot) must be excluded without crashing the report.
    """

    def setUp(self):
        self.admin = _user('admin_cat_blank')
        self.client = Client()
        self.client.login(username='admin_cat_blank', password='pass123')
        cat  = _category('Coffee')
        prod = _product('Latte', Decimal('100.00'), cat)
        # Order with explicit blank category_name
        o = _completed_order()
        _add_item(o, prod, quantity=1, category_name='')
        # Order with proper snapshot
        o2 = _completed_order()
        _add_item(o2, prod, quantity=2, category_name='Coffee')

    def test_blank_category_excluded(self):
        cats  = _cat_sales(_reports(self.client))
        names = {c['name'] for c in cats}
        self.assertNotIn('', names)

    def test_proper_category_still_shown(self):
        cats   = _cat_sales(_reports(self.client))
        coffee = _cat(cats, 'Coffee')
        self.assertIsNotNone(coffee)
        # Only the 2-qty order with proper snapshot should count
        self.assertAlmostEqual(coffee['total'], 200.0, places=2)

    def test_page_does_not_crash(self):
        resp = _reports(self.client)
        self.assertEqual(resp.status_code, 200)


# ── 13. Multiple orders same category — aggregated ───────────────────────────

class SameCategoryMultipleOrdersTest(TestCase):
    def setUp(self):
        self.admin = _user('admin_cat_multi_order')
        self.client = Client()
        self.client.login(username='admin_cat_multi_order', password='pass123')
        cat  = _category('Coffee')
        prod = _product('Latte', Decimal('100.00'), cat)
        for _ in range(5):
            o = _completed_order()
            _add_item(o, prod, quantity=2, category_name='Coffee')

    def test_aggregated_into_single_entry(self):
        cats = _cat_sales(_reports(self.client))
        self.assertEqual(len(cats), 1)

    def test_revenue_summed(self):
        # 5 orders × 2 qty × 100 = 1000
        cats = _cat_sales(_reports(self.client))
        self.assertAlmostEqual(cats[0]['total'], 1000.0, places=2)

    def test_qty_summed(self):
        cats = _cat_sales(_reports(self.client))
        self.assertEqual(cats[0]['qty'], 10)
