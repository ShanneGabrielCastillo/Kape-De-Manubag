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


class InventoryListFilterTests(TestCase):
    """Regression tests for the inventory list search / filter bar (Phase 6B)."""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='inv_filter_admin', password=PASSWORD, role='admin',
        )
        self.cashier = CustomUser.objects.create_user(
            username='inv_filter_cashier', password=PASSWORD, role='cashier',
        )
        self.client.force_login(self.admin)

        self.cat_drinks = Category.objects.create(name='Drinks', slug='drinks-flt')
        self.cat_food = Category.objects.create(name='Food', slug='food-flt')

        # ok stock (qty=50, threshold=10)
        self.espresso = Product.objects.create(
            category=self.cat_drinks, name='Espresso Shot', price='60.00',
            stock_quantity=50,
        )
        # low stock (qty=8 <= threshold=10)
        self.latte = Product.objects.create(
            category=self.cat_drinks, name='Caffe Latte', price='75.00',
            stock_quantity=8,
        )
        # critical stock (qty=3 <= CRITICAL_STOCK_THRESHOLD=5)
        self.cake = Product.objects.create(
            category=self.cat_food, name='Chocolate Cake', price='120.00',
            stock_quantity=3,
        )
        # out of stock (qty=0)
        self.cookie = Product.objects.create(
            category=self.cat_food, name='Butter Cookie', price='40.00',
            stock_quantity=0,
        )

    # ── Access control ────────────────────────────────────────────────────────

    def test_inventory_list_requires_login(self):
        """Unauthenticated GET /inventory/ redirects to login."""
        self.client.logout()
        response = self.client.get(reverse('inventory:list'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])

    def test_inventory_list_cashier_can_access(self):
        """Cashier role can access the inventory list."""
        self.client.force_login(self.cashier)
        response = self.client.get(reverse('inventory:list'))
        self.assertEqual(response.status_code, 200)

    # ── No-filter baseline ────────────────────────────────────────────────────

    def test_inventory_list_no_filters_returns_all(self):
        """No query params → all products are returned."""
        response = self.client.get(reverse('inventory:list'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(list(response.context['products'])), 4)

    # ── Text search ───────────────────────────────────────────────────────────

    def test_inventory_list_search_by_name(self):
        """?q=espresso returns only the matching product."""
        response = self.client.get(reverse('inventory:list'), {'q': 'espresso'})
        products = list(response.context['products'])
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].name, 'Espresso Shot')

    def test_inventory_list_search_case_insensitive(self):
        """Uppercase search term still matches."""
        response = self.client.get(reverse('inventory:list'), {'q': 'ESPRESSO'})
        products = list(response.context['products'])
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].name, 'Espresso Shot')

    def test_inventory_list_search_no_results(self):
        """Unrecognised search term returns empty queryset."""
        response = self.client.get(reverse('inventory:list'), {'q': 'zzznomatch'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(list(response.context['products'])), 0)

    def test_inventory_list_search_whitespace(self):
        """Whitespace-only query is stripped and returns all products."""
        response = self.client.get(reverse('inventory:list'), {'q': '   '})
        self.assertEqual(len(list(response.context['products'])), 4)
        # Stripped value passed to template is empty string
        self.assertEqual(response.context['search'], '')

    # ── Category filter ───────────────────────────────────────────────────────

    def test_inventory_list_category_filter(self):
        """?category=<id> returns only products in that category."""
        response = self.client.get(
            reverse('inventory:list'), {'category': str(self.cat_drinks.pk)},
        )
        products = list(response.context['products'])
        self.assertEqual(len(products), 2)
        names = {p.name for p in products}
        self.assertIn('Espresso Shot', names)
        self.assertIn('Caffe Latte', names)

    # ── Stock-status filter ───────────────────────────────────────────────────

    def test_inventory_list_stock_status_out_of_stock(self):
        """?stock_status=out_of_stock returns only quantity=0 products."""
        response = self.client.get(
            reverse('inventory:list'), {'stock_status': 'out_of_stock'},
        )
        products = list(response.context['products'])
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].name, 'Butter Cookie')
        self.assertEqual(products[0].stock_quantity, 0)

    def test_inventory_list_stock_status_critical(self):
        """?stock_status=critical returns quantity<=CRITICAL_STOCK_THRESHOLD(5)."""
        response = self.client.get(
            reverse('inventory:list'), {'stock_status': 'critical'},
        )
        products = list(response.context['products'])
        # Butter Cookie (0) and Chocolate Cake (3) are both <= 5
        self.assertEqual(len(products), 2)
        quantities = {p.stock_quantity for p in products}
        self.assertTrue(all(q <= 5 for q in quantities))

    def test_inventory_list_stock_status_ok(self):
        """?stock_status=ok returns quantity > low_stock_threshold."""
        response = self.client.get(
            reverse('inventory:list'), {'stock_status': 'ok'},
        )
        products = list(response.context['products'])
        # Only Espresso Shot (50 > 10) qualifies
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].name, 'Espresso Shot')

    # ── Combined filters ──────────────────────────────────────────────────────

    def test_inventory_list_combined_filters(self):
        """Text search + stock_status combined narrow results correctly."""
        response = self.client.get(
            reverse('inventory:list'), {'q': 'latte', 'stock_status': 'low'},
        )
        products = list(response.context['products'])
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].name, 'Caffe Latte')

    # ── Invalid input ─────────────────────────────────────────────────────────

    def test_inventory_list_invalid_stock_status_ignored(self):
        """An unrecognised stock_status value is ignored; all products return."""
        response = self.client.get(
            reverse('inventory:list'), {'stock_status': 'invalid_garbage'},
        )
        self.assertEqual(len(list(response.context['products'])), 4)
        self.assertEqual(response.context['stock_status_filter'], '')

    def test_inventory_list_invalid_category_ignored(self):
        """A non-integer category value is ignored; all products return."""
        response = self.client.get(
            reverse('inventory:list'), {'category': 'notanumber'},
        )
        self.assertEqual(len(list(response.context['products'])), 4)
        self.assertEqual(response.context['category_filter'], '')

    # ── Clear behaviour ───────────────────────────────────────────────────────

    def test_inventory_list_clear_returns_all(self):
        """GET with no params returns all products (simulates Clear action)."""
        response = self.client.get(reverse('inventory:list'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(list(response.context['products'])), 4)

    # ── Summary cards unaffected by filters (CRITICAL) ───────────────────────

    def test_inventory_list_summary_cards_unaffected_by_filters(self):
        """Summary card counts always reflect the full catalog, not filtered rows."""
        # Apply a filter that returns only 1 product
        response = self.client.get(reverse('inventory:list'), {'q': 'espresso'})
        self.assertEqual(response.status_code, 200)
        ctx = response.context

        # Only 1 product in filtered table
        self.assertEqual(len(list(ctx['products'])), 1)

        # But summary cards still show full-catalog figures
        self.assertEqual(ctx['total_count'], 4)
        # low_stock_count: latte(8<=10), cake(3<=10 via threshold), cookie(0<=10) = 3
        self.assertEqual(ctx['low_stock_count'], 3)
        # out_of_stock_count: only cookie
        self.assertEqual(ctx['out_of_stock_count'], 1)

    # ── Read-only assertion ───────────────────────────────────────────────────

    def test_inventory_list_filtering_is_read_only(self):
        """Filtering must not change any product quantities."""
        before = {
            p.pk: p.stock_quantity
            for p in Product.objects.all()
        }
        self.client.get(reverse('inventory:list'), {
            'q': 'espresso', 'category': str(self.cat_drinks.pk),
            'stock_status': 'ok',
        })
        after = {
            p.pk: p.stock_quantity
            for p in Product.objects.all()
        }
        self.assertEqual(before, after)


class InventoryLogActionSourceTests(TestCase):
    """Phase 6C regression tests: action filter, source filter, combined."""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='inv_as_admin', password=PASSWORD, role='admin',
        )
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks-as')
        self.coffee = Product.objects.create(
            category=self.category, name='Iced Coffee', price='60.00',
            stock_quantity=10,
        )
        self.tea = Product.objects.create(
            category=self.category, name='Milk Tea', price='80.00',
            stock_quantity=10,
        )

        # One log row per unique (action, source) combination used in tests.
        self.log_restock = InventoryLog.objects.create(
            product=self.coffee, action='restock', source='manual_adjustment',
            reason='Restock', quantity_change=10, quantity_before=0,
            quantity_after=10, performed_by=self.admin,
        )
        self.log_adjustment = InventoryLog.objects.create(
            product=self.coffee, action='adjustment', source='inventory_update',
            reason='Stock count fix', quantity_change=-1, quantity_before=10,
            quantity_after=9, performed_by=self.admin,
        )
        self.log_sale = InventoryLog.objects.create(
            product=self.tea, action='sale', source='customer_order',
            reason='Order #KDM-001', quantity_change=-2, quantity_before=10,
            quantity_after=8, performed_by=self.admin,
        )
        self.log_waste = InventoryLog.objects.create(
            product=self.tea, action='waste', source='pos_order',
            reason='Expired product', quantity_change=-1, quantity_before=8,
            quantity_after=7, performed_by=self.admin,
        )

    # ── Single action filters ─────────────────────────────────────────────

    def test_action_filter_restock(self):
        response = self.client.get(reverse('inventory:log'), {'action': 'restock'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 1)
        self.assertEqual(response.context['logs'][0].action, 'restock')

    def test_action_filter_adjustment(self):
        response = self.client.get(reverse('inventory:log'), {'action': 'adjustment'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 1)
        self.assertEqual(response.context['logs'][0].action, 'adjustment')

    def test_action_filter_sale(self):
        response = self.client.get(reverse('inventory:log'), {'action': 'sale'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 1)
        self.assertEqual(response.context['logs'][0].action, 'sale')

    def test_action_filter_waste(self):
        response = self.client.get(reverse('inventory:log'), {'action': 'waste'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 1)
        self.assertEqual(response.context['logs'][0].action, 'waste')

    # ── Single source filters ─────────────────────────────────────────────

    def test_source_filter_manual_adjustment(self):
        response = self.client.get(reverse('inventory:log'), {'source': 'manual_adjustment'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 1)
        self.assertEqual(response.context['logs'][0].source, 'manual_adjustment')

    def test_source_filter_customer_order(self):
        response = self.client.get(reverse('inventory:log'), {'source': 'customer_order'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 1)
        self.assertEqual(response.context['logs'][0].source, 'customer_order')

    def test_source_filter_pos_order(self):
        response = self.client.get(reverse('inventory:log'), {'source': 'pos_order'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 1)
        self.assertEqual(response.context['logs'][0].source, 'pos_order')

    def test_source_filter_inventory_update(self):
        response = self.client.get(reverse('inventory:log'), {'source': 'inventory_update'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 1)
        self.assertEqual(response.context['logs'][0].source, 'inventory_update')

    # ── Combined filters ──────────────────────────────────────────────────

    def test_action_and_source_combined(self):
        """action=restock AND source=manual_adjustment → exactly one record."""
        response = self.client.get(
            reverse('inventory:log'),
            {'action': 'restock', 'source': 'manual_adjustment'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 1)
        log = response.context['logs'][0]
        self.assertEqual(log.action, 'restock')
        self.assertEqual(log.source, 'manual_adjustment')

    def test_text_search_with_action_filter(self):
        """?q=coffee&action=restock returns only the coffee restock record."""
        response = self.client.get(
            reverse('inventory:log'),
            {'q': 'coffee', 'action': 'restock'},
        )
        self.assertEqual(response.status_code, 200)
        logs = list(response.context['logs'])
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].product.name, 'Iced Coffee')
        self.assertEqual(logs[0].action, 'restock')

    def test_all_three_filters_combined(self):
        """All three filters combined produce the narrowest correct result set."""
        response = self.client.get(
            reverse('inventory:log'),
            {'q': 'Expired', 'action': 'waste', 'source': 'pos_order'},
        )
        self.assertEqual(response.status_code, 200)
        logs = list(response.context['logs'])
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].action, 'waste')
        self.assertEqual(logs[0].source, 'pos_order')

    # ── Invalid / empty values ────────────────────────────────────────────

    def test_invalid_action_ignored(self):
        """An unrecognised action value is ignored; all records return."""
        response = self.client.get(
            reverse('inventory:log'), {'action': 'invalid_garbage'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 4)
        self.assertEqual(response.context['action_filter'], '')

    def test_invalid_source_ignored(self):
        """An unrecognised source value is ignored; all records return."""
        response = self.client.get(
            reverse('inventory:log'), {'source': 'invalid_garbage'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 4)
        self.assertEqual(response.context['source_filter'], '')

    def test_empty_action_returns_all(self):
        """?action= (empty string) returns all records."""
        response = self.client.get(reverse('inventory:log'), {'action': ''})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 4)

    # ── Clear behaviour ───────────────────────────────────────────────────

    def test_clear_resets_action_and_source(self):
        """GET with no params returns all records (simulates Clear)."""
        response = self.client.get(reverse('inventory:log'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 4)
        self.assertEqual(response.context['action_filter'], '')
        self.assertEqual(response.context['source_filter'], '')
        self.assertEqual(response.context['q'], '')

    # ── Pagination ────────────────────────────────────────────────────────

    def test_pagination_preserves_action_filter(self):
        """Pagination links carry the active action= param."""
        # Create 12 more restock entries to force 2 pages (page size = 10).
        for i in range(12):
            InventoryLog.objects.create(
                product=self.coffee, action='restock', source='manual_adjustment',
                reason='Restock batch', quantity_change=1, quantity_before=i,
                quantity_after=i + 1, performed_by=self.admin,
            )
        response = self.client.get(
            reverse('inventory:log'), {'action': 'restock', 'page': '1'},
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('action=restock', content)

    def test_filter_resets_to_page_1(self):
        """Applying a filter without a page param returns page 1."""
        # Create 15 restock entries so there are multiple pages.
        for i in range(15):
            InventoryLog.objects.create(
                product=self.coffee, action='restock', source='manual_adjustment',
                reason='Restock batch', quantity_change=1, quantity_before=i,
                quantity_after=i + 1, performed_by=self.admin,
            )
        # Request without explicit page param → must land on page 1.
        response = self.client.get(
            reverse('inventory:log'), {'action': 'restock'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['logs'].number, 1)

    # ── Read-only assertion ───────────────────────────────────────────────

    def test_action_filter_does_not_create_records(self):
        """Filtering must not create, modify, or delete any inventory records."""
        before_count = InventoryLog.objects.count()
        before_qtys  = {p.pk: p.stock_quantity for p in Product.objects.all()}

        self.client.get(
            reverse('inventory:log'),
            {'action': 'restock', 'source': 'manual_adjustment', 'q': 'coffee'},
        )

        self.assertEqual(InventoryLog.objects.count(), before_count)
        after_qtys = {p.pk: p.stock_quantity for p in Product.objects.all()}
        self.assertEqual(before_qtys, after_qtys)


class InventoryListAjaxTests(TestCase):
    """AJAX partial-replacement: confirm results-region is present in inventory
    list responses and that stats-grid is NOT nested inside results-region.

    The stats-grid must stay outside results-region at all times — the JS
    innerHTML swap must never clobber the summary cards.
    """

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='inv_ajax_admin', password='test-pass-1', role='admin',
        )
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks-ajax')
        Product.objects.create(
            category=self.category, name='Espresso',
            price='60.00', stock_quantity=50,
        )
        Product.objects.create(
            category=self.category, name='Cold Brew',
            price='75.00', stock_quantity=0,
        )

    def test_results_region_present(self):
        """GET /inventory/ returns id="results-region"."""
        response = self.client.get(reverse('inventory:list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="results-region"')

    def test_results_region_present_with_q_filter(self):
        """GET ?q=coffee returns id="results-region"."""
        response = self.client.get(reverse('inventory:list'), {'q': 'coffee'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="results-region"')

    def test_results_region_present_with_stock_status_filter(self):
        """GET ?stock_status=out_of_stock returns id="results-region"."""
        response = self.client.get(
            reverse('inventory:list'), {'stock_status': 'out_of_stock'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="results-region"')

    def test_stats_grid_not_inside_results_region(self):
        """stats-grid must appear before results-region in the HTML source.

        This ensures the JS innerHTML replacement of results-region can never
        remove or duplicate the summary cards.
        """
        response = self.client.get(reverse('inventory:list'), {'q': 'test'})
        content = response.content.decode()
        stats_pos = content.find('class="stats-grid"')
        region_pos = content.find('id="results-region"')
        self.assertGreater(
            region_pos, stats_pos,
            'stats-grid must appear before results-region in the HTML '
            '(i.e. is not nested inside it)',
        )

    def test_restock_modal_inside_results_region(self):
        """restock-modal must appear inside results-region so it updates with results."""
        response = self.client.get(reverse('inventory:list'))
        content = response.content.decode()
        region_open = content.find('id="results-region"')
        modal_pos = content.find('id="restock-modal"')
        region_close = content.rfind('</div>', 0, len(content))
        # Modal must come after the results-region opening tag
        self.assertGreater(modal_pos, region_open,
            'restock-modal must appear after the results-region opening tag')

    def test_filter_bar_outside_results_region(self):
        """filter-bar must appear before results-region in the HTML source."""
        response = self.client.get(reverse('inventory:list'))
        content = response.content.decode()
        filter_bar_pos = content.find('class="filter-bar"')
        region_pos = content.find('id="results-region"')
        self.assertGreater(region_pos, filter_bar_pos,
            'filter-bar must appear before results-region (not nested inside it)')

