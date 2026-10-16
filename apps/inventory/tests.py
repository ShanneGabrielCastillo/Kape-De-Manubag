"""
Tests for the inventory audit trail and the log search.

Every entry carries product, user, timestamp, before/after/change, a source
(Customer Order / POS Order / Manual Adjustment / Inventory Update) and a
short reason; the log page must show them and support searching.
"""
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import CustomUser
from apps.inventory.models import InventoryLog
from apps.menu.models import Category, Product

PASSWORD = 'kdm-inv-pass-1'


class InventoryLogSearchTests(TestCase):
    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='inv_admin', password=PASSWORD, role='admin',
        )
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.coffee = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
            stock_quantity=10,
        )
        self.tea = Product.objects.create(
            category=self.category, name='Milk Tea', price='80.00',
            stock_quantity=10,
        )
        InventoryLog.objects.create(
            product=self.coffee, action='restock', source='manual_adjustment',
            reason='Restock', quantity_change=10, quantity_before=0,
            quantity_after=10, notes='From supplier', performed_by=self.admin,
        )
        InventoryLog.objects.create(
            product=self.tea, action='sale', source='pos_order',
            reason='Order #KDM-20260814-0001', quantity_change=-2,
            quantity_before=10, quantity_after=8, notes='',
            performed_by=self.admin,
        )

    def test_log_page_shows_source_and_reason(self):
        response = self.client.get(reverse('inventory:log'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Iced Coffee')
        self.assertContains(response, 'Milk Tea')
        # Source displays and reason text are rendered.
        self.assertContains(response, 'Manual Adjustment')
        self.assertContains(response, 'POS Order')
        self.assertContains(response, 'Order #KDM-20260814-0001')

    def test_search_by_product_name(self):
        response = self.client.get(reverse('inventory:log'), {'q': 'coffee'})
        self.assertContains(response, 'Iced Coffee')
        self.assertNotContains(response, 'Milk Tea')

    def test_search_by_reason(self):
        response = self.client.get(reverse('inventory:log'), {'q': 'KDM-20260814'})
        self.assertContains(response, 'Milk Tea')
        self.assertNotContains(response, 'Iced Coffee')

    def test_search_by_notes(self):
        response = self.client.get(reverse('inventory:log'), {'q': 'supplier'})
        self.assertContains(response, 'Iced Coffee')
        self.assertNotContains(response, 'Milk Tea')

    def test_search_with_no_matches_shows_clear_message(self):
        response = self.client.get(reverse('inventory:log'), {'q': 'zzz-nope'})
        self.assertContains(response, 'No inventory logs match')

    # ── New tests for automatic-search feature ────────────────────────────────

    def test_inventory_log_filter_by_query(self):
        """GET ?q=<term> returns only matching records."""
        response = self.client.get(reverse('inventory:log'), {'q': 'coffee'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Iced Coffee')
        self.assertNotContains(response, 'Milk Tea')

    def test_inventory_log_filter_empty_query(self):
        """GET ?q= (empty) strips to empty string and returns all records."""
        response = self.client.get(reverse('inventory:log'), {'q': ''})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Iced Coffee')
        self.assertContains(response, 'Milk Tea')

    def test_inventory_log_filter_no_results(self):
        """GET ?q=zzznomatch returns empty queryset; page renders with empty state."""
        response = self.client.get(reverse('inventory:log'), {'q': 'zzznomatch'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No inventory logs match')
        # Confirm no log rows are in context
        self.assertEqual(len(response.context['logs']), 0)

    def test_inventory_log_pagination_preserves_query(self):
        """Pagination links include the active q= parameter."""
        # Create 15 extra coffee logs to force multiple pages.
        for i in range(15):
            InventoryLog.objects.create(
                product=self.coffee, action='restock', source='manual_adjustment',
                reason='Restock', quantity_change=1, quantity_before=i,
                quantity_after=i + 1, notes='batch', performed_by=self.admin,
            )
        response = self.client.get(reverse('inventory:log'), {'q': 'coffee', 'page': '1'})
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # Pagination links must carry q=coffee so the query is preserved.
        self.assertIn('q=coffee', content)

    def test_inventory_log_unauthorized(self):
        """Unauthenticated GET /inventory/log/ redirects to login."""
        self.client.logout()
        response = self.client.get(reverse('inventory:log'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])


class InventorySummaryCardTests(TestCase):
    """The inventory list summary cards must report accurate counts.

    Covers every state that feeds the cards: total vs active, low stock using
    each product's OWN threshold (not a hardcoded number), critical (<= the
    shared threshold), and out-of-stock -- including inactive products, which
    still count toward the totals but must be reflected exactly once.
    """

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='inv_summary', password=PASSWORD, role='admin',
        )
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks')

    def _product(self, name, stock, **kwargs):
        return Product.objects.create(
            category=self.category, name=name, price='60.00',
            stock_quantity=stock, **kwargs,
        )

    def test_summary_counts_are_accurate(self):
        self._product('Ok Item', 50)                          # active, ok
        self._product('Low Item', 8)                          # active, low
        self._product('Critical Item', 3)                     # active, critical
        self._product('Inactive Gone', 0, is_active=False)    # inactive, out of stock
        self._product('Out Item', 0)                          # active, out of stock
        self._product('Custom Threshold', 15, low_stock_threshold=20)  # low via own threshold

        response = self.client.get(reverse('inventory:list'))
        self.assertEqual(response.status_code, 200)
        ctx = response.context
        self.assertEqual(ctx['total_count'], 6)
        self.assertEqual(ctx['active_count'], 5)
        self.assertEqual(ctx['low_stock_count'], 5)   # Low, Critical, Inactive Gone, Out, Custom Threshold
        self.assertEqual(ctx['critical_stock_count'], 3)  # Critical, Inactive Gone, Out
        self.assertEqual(ctx['out_of_stock_count'], 2)    # Inactive Gone, Out

    def test_summary_cards_render_labels_and_values(self):
        self._product('Ok Item', 50)
        self._product('Out Item', 0)
        self._product('Critical Item', 3)

        response = self.client.get(reverse('inventory:list'))
        # All four card labels are present.
        for label in ['Total Products', 'Active Products',
                      'Low Stock Items', 'Out of Stock']:
            self.assertContains(response, label)
        # Values render inside the cards.
        self.assertContains(response, '>3<')   # total
        self.assertContains(response, '>3<')   # active
        self.assertContains(response, '>2<')   # low (Out + Critical)
        self.assertContains(response, '>1<')   # out of stock

    def test_zero_products_shows_zero_summary(self):
        response = self.client.get(reverse('inventory:list'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_count'], 0)
        self.assertEqual(response.context['active_count'], 0)
        self.assertEqual(response.context['low_stock_count'], 0)
        self.assertEqual(response.context['out_of_stock_count'], 0)
        self.assertContains(response, '>0<')


class RestockInputRobustnessTests(TestCase):
    """Non-numeric restock input must fail gracefully, not 500."""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='restock_input', password=PASSWORD, role='admin',
        )
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
            stock_quantity=10,
        )

    def test_non_numeric_quantity_is_rejected_cleanly(self):
        response = self.client.post(
            reverse('inventory:restock', args=[self.product.pk]),
            {'quantity': 'abc'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        # No stock change, no log row.
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_missing_quantity_is_rejected_cleanly(self):
        response = self.client.post(
            reverse('inventory:restock', args=[self.product.pk]),
            {'quantity': ''},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)
