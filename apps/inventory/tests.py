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
        response = self.client.get(reverse('inventory:legacy_log'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Iced Coffee')
        self.assertContains(response, 'Milk Tea')
        # Source displays and reason text are rendered.
        self.assertContains(response, 'Manual Adjustment')
        self.assertContains(response, 'POS Order')
        self.assertContains(response, 'Order #KDM-20260814-0001')

    def test_search_by_product_name(self):
        response = self.client.get(reverse('inventory:legacy_log'), {'q': 'coffee'})
        self.assertContains(response, 'Iced Coffee')
        self.assertNotContains(response, 'Milk Tea')

    def test_search_by_reason(self):
        response = self.client.get(reverse('inventory:legacy_log'), {'q': 'KDM-20260814'})
        self.assertContains(response, 'Milk Tea')
        self.assertNotContains(response, 'Iced Coffee')

    def test_search_by_notes(self):
        response = self.client.get(reverse('inventory:legacy_log'), {'q': 'supplier'})
        self.assertContains(response, 'Iced Coffee')
        self.assertNotContains(response, 'Milk Tea')

    def test_search_with_no_matches_shows_clear_message(self):
        response = self.client.get(reverse('inventory:legacy_log'), {'q': 'zzz-nope'})
        self.assertContains(response, 'No inventory logs match')

    # ── New tests for automatic-search feature ────────────────────────────────

    def test_inventory_log_filter_by_query(self):
        """GET ?q=<term> returns only matching records."""
        response = self.client.get(reverse('inventory:legacy_log'), {'q': 'coffee'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Iced Coffee')
        self.assertNotContains(response, 'Milk Tea')

    def test_inventory_log_filter_empty_query(self):
        """GET ?q= (empty) strips to empty string and returns all records."""
        response = self.client.get(reverse('inventory:legacy_log'), {'q': ''})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Iced Coffee')
        self.assertContains(response, 'Milk Tea')

    def test_inventory_log_filter_no_results(self):
        """GET ?q=zzznomatch returns empty queryset; page renders with empty state."""
        response = self.client.get(reverse('inventory:legacy_log'), {'q': 'zzznomatch'})
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
        response = self.client.get(reverse('inventory:legacy_log'), {'q': 'coffee', 'page': '1'})
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # Pagination links must carry q=coffee so the query is preserved.
        self.assertIn('q=coffee', content)

    def test_inventory_log_unauthorized(self):
        """Unauthenticated GET /inventory/log/ redirects to login."""
        self.client.logout()
        response = self.client.get(reverse('inventory:legacy_log'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])


class LegacyStockPageRetiredTests(TestCase):
    """Phase 7B-3A — the legacy finished-product stock management page is
    RETIRED. /inventory/ is retained only as a safe compatibility redirect
    to the ingredient inventory screen (staff bookmarks and the dashboard
    low-stock card link may still point here). The followed target must
    never expose finished-product stock-management controls."""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='inv_summary', password=PASSWORD, role='admin',
        )
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks')

    def _legacy_product(self, name, stock):
        return Product.objects.create(
            inventory_tracking_mode='legacy',
            category=self.category, name=name, price='60.00',
            stock_quantity=stock,
        )

    def test_legacy_list_url_redirects_to_ingredients(self):
        self._legacy_product('Redirect Probe', 50)
        response = self.client.get(reverse('inventory:list'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('inventory:ingredient_list'))

    def test_followed_target_serves_ingredients_not_stock_controls(self):
        self._legacy_product('Control Probe', 50)
        response = self.client.get(reverse('inventory:list'), follow=True)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        # The retired page's identity must be gone entirely.
        self.assertNotIn('Legacy Finished-Product Stock', html)
        self.assertNotIn('Legacy Products', html)
        # No restock affordance anywhere on the followed target.
        self.assertNotIn('openRestockModal', html)
        self.assertNotIn('restock-modal', html)
        self.assertNotIn('/restock/', html)

    def test_legacy_products_not_listed_on_followed_target(self):
        product = self._legacy_product('Not Listed Probe', 50)
        response = self.client.get(reverse('inventory:list'), follow=True)
        html = response.content.decode()
        self.assertNotIn(product.name, html)


class RestockRetiredContractTests(TestCase):
    """Phase 7B-3A — legacy finished-product restocking is RETIRED.

    The URL is preserved for unknown external consumers, but EVERY request
    is non-mutating: POST returns 410 Gone with an explicit error (never a
    fake success), GET redirects to the ingredient inventory. No request
    may change Product.stock_quantity or write an InventoryLog row."""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='restock_input', password=PASSWORD, role='admin',
        )
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks')
        self.product = Product.objects.create(
            inventory_tracking_mode='legacy',
            category=self.category, name='Iced Coffee', price='60.00',
            stock_quantity=10,
        )

    def _assert_not_mutated(self):
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)
        self.assertEqual(InventoryLog.objects.count(), 0)

    def test_post_returns_410_gone_not_success(self):
        response = self.client.post(
            reverse('inventory:restock', args=[self.product.pk]),
            {'quantity': '5', 'notes': 'retired probe'},
        )
        self.assertEqual(response.status_code, 410)
        self.assertFalse(response.json()['success'])
        self.assertIn('retired', response.json()['error'].lower())
        self._assert_not_mutated()

    def test_post_with_non_numeric_quantity_still_410(self):
        response = self.client.post(
            reverse('inventory:restock', args=[self.product.pk]),
            {'quantity': 'abc'},
        )
        self.assertEqual(response.status_code, 410)
        self.assertFalse(response.json()['success'])
        self._assert_not_mutated()

    def test_post_with_missing_quantity_still_410(self):
        response = self.client.post(
            reverse('inventory:restock', args=[self.product.pk]),
            {'quantity': ''},
        )
        self.assertEqual(response.status_code, 410)
        self.assertFalse(response.json()['success'])
        self._assert_not_mutated()

    def test_get_redirects_to_ingredients_without_mutating(self):
        response = self.client.get(
            reverse('inventory:restock', args=[self.product.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'],
                         reverse('inventory:ingredient_list'))
        self._assert_not_mutated()

    def test_unknown_product_still_404s(self):
        response = self.client.post(
            reverse('inventory:restock', args=[self.product.pk + 999]),
            {'quantity': '5'},
        )
        self.assertEqual(response.status_code, 404)


class LegacyStockRedirectAccessTests(TestCase):
    """Phase 7B-3A — access control on the retired /inventory/ URL is
    UNCHANGED: the decorators still run before the compatibility redirect,
    and visiting it (with any query-string filters a bookmark may carry)
    never mutates stock."""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='inv_filter_admin', password=PASSWORD, role='admin',
        )
        self.cashier = CustomUser.objects.create_user(
            username='inv_filter_cashier', password=PASSWORD, role='cashier',
        )
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks-flt')
        self.product = Product.objects.create(
            inventory_tracking_mode='legacy',
            category=self.category, name='Espresso Shot', price='60.00',
            stock_quantity=50,
        )

    def test_inventory_list_requires_login(self):
        """Unauthenticated GET /inventory/ redirects to login (not ingredients)."""
        self.client.logout()
        response = self.client.get(reverse('inventory:list'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])

    def test_inventory_list_cashier_can_still_reach_target(self):
        """Cashier role is still authorized — redirect resolves to a 200 page."""
        self.client.force_login(self.cashier)
        response = self.client.get(reverse('inventory:list'), follow=True)
        self.assertEqual(response.status_code, 200)
        # The followed target is the ingredient inventory screen.
        self.assertContains(response, 'Ingredients')

    def test_stale_bookmark_filters_never_mutate_stock(self):
        """Old filter query-strings on the retired URL change nothing."""
        before = {p.pk: p.stock_quantity for p in Product.objects.all()}
        self.client.get(reverse('inventory:list'), {
            'q': 'espresso', 'category': self.category.pk,
            'stock_status': 'ok',
        })
        after = {p.pk: p.stock_quantity for p in Product.objects.all()}
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
        response = self.client.get(reverse('inventory:legacy_log'), {'action': 'restock'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 1)
        self.assertEqual(response.context['logs'][0].action, 'restock')

    def test_action_filter_adjustment(self):
        response = self.client.get(reverse('inventory:legacy_log'), {'action': 'adjustment'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 1)
        self.assertEqual(response.context['logs'][0].action, 'adjustment')

    def test_action_filter_sale(self):
        response = self.client.get(reverse('inventory:legacy_log'), {'action': 'sale'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 1)
        self.assertEqual(response.context['logs'][0].action, 'sale')

    def test_action_filter_waste(self):
        response = self.client.get(reverse('inventory:legacy_log'), {'action': 'waste'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 1)
        self.assertEqual(response.context['logs'][0].action, 'waste')

    # ── Single source filters ─────────────────────────────────────────────

    def test_source_filter_manual_adjustment(self):
        response = self.client.get(reverse('inventory:legacy_log'), {'source': 'manual_adjustment'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 1)
        self.assertEqual(response.context['logs'][0].source, 'manual_adjustment')

    def test_source_filter_customer_order(self):
        response = self.client.get(reverse('inventory:legacy_log'), {'source': 'customer_order'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 1)
        self.assertEqual(response.context['logs'][0].source, 'customer_order')

    def test_source_filter_pos_order(self):
        response = self.client.get(reverse('inventory:legacy_log'), {'source': 'pos_order'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 1)
        self.assertEqual(response.context['logs'][0].source, 'pos_order')

    def test_source_filter_inventory_update(self):
        response = self.client.get(reverse('inventory:legacy_log'), {'source': 'inventory_update'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 1)
        self.assertEqual(response.context['logs'][0].source, 'inventory_update')

    # ── Combined filters ──────────────────────────────────────────────────

    def test_action_and_source_combined(self):
        """action=restock AND source=manual_adjustment → exactly one record."""
        response = self.client.get(
            reverse('inventory:legacy_log'),
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
            reverse('inventory:legacy_log'),
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
            reverse('inventory:legacy_log'),
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
            reverse('inventory:legacy_log'), {'action': 'invalid_garbage'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 4)
        self.assertEqual(response.context['action_filter'], '')

    def test_invalid_source_ignored(self):
        """An unrecognised source value is ignored; all records return."""
        response = self.client.get(
            reverse('inventory:legacy_log'), {'source': 'invalid_garbage'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 4)
        self.assertEqual(response.context['source_filter'], '')

    def test_empty_action_returns_all(self):
        """?action= (empty string) returns all records."""
        response = self.client.get(reverse('inventory:legacy_log'), {'action': ''})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['logs']), 4)

    # ── Clear behaviour ───────────────────────────────────────────────────

    def test_clear_resets_action_and_source(self):
        """GET with no params returns all records (simulates Clear)."""
        response = self.client.get(reverse('inventory:legacy_log'))
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
            reverse('inventory:legacy_log'), {'action': 'restock', 'page': '1'},
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
            reverse('inventory:legacy_log'), {'action': 'restock'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['logs'].number, 1)

    # ── Read-only assertion ───────────────────────────────────────────────

    def test_action_filter_does_not_create_records(self):
        """Filtering must not create, modify, or delete any inventory records."""
        before_count = InventoryLog.objects.count()
        before_qtys  = {p.pk: p.stock_quantity for p in Product.objects.all()}

        self.client.get(
            reverse('inventory:legacy_log'),
            {'action': 'restock', 'source': 'manual_adjustment', 'q': 'coffee'},
        )

        self.assertEqual(InventoryLog.objects.count(), before_count)
        after_qtys = {p.pk: p.stock_quantity for p in Product.objects.all()}
        self.assertEqual(before_qtys, after_qtys)


class RetiredListServesNoAjaxScaffoldingTests(TestCase):
    """Phase 7B-3A — the retired /inventory/ page's AJAX scaffolding
    (results-region swap target, restock modal, legacy filter bar) must not
    be served by the compatibility redirect's followed target."""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='inv_ajax_admin', password='test-pass-1', role='admin',
        )
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks-ajax')

    def _followed_html(self, query=None):
        response = self.client.get(reverse('inventory:list'), query or {},
                                   follow=True)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_followed_target_is_the_ingredients_page(self):
        # The ingredient screen legitimately has its own results-region
        # AJAX scaffolding — assert positive identity of the target instead.
        html = self._followed_html()
        self.assertIn('Ingredients', html)
        self.assertIn('id="results-region"', html)

    def test_stale_filters_still_land_on_ingredients(self):
        html = self._followed_html({'q': 'coffee',
                                    'stock_status': 'out_of_stock'})
        self.assertIn('Ingredients', html)

    def test_no_restock_modal_on_followed_target(self):
        html = self._followed_html()
        self.assertNotIn('id="restock-modal"', html)
        self.assertNotIn('submitRestock', html)

    def test_no_legacy_filter_bar_on_followed_target(self):
        html = self._followed_html()
        self.assertNotIn('id="inv-stock-status"', html)
        self.assertNotIn('All Stock Levels', html)


class InventoryLogAjaxTests(TestCase):
    """AJAX partial-replacement: confirm id='results-region' is present in
    Inventory Log responses for plain, q=, action=, and source= requests."""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='invlog_ajax_admin', password='test-pass-1', role='admin',
        )
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Drinks', slug='drinks-log-ajax')
        self.coffee = Product.objects.create(
            category=self.category, name='Iced Coffee',
            price='60.00', stock_quantity=10,
        )
        InventoryLog.objects.create(
            product=self.coffee, action='restock', source='manual_adjustment',
            reason='Restock', quantity_change=10, quantity_before=0,
            quantity_after=10, performed_by=self.admin,
        )

    def test_results_region_present(self):
        """GET /inventory/log/ returns id="results-region"."""
        response = self.client.get(reverse('inventory:legacy_log'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="results-region"')

    def test_results_region_present_with_q_filter(self):
        """GET /inventory/log/?q=coffee returns id="results-region"."""
        response = self.client.get(reverse('inventory:legacy_log'), {'q': 'coffee'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="results-region"')

    def test_results_region_present_with_action_filter(self):
        """GET /inventory/log/?action=restock returns id="results-region"."""
        response = self.client.get(reverse('inventory:legacy_log'), {'action': 'restock'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="results-region"')

    def test_results_region_present_with_source_filter(self):
        """GET /inventory/log/?source=manual_adjustment returns id="results-region"."""
        response = self.client.get(
            reverse('inventory:legacy_log'), {'source': 'manual_adjustment'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="results-region"')

    def test_filter_bar_outside_results_region(self):
        """filter-bar must appear before results-region in the HTML source."""
        response = self.client.get(reverse('inventory:legacy_log'))
        content = response.content.decode()
        filter_bar_pos = content.find('id="log-filter-form"')
        region_pos = content.find('id="results-region"')
        self.assertGreater(region_pos, filter_bar_pos,
            'log-filter-form must appear before results-region (not nested inside it)')


# ═════════════════════════════════════════════════════════════════════════════
# Phase 2B-A tests — ingredient listing and creation
# ═════════════════════════════════════════════════════════════════════════════

from decimal import Decimal

from apps.inventory.models import InventoryItem

_ING_PASSWORD = 'test-ing-pass-1'

INGREDIENT_LIST_URL = reverse('inventory:ingredient_list')
INGREDIENT_CREATE_URL = reverse('inventory:ingredient_create')


def _make_user(username, role):
    return CustomUser.objects.create_user(
        username=username, password=_ING_PASSWORD, role=role,
    )


def _make_item(name='Rice', item_type='ingredient', base_unit='g',
               on_hand=Decimal('1000.000')):
    return InventoryItem.objects.create(
        name=name,
        item_type=item_type,
        base_unit=base_unit,
        on_hand_quantity=on_hand,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Ingredient list — authorization
# ─────────────────────────────────────────────────────────────────────────────

class IngredientListAuthTests(TestCase):
    """Role-based access control for the ingredient list page."""

    def test_admin_can_access(self):
        admin = _make_user('ing_list_admin', 'admin')
        self.client.force_login(admin)
        response = self.client.get(INGREDIENT_LIST_URL)
        self.assertEqual(response.status_code, 200)

    def test_cashier_can_access(self):
        cashier = _make_user('ing_list_cashier', 'cashier')
        self.client.force_login(cashier)
        response = self.client.get(INGREDIENT_LIST_URL)
        self.assertEqual(response.status_code, 200)

    def test_kitchen_staff_cannot_access(self):
        kitchen = _make_user('ing_list_kitchen', 'kitchen_staff')
        self.client.force_login(kitchen)
        response = self.client.get(INGREDIENT_LIST_URL)
        # cashier_or_admin_required redirects denied users to menu:index
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('/inventory/ingredients', response['Location'])

    def test_customer_cannot_access(self):
        customer = _make_user('ing_list_customer', 'customer')
        self.client.force_login(customer)
        response = self.client.get(INGREDIENT_LIST_URL)
        self.assertEqual(response.status_code, 302)

    def test_unauthenticated_redirected_to_login(self):
        response = self.client.get(INGREDIENT_LIST_URL)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])


# ─────────────────────────────────────────────────────────────────────────────
# 2. Ingredient list — rendering
# ─────────────────────────────────────────────────────────────────────────────

class IngredientListRenderTests(TestCase):
    """Content and empty-state rendering for the ingredient list page."""

    def setUp(self):
        self.user = _make_user('ing_render_admin', 'admin')
        self.client.force_login(self.user)

    def test_empty_state_rendered(self):
        response = self.client.get(INGREDIENT_LIST_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No ingredients yet')

    def test_ingredient_items_appear(self):
        _make_item('Coffee Powder', 'ingredient', 'g')
        response = self.client.get(INGREDIENT_LIST_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Coffee Powder')

    def test_packaging_items_excluded(self):
        """The page is ingredient-only: packaging must never appear here."""
        _make_item('Coffee Powder', 'ingredient', 'g')
        _make_item('Medium Cup', 'packaging', 'pcs')
        response = self.client.get(INGREDIENT_LIST_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Coffee Powder')
        self.assertNotContains(response, 'Medium Cup')

    def test_search_filters_results(self):
        _make_item('Oat Milk', 'ingredient', 'ml')
        _make_item('Sugar Syrup', 'ingredient', 'ml')
        response = self.client.get(INGREDIENT_LIST_URL, {'q': 'oat'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Oat Milk')
        self.assertNotContains(response, 'Sugar Syrup')

    def test_type_param_cannot_surface_packaging(self):
        """A manipulated ?type=packaging must not leak packaging items."""
        _make_item('Ground Coffee', 'ingredient', 'g')
        _make_item('Takeout Box', 'packaging', 'pcs')
        for params in ({'type': 'packaging'}, {'type': 'ingredient'}, {'type': ''}):
            response = self.client.get(INGREDIENT_LIST_URL, params)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'Ground Coffee')
            self.assertNotContains(response, 'Takeout Box')

    def test_search_is_scoped_to_ingredients(self):
        _make_item('Ground Coffee', 'ingredient', 'g')
        _make_item('Coffee Cup', 'packaging', 'pcs')
        response = self.client.get(INGREDIENT_LIST_URL, {'q': 'coffee'})
        self.assertContains(response, 'Ground Coffee')
        self.assertNotContains(response, 'Coffee Cup')

    def test_status_filter_active_only(self):
        _make_item('Active Item', 'ingredient', 'g')
        InventoryItem.objects.create(
            name='Inactive Item', item_type='ingredient', base_unit='g',
            is_active=False,
        )
        response = self.client.get(INGREDIENT_LIST_URL, {'status': 'active'})
        self.assertContains(response, 'Active Item')
        self.assertNotContains(response, 'Inactive Item')

    def test_status_filter_inactive_only(self):
        _make_item('Active Item', 'ingredient', 'g')
        InventoryItem.objects.create(
            name='Inactive Item', item_type='ingredient', base_unit='g',
            is_active=False,
        )
        response = self.client.get(INGREDIENT_LIST_URL, {'status': 'inactive'})
        self.assertNotContains(response, 'Active Item')
        self.assertContains(response, 'Inactive Item')

    def test_summary_counts_in_context(self):
        _make_item('Coffee', 'ingredient', 'g')
        _make_item('Cup', 'packaging', 'pcs')
        response = self.client.get(INGREDIENT_LIST_URL)
        self.assertEqual(response.context['total_count'], 1)
        self.assertNotIn('packaging_count', response.context)
        self.assertNotIn('ingredient_count', response.context)

    def test_low_stock_count_covers_ingredients_only(self):
        """Low Stock on this page must ignore packaging stock."""
        _make_item('Plenty Rice', 'ingredient', 'g', on_hand=Decimal('1000.000'))
        _make_item('Empty Rice', 'ingredient', 'g', on_hand=Decimal('0.000'))
        InventoryItem.objects.create(
            name='Empty Cup', item_type='packaging', base_unit='pcs',
            on_hand_quantity=Decimal('0.000'), low_stock_threshold=Decimal('10.000'),
        )
        response = self.client.get(INGREDIENT_LIST_URL)
        self.assertEqual(response.context['low_stock_count'], 1)

    def test_results_region_present(self):
        response = self.client.get(INGREDIENT_LIST_URL)
        self.assertContains(response, 'id="results-region"')

    def test_no_match_filtered_empty_state(self):
        _make_item('Milk', 'ingredient', 'ml')
        response = self.client.get(INGREDIENT_LIST_URL, {'q': 'xyz_no_match'})
        self.assertContains(response, 'No items match the current filters')


# ─────────────────────────────────────────────────────────────────────────────
# 3. Ingredient creation — authorization
# ─────────────────────────────────────────────────────────────────────────────

class IngredientCreateAuthTests(TestCase):
    """Role-based access control for the ingredient creation page."""

    def test_admin_can_access_form(self):
        admin = _make_user('ing_create_admin', 'admin')
        self.client.force_login(admin)
        response = self.client.get(INGREDIENT_CREATE_URL)
        self.assertEqual(response.status_code, 200)

    def test_cashier_can_access_form(self):
        cashier = _make_user('ing_create_cashier', 'cashier')
        self.client.force_login(cashier)
        response = self.client.get(INGREDIENT_CREATE_URL)
        self.assertEqual(response.status_code, 200)

    def test_kitchen_staff_cannot_access_form(self):
        kitchen = _make_user('ing_create_kitchen', 'kitchen_staff')
        self.client.force_login(kitchen)
        response = self.client.get(INGREDIENT_CREATE_URL)
        self.assertEqual(response.status_code, 302)

    def test_customer_cannot_access_form(self):
        customer = _make_user('ing_create_customer', 'customer')
        self.client.force_login(customer)
        response = self.client.get(INGREDIENT_CREATE_URL)
        self.assertEqual(response.status_code, 302)

    def test_unauthenticated_redirected_to_login(self):
        response = self.client.get(INGREDIENT_CREATE_URL)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])


# ─────────────────────────────────────────────────────────────────────────────
# 4. Ingredient creation — POST behavior
# ─────────────────────────────────────────────────────────────────────────────

class IngredientCreatePostTests(TestCase):
    """Functional tests for ingredient creation via POST."""

    def setUp(self):
        self.admin = _make_user('ing_post_admin', 'admin')
        self.cashier = _make_user('ing_post_cashier', 'cashier')

    def _valid_data(self, **overrides):
        data = {
            'name': 'Brown Sugar',
            'item_type': 'ingredient',
            'base_unit': 'g',
            'on_hand_quantity': '500.000',
            'low_stock_threshold': '100.000',
            'description': '',
            'is_active': 'on',
        }
        data.update(overrides)
        return data

    def test_admin_can_create_ingredient(self):
        self.client.force_login(self.admin)
        response = self.client.post(INGREDIENT_CREATE_URL, self._valid_data())
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, INGREDIENT_LIST_URL)
        self.assertEqual(InventoryItem.objects.filter(name='Brown Sugar').count(), 1)

    def test_cashier_can_create_ingredient(self):
        self.client.force_login(self.cashier)
        response = self.client.post(INGREDIENT_CREATE_URL, self._valid_data(
            name='Oat Milk', base_unit='ml',
        ))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(InventoryItem.objects.filter(name='Oat Milk').exists())

    def test_valid_data_creates_exactly_one_item(self):
        self.client.force_login(self.admin)
        self.client.post(INGREDIENT_CREATE_URL, self._valid_data(name='Unique Item'))
        self.assertEqual(InventoryItem.objects.filter(name='Unique Item').count(), 1)

    def test_item_fields_saved_correctly(self):
        self.client.force_login(self.admin)
        self.client.post(INGREDIENT_CREATE_URL, self._valid_data(
            name='Espresso Powder',
            item_type='ingredient',
            base_unit='g',
            on_hand_quantity='250.000',
            low_stock_threshold='50.000',
        ))
        item = InventoryItem.objects.get(name='Espresso Powder')
        self.assertEqual(item.item_type, 'ingredient')
        self.assertEqual(item.base_unit, 'g')
        self.assertEqual(item.on_hand_quantity, Decimal('250.000'))
        self.assertEqual(item.low_stock_threshold, Decimal('50.000'))
        self.assertTrue(item.is_active)

    def test_missing_name_rejected(self):
        self.client.force_login(self.admin)
        response = self.client.post(INGREDIENT_CREATE_URL, self._valid_data(name=''))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(InventoryItem.objects.exists())
        self.assertFormError(response, 'form', 'name', 'This field is required.')

    def test_item_type_is_not_a_form_field(self):
        """item_type is pinned server-side, so posting an empty value is irrelevant."""
        self.client.force_login(self.admin)
        response = self.client.post(INGREDIENT_CREATE_URL, self._valid_data(
            name='Pinned Ingredient', item_type='',
        ))
        self.assertEqual(response.status_code, 302)
        item = InventoryItem.objects.get(name='Pinned Ingredient')
        self.assertEqual(item.item_type, 'ingredient')

    def test_missing_base_unit_rejected(self):
        self.client.force_login(self.admin)
        response = self.client.post(INGREDIENT_CREATE_URL, self._valid_data(base_unit=''))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(InventoryItem.objects.exists())

    def test_negative_on_hand_rejected(self):
        self.client.force_login(self.admin)
        response = self.client.post(INGREDIENT_CREATE_URL, self._valid_data(
            on_hand_quantity='-1.000',
        ))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(InventoryItem.objects.exists())

    def test_duplicate_name_case_insensitive_rejected(self):
        # The model has a case-insensitive unique constraint on name.
        InventoryItem.objects.create(
            name='Whole Milk', item_type='ingredient', base_unit='ml',
        )
        self.client.force_login(self.admin)
        response = self.client.post(INGREDIENT_CREATE_URL, self._valid_data(
            name='whole milk', base_unit='ml',
        ))
        # Should redisplay the form, not create a second item.
        self.assertNotEqual(response.status_code, 302)
        self.assertEqual(InventoryItem.objects.filter(
            name__iexact='whole milk').count(), 1)

    def test_packaging_item_cannot_be_created_here(self):
        """Packaging must be created through Packaging Inventory, not here."""
        self.client.force_login(self.admin)
        response = self.client.post(INGREDIENT_CREATE_URL, self._valid_data(
            name='Medium Cup', item_type='packaging', base_unit='pcs',
            on_hand_quantity='200.000',
        ))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(InventoryItem.objects.filter(name='Medium Cup').exists())
        self.assertContains(response, 'only creates ingredient items')

    def test_inactive_item_can_be_created(self):
        self.client.force_login(self.admin)
        data = self._valid_data(name='Inactive Ingredient')
        data.pop('is_active', None)   # unchecked checkbox sends no value
        self.client.post(INGREDIENT_CREATE_URL, data)
        item = InventoryItem.objects.get(name='Inactive Ingredient')
        self.assertFalse(item.is_active)

    def test_reserved_quantity_starts_at_zero(self):
        """reserved_quantity is system-managed and must always start at 0."""
        self.client.force_login(self.admin)
        self.client.post(INGREDIENT_CREATE_URL, self._valid_data(name='Tea Leaves'))
        item = InventoryItem.objects.get(name='Tea Leaves')
        self.assertEqual(item.reserved_quantity, Decimal('0.000'))

    def test_success_message_shown(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            INGREDIENT_CREATE_URL, self._valid_data(name='Coconut Milk'),
            follow=True,
        )
        self.assertContains(response, 'Coconut Milk')

    def test_form_redisplayed_on_error(self):
        self.client.force_login(self.admin)
        response = self.client.post(INGREDIENT_CREATE_URL, self._valid_data(name=''))
        self.assertContains(response, 'Add Ingredient')


# ─────────────────────────────────────────────────────────────────────────────
# 5. Regression — existing legacy inventory unaffected
# ─────────────────────────────────────────────────────────────────────────────

class LegacyInventoryRegressionTests(TestCase):
    """
    Confirm that Phase 2B-A additions do not alter any existing legacy
    inventory behavior. The legacy product list, restock, and log views
    must behave exactly as before.
    """

    def setUp(self):
        self.admin = _make_user('reg_admin', 'admin')
        self.client.force_login(self.admin)
        self.category = Category.objects.create(name='Reg Drinks', slug='reg-drinks')
        self.product = Product.objects.create(
            category=self.category,
            inventory_tracking_mode='legacy',
            name='Reg Coffee',
            price=Decimal('60.00'),
            stock_quantity=50,
        )

    def test_legacy_inventory_list_still_accessible(self):
        # Phase 7B-3A: the page is retired — the URL now redirects to the
        # ingredient inventory, which does NOT list legacy products.
        response = self.client.get(reverse('inventory:list'))
        self.assertEqual(response.status_code, 302)
        followed = self.client.get(reverse('inventory:list'), follow=True)
        self.assertEqual(followed.status_code, 200)
        self.assertNotIn('Reg Coffee', followed.content.decode())

    def test_legacy_restock_still_works(self):
        # Phase 7B-3A: restocking is retired — the request must NOT mutate.
        url = reverse('inventory:restock', kwargs={'pk': self.product.pk})
        response = self.client.post(url, {'quantity': '10', 'notes': 'test'})
        self.assertEqual(response.status_code, 410)
        self.assertFalse(response.json()['success'])
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 50)

    def test_legacy_inventory_log_still_accessible(self):
        response = self.client.get(reverse('inventory:legacy_log'))
        self.assertEqual(response.status_code, 200)

    def test_inventory_log_record_unaffected(self):
        old_qty = self.product.stock_quantity
        InventoryLog.record(
            product=self.product,
            action='restock',
            source='manual_adjustment',
            reason='Regression test',
            quantity_change=5,
            quantity_before=old_qty,
            quantity_after=old_qty + 5,
        )
        self.assertEqual(InventoryLog.objects.count(), 1)

    def test_ingredient_and_legacy_logs_are_independent(self):
        """InventoryItem creation does not affect InventoryLog."""
        _make_item('Regression Ingredient', 'ingredient', 'g')
        self.assertEqual(InventoryLog.objects.count(), 0)
        self.assertEqual(InventoryItem.objects.count(), 1)


# ═════════════════════════════════════════════════════════════════════════════
# Phase 2B-B tests — stock adjustment and wastage recording
# ═════════════════════════════════════════════════════════════════════════════

from apps.inventory.models import IngredientMovementLog
from apps.inventory.services import (
    InventoryServiceError,
    StaleExpectedQuantityError,
    ReservedConflictError,
    ExceedsAvailableError,
)

_BB_PASSWORD = 'test-bb-pass-1'
ADJUST_URL_NAME  = 'inventory:ingredient_adjust'
WASTAGE_URL_NAME = 'inventory:ingredient_wastage'


def _bb_user(username, role):
    return CustomUser.objects.create_user(
        username=username, password=_BB_PASSWORD, role=role,
    )


def _bb_item(name='Coffee', item_type='ingredient', base_unit='g',
             on_hand=Decimal('1000.000'), reserved=Decimal('0.000')):
    return InventoryItem.objects.create(
        name=name,
        item_type=item_type,
        base_unit=base_unit,
        on_hand_quantity=on_hand,
        reserved_quantity=reserved,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 6. Adjustment — authorization
# ─────────────────────────────────────────────────────────────────────────────

class AdjustmentAuthTests(TestCase):
    """Role-based access control for the stock adjustment page."""

    def setUp(self):
        self.item = _bb_item()

    def _url(self):
        return reverse(ADJUST_URL_NAME, kwargs={'pk': self.item.pk})

    def test_admin_can_access(self):
        self.client.force_login(_bb_user('adj_admin', 'admin'))
        self.assertEqual(self.client.get(self._url()).status_code, 200)

    def test_cashier_can_access(self):
        self.client.force_login(_bb_user('adj_cashier', 'cashier'))
        self.assertEqual(self.client.get(self._url()).status_code, 200)

    def test_kitchen_staff_denied(self):
        self.client.force_login(_bb_user('adj_kitchen', 'kitchen_staff'))
        self.assertEqual(self.client.get(self._url()).status_code, 302)

    def test_customer_denied(self):
        self.client.force_login(_bb_user('adj_customer', 'customer'))
        self.assertEqual(self.client.get(self._url()).status_code, 302)

    def test_unauthenticated_redirected_to_login(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])


# ─────────────────────────────────────────────────────────────────────────────
# 7. Adjustment — POST behavior
# ─────────────────────────────────────────────────────────────────────────────

class AdjustmentPostTests(TestCase):
    """Functional tests for stock adjustment via POST."""

    def setUp(self):
        self.admin = _bb_user('adj_post_admin', 'admin')
        self.client.force_login(self.admin)
        self.item = _bb_item(name='Adj Coffee', on_hand=Decimal('1000.000'))

    def _url(self):
        return reverse(ADJUST_URL_NAME, kwargs={'pk': self.item.pk})

    def _valid_post(self, new_on_hand='1200.000', reason_code='stock_count_correction', note=''):
        return self.client.post(self._url(), {
            'new_on_hand': new_on_hand,
            'expected_on_hand': str(self.item.on_hand_quantity),
            'reason_code': reason_code,
            'note': note,
        })

    def test_valid_upward_adjustment_succeeds(self):
        response = self._valid_post('1200.000')
        self.assertRedirects(response, reverse('inventory:ingredient_list'))
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('1200.000'))

    def test_valid_downward_adjustment_succeeds(self):
        response = self._valid_post('800.000')
        self.assertRedirects(response, reverse('inventory:ingredient_list'))
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('800.000'))

    def test_cashier_can_adjust(self):
        cashier = _bb_user('adj_cash2', 'cashier')
        self.client.force_login(cashier)
        response = self._valid_post('950.000')
        self.assertRedirects(response, reverse('inventory:ingredient_list'))
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('950.000'))

    def test_adjustment_creates_movement_log(self):
        self._valid_post('1100.000')
        log = IngredientMovementLog.objects.get(inventory_item=self.item)
        self.assertEqual(log.movement_type, 'adjustment')
        self.assertEqual(log.on_hand_before, Decimal('1000.000'))
        self.assertEqual(log.on_hand_after, Decimal('1100.000'))
        self.assertEqual(log.on_hand_delta, Decimal('100.000'))
        self.assertEqual(log.reserved_delta, Decimal('0.000'))

    def test_movement_log_performer_recorded(self):
        self._valid_post('1100.000')
        log = IngredientMovementLog.objects.get(inventory_item=self.item)
        self.assertEqual(log.performed_by, self.admin)

    def test_movement_log_reason_code_saved(self):
        self._valid_post('1100.000', reason_code='found_unrecorded_stock')
        log = IngredientMovementLog.objects.get(inventory_item=self.item)
        self.assertEqual(log.reason_code, 'found_unrecorded_stock')

    def test_reserved_quantity_unchanged_after_adjustment(self):
        item_reserved = _bb_item(
            name='Reserved Item', on_hand=Decimal('500.000'),
            reserved=Decimal('100.000'),
        )
        self.client.post(reverse(ADJUST_URL_NAME, kwargs={'pk': item_reserved.pk}), {
            'new_on_hand': '600.000',
            'expected_on_hand': '500.000',
            'reason_code': 'stock_count_correction',
            'note': '',
        })
        item_reserved.refresh_from_db()
        self.assertEqual(item_reserved.reserved_quantity, Decimal('100.000'))
        self.assertEqual(item_reserved.on_hand_quantity, Decimal('600.000'))

    def test_no_op_adjustment_rejected(self):
        """Adjusting to the same value as current must be rejected."""
        response = self._valid_post('1000.000')  # same as current
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IngredientMovementLog.objects.count(), 0)

    def test_negative_new_on_hand_rejected(self):
        response = self._valid_post('-1.000')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IngredientMovementLog.objects.count(), 0)
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('1000.000'))

    def test_adjustment_below_reserved_rejected(self):
        item_r = _bb_item(
            name='Below Reserved', on_hand=Decimal('500.000'),
            reserved=Decimal('300.000'),
        )
        response = self.client.post(reverse(ADJUST_URL_NAME, kwargs={'pk': item_r.pk}), {
            'new_on_hand': '200.000',   # below reserved=300
            'expected_on_hand': '500.000',
            'reason_code': 'stock_count_correction',
            'note': '',
        })
        # Service raises ReservedConflictError → view re-renders form
        self.assertEqual(response.status_code, 200)
        item_r.refresh_from_db()
        self.assertEqual(item_r.on_hand_quantity, Decimal('500.000'))

    def test_missing_reason_code_rejected(self):
        response = self.client.post(self._url(), {
            'new_on_hand': '1100.000',
            'expected_on_hand': '1000.000',
            'reason_code': '',
            'note': '',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IngredientMovementLog.objects.count(), 0)

    def test_other_reason_without_note_rejected(self):
        response = self.client.post(self._url(), {
            'new_on_hand': '1100.000',
            'expected_on_hand': '1000.000',
            'reason_code': 'other',
            'note': '',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IngredientMovementLog.objects.count(), 0)

    def test_other_reason_with_note_accepted(self):
        response = self.client.post(self._url(), {
            'new_on_hand': '1100.000',
            'expected_on_hand': '1000.000',
            'reason_code': 'other',
            'note': 'Discrepancy after supplier delivery',
        })
        self.assertRedirects(response, reverse('inventory:ingredient_list'))

    def test_stale_expected_on_hand_is_rejected(self):
        """Submitting an expected_on_hand that no longer matches triggers a
        StaleExpectedQuantityError → the view redirects back with a warning
        rather than silently overwriting concurrent changes."""
        response = self.client.post(self._url(), {
            'new_on_hand': '1100.000',
            'expected_on_hand': '999.000',  # differs from actual 1000
            'reason_code': 'stock_count_correction',
            'note': '',
        })
        # StaleExpectedQuantityError → redirect back to adjust page
        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('1000.000'))
        self.assertEqual(IngredientMovementLog.objects.count(), 0)

    def test_failed_adjustment_produces_no_movement_log(self):
        """A service error must leave the movement log empty."""
        self._valid_post('-5.000')   # negative → rejected
        self.assertEqual(IngredientMovementLog.objects.count(), 0)

    def test_expected_on_hand_prefilled_on_get(self):
        """GET must render the form with expected_on_hand pre-filled."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f'value="{self.item.on_hand_quantity}"',
        )


# ─────────────────────────────────────────────────────────────────────────────
# 8. Wastage — authorization
# ─────────────────────────────────────────────────────────────────────────────

class WastageAuthTests(TestCase):
    """Role-based access control for the wastage recording page."""

    def setUp(self):
        self.item = _bb_item(name='Waste Auth Item')

    def _url(self):
        return reverse(WASTAGE_URL_NAME, kwargs={'pk': self.item.pk})

    def test_admin_can_access(self):
        self.client.force_login(_bb_user('wst_admin', 'admin'))
        self.assertEqual(self.client.get(self._url()).status_code, 200)

    def test_cashier_can_access(self):
        self.client.force_login(_bb_user('wst_cashier', 'cashier'))
        self.assertEqual(self.client.get(self._url()).status_code, 200)

    def test_kitchen_staff_denied(self):
        self.client.force_login(_bb_user('wst_kitchen', 'kitchen_staff'))
        self.assertEqual(self.client.get(self._url()).status_code, 302)

    def test_customer_denied(self):
        self.client.force_login(_bb_user('wst_customer', 'customer'))
        self.assertEqual(self.client.get(self._url()).status_code, 302)

    def test_unauthenticated_redirected_to_login(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])


# ─────────────────────────────────────────────────────────────────────────────
# 9. Wastage — POST behavior
# ─────────────────────────────────────────────────────────────────────────────

class WastagePostTests(TestCase):
    """Functional tests for wastage recording via POST."""

    def setUp(self):
        self.admin = _bb_user('wst_post_admin', 'admin')
        self.client.force_login(self.admin)
        # 1000 g on hand, 200 g reserved → 800 g available
        self.item = _bb_item(
            name='Wst Coffee',
            on_hand=Decimal('1000.000'),
            reserved=Decimal('200.000'),
        )

    def _url(self):
        return reverse(WASTAGE_URL_NAME, kwargs={'pk': self.item.pk})

    def _valid_post(self, quantity='100.000', reason_code='expired', note=''):
        return self.client.post(self._url(), {
            'quantity': quantity,
            'reason_code': reason_code,
            'note': note,
        })

    def test_valid_wastage_succeeds(self):
        response = self._valid_post('100.000')
        self.assertRedirects(response, reverse('inventory:ingredient_list'))
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('900.000'))

    def test_cashier_can_record_wastage(self):
        cashier = _bb_user('wst_cash2', 'cashier')
        self.client.force_login(cashier)
        response = self._valid_post('50.000')
        self.assertRedirects(response, reverse('inventory:ingredient_list'))
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('950.000'))

    def test_wastage_creates_movement_log(self):
        self._valid_post('100.000')
        log = IngredientMovementLog.objects.get(inventory_item=self.item)
        self.assertEqual(log.movement_type, 'wastage')
        self.assertEqual(log.on_hand_before, Decimal('1000.000'))
        self.assertEqual(log.on_hand_after, Decimal('900.000'))
        self.assertEqual(log.on_hand_delta, Decimal('-100.000'))
        self.assertEqual(log.reserved_delta, Decimal('0.000'))

    def test_movement_log_performer_recorded(self):
        self._valid_post('100.000')
        log = IngredientMovementLog.objects.get(inventory_item=self.item)
        self.assertEqual(log.performed_by, self.admin)

    def test_movement_log_reason_code_saved(self):
        self._valid_post('100.000', reason_code='contaminated')
        log = IngredientMovementLog.objects.get(inventory_item=self.item)
        self.assertEqual(log.reason_code, 'contaminated')

    def test_reserved_quantity_unchanged_after_wastage(self):
        """Wastage reduces on_hand only; reserved must remain untouched."""
        self._valid_post('100.000')
        self.item.refresh_from_db()
        self.assertEqual(self.item.reserved_quantity, Decimal('200.000'))

    def test_zero_quantity_rejected(self):
        response = self._valid_post('0.000')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IngredientMovementLog.objects.count(), 0)

    def test_negative_quantity_rejected(self):
        response = self._valid_post('-10.000')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IngredientMovementLog.objects.count(), 0)

    def test_wastage_exceeding_available_rejected(self):
        """Cannot waste more than available (on_hand - reserved = 800)."""
        response = self._valid_post('801.000')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IngredientMovementLog.objects.count(), 0)
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('1000.000'))

    def test_wastage_exactly_available_succeeds(self):
        """Wasting exactly the available amount (800) is allowed."""
        response = self._valid_post('800.000')
        self.assertRedirects(response, reverse('inventory:ingredient_list'))
        self.item.refresh_from_db()
        # on_hand: 1000 - 800 = 200 (equals reserved); reserved unchanged
        self.assertEqual(self.item.on_hand_quantity, Decimal('200.000'))
        self.assertEqual(self.item.reserved_quantity, Decimal('200.000'))

    def test_wastage_of_reserved_stock_rejected(self):
        """Reserved stock cannot be wasted through this workflow."""
        # Try to waste 900, but only 800 is available (200 reserved).
        response = self._valid_post('900.000')
        self.assertEqual(response.status_code, 200)
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('1000.000'))

    def test_missing_reason_code_rejected(self):
        response = self.client.post(self._url(), {
            'quantity': '100.000',
            'reason_code': '',
            'note': '',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IngredientMovementLog.objects.count(), 0)

    def test_other_reason_without_note_rejected(self):
        response = self.client.post(self._url(), {
            'quantity': '100.000',
            'reason_code': 'other',
            'note': '',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IngredientMovementLog.objects.count(), 0)

    def test_other_reason_with_note_accepted(self):
        response = self.client.post(self._url(), {
            'quantity': '100.000',
            'reason_code': 'other',
            'note': 'Power outage spoiled refrigerated items',
        })
        self.assertRedirects(response, reverse('inventory:ingredient_list'))

    def test_failed_wastage_produces_no_movement_log(self):
        self._valid_post('0.000')   # zero → rejected
        self.assertEqual(IngredientMovementLog.objects.count(), 0)

    def test_on_hand_unchanged_on_failed_wastage(self):
        self._valid_post('-5.000')
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('1000.000'))


# ─────────────────────────────────────────────────────────────────────────────
# 10. Phase 2B-B regression — sidebar and Phase 2B-A still work
# ─────────────────────────────────────────────────────────────────────────────

class Phase2BBRegressionTests(TestCase):
    """
    Confirm that Phase 2B-B additions, and the three audit cleanups,
    do not break Phase 2B-A or legacy inventory behavior.
    """

    def setUp(self):
        self.admin   = _bb_user('bb_reg_admin', 'admin')
        self.cashier = _bb_user('bb_reg_cashier', 'cashier')
        self.item    = _bb_item(name='Reg Ingredient', on_hand=Decimal('500.000'))
        self.client.force_login(self.admin)

        self.cat = Category.objects.create(name='BB Reg Drinks', slug='bb-reg-drinks')
        self.product = Product.objects.create(
            inventory_tracking_mode='legacy',
            category=self.cat, name='BB Reg Coffee',
            price=Decimal('60.00'), stock_quantity=50,
        )

    def test_ingredient_list_still_accessible(self):
        response = self.client.get(reverse('inventory:ingredient_list'))
        self.assertEqual(response.status_code, 200)

    def test_ingredient_create_still_works(self):
        response = self.client.get(reverse('inventory:ingredient_create'))
        self.assertEqual(response.status_code, 200)

    def test_legacy_inventory_list_still_accessible(self):
        # Phase 7B-3A: retired page — URL redirects to the ingredient list.
        response = self.client.get(reverse('inventory:list'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'],
                         reverse('inventory:ingredient_list'))

    def test_legacy_restock_still_works(self):
        # Phase 7B-3A: retired — must not mutate.
        url = reverse('inventory:restock', kwargs={'pk': self.product.pk})
        response = self.client.post(url, {'quantity': '5', 'notes': 'regression'})
        self.assertEqual(response.status_code, 410)
        self.assertFalse(response.json()['success'])
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 50)

    def test_legacy_log_still_accessible(self):
        response = self.client.get(reverse('inventory:legacy_log'))
        self.assertEqual(response.status_code, 200)

    def test_adjust_page_not_active_on_legacy_inventory_url(self):
        """Phase 7B-3A: the Stock Movements nav link must NOT be active on
        ingredient URLs (it activates only on the /inventory/log/ page)."""
        response = self.client.get(reverse('inventory:ingredient_list'))
        content  = response.content.decode()
        import re
        pattern = r'href="/inventory/log/"[^>]*class="[^"]*active[^"]*"'
        self.assertIsNone(
            re.search(pattern, content),
            'Stock Movements nav link should NOT be active on ingredient_list URL',
        )

    def test_ingredients_nav_active_on_adjust_page(self):
        """Ingredients nav link must be active when on the adjust page."""
        response = self.client.get(
            reverse('inventory:ingredient_adjust', kwargs={'pk': self.item.pk})
        )
        content = response.content.decode()
        import re
        # The Ingredients link href leads to /inventory/ingredients/
        pattern = r'href="/inventory/ingredients/"[^>]*class="[^"]*active[^"]*"'
        self.assertIsNotNone(
            re.search(pattern, content),
            'Ingredients nav link should be active on ingredient_adjust URL',
        )

    def test_adjustment_and_legacy_logs_independent(self):
        """An ingredient adjustment must not write to InventoryLog."""
        response = self.client.post(
            reverse('inventory:ingredient_adjust', kwargs={'pk': self.item.pk}),
            {
                'new_on_hand': '600.000',
                'expected_on_hand': '500.000',
                'reason_code': 'stock_count_correction',
                'note': '',
            },
        )
        self.assertRedirects(response, reverse('inventory:ingredient_list'))
        self.assertEqual(InventoryLog.objects.count(), 0)
        self.assertEqual(IngredientMovementLog.objects.count(), 1)


# ═════════════════════════════════════════════════════════════════════════════
# Phase 2B-C tests — packaging inventory management
# ═════════════════════════════════════════════════════════════════════════════

_PC_PASSWORD = 'test-pc-pass-1'
PKG_LIST_URL   = reverse('inventory:packaging_list')
PKG_CREATE_URL = reverse('inventory:packaging_create')
PKG_ADJUST_URL_NAME  = 'inventory:packaging_adjust'
PKG_WASTAGE_URL_NAME = 'inventory:packaging_wastage'


def _pc_user(username, role):
    return CustomUser.objects.create_user(
        username=username, password=_PC_PASSWORD, role=role,
    )


def _pkg_item(name='Medium Cup', base_unit='pcs',
              on_hand=Decimal('200.000'), reserved=Decimal('0.000')):
    """Create an InventoryItem with item_type='packaging'."""
    return InventoryItem.objects.create(
        name=name,
        item_type='packaging',
        base_unit=base_unit,
        on_hand_quantity=on_hand,
        reserved_quantity=reserved,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 11. Packaging list — authorization
# ─────────────────────────────────────────────────────────────────────────────

class PackagingListAuthTests(TestCase):
    """Role-based access for the packaging inventory list page."""

    def test_admin_can_access(self):
        self.client.force_login(_pc_user('pkg_list_admin', 'admin'))
        self.assertEqual(self.client.get(PKG_LIST_URL).status_code, 200)

    def test_cashier_can_access(self):
        self.client.force_login(_pc_user('pkg_list_cashier', 'cashier'))
        self.assertEqual(self.client.get(PKG_LIST_URL).status_code, 200)

    def test_kitchen_staff_denied(self):
        self.client.force_login(_pc_user('pkg_list_kitchen', 'kitchen_staff'))
        self.assertEqual(self.client.get(PKG_LIST_URL).status_code, 302)

    def test_customer_denied(self):
        self.client.force_login(_pc_user('pkg_list_customer', 'customer'))
        self.assertEqual(self.client.get(PKG_LIST_URL).status_code, 302)

    def test_unauthenticated_redirected_to_login(self):
        response = self.client.get(PKG_LIST_URL)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])


# ─────────────────────────────────────────────────────────────────────────────
# 12. Packaging list — content and classification
# ─────────────────────────────────────────────────────────────────────────────

class PackagingListRenderTests(TestCase):
    """Packaging list shows packaging items and excludes ingredient items."""

    def setUp(self):
        self.user = _pc_user('pkg_render_admin', 'admin')
        self.client.force_login(self.user)

    def test_empty_state_rendered(self):
        response = self.client.get(PKG_LIST_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No packaging items yet')

    def test_packaging_items_appear(self):
        _pkg_item('Medium Cup')
        _pkg_item('Lid')
        response = self.client.get(PKG_LIST_URL)
        self.assertContains(response, 'Medium Cup')
        self.assertContains(response, 'Lid')

    def test_ingredient_items_excluded(self):
        """Ingredient items must NOT appear on the packaging list page."""
        InventoryItem.objects.create(
            name='Coffee Powder', item_type='ingredient', base_unit='g',
        )
        _pkg_item('Straw')
        response = self.client.get(PKG_LIST_URL)
        self.assertContains(response, 'Straw')
        self.assertNotContains(response, 'Coffee Powder')

    def test_search_filter_works(self):
        _pkg_item('Paper Cup')
        _pkg_item('Plastic Lid')
        response = self.client.get(PKG_LIST_URL, {'q': 'cup'})
        self.assertContains(response, 'Paper Cup')
        self.assertNotContains(response, 'Plastic Lid')

    def test_status_filter_active(self):
        _pkg_item('Active Pkg')
        InventoryItem.objects.create(
            name='Inactive Pkg', item_type='packaging', base_unit='pcs',
            is_active=False,
        )
        response = self.client.get(PKG_LIST_URL, {'status': 'active'})
        self.assertContains(response, 'Active Pkg')
        self.assertNotContains(response, 'Inactive Pkg')

    def test_status_filter_inactive(self):
        _pkg_item('Active Pkg')
        InventoryItem.objects.create(
            name='Inactive Pkg', item_type='packaging', base_unit='pcs',
            is_active=False,
        )
        response = self.client.get(PKG_LIST_URL, {'status': 'inactive'})
        self.assertNotContains(response, 'Active Pkg')
        self.assertContains(response, 'Inactive Pkg')

    def test_summary_counts_in_context(self):
        _pkg_item('Cup A')
        _pkg_item('Cup B')
        response = self.client.get(PKG_LIST_URL)
        self.assertEqual(response.context['total_count'], 2)

    def test_summary_counts_ignore_ingredients(self):
        """Total / Low Stock / Inactive on this page must ignore ingredients."""
        _pkg_item('Plenty Cups', on_hand=Decimal('500.000'))
        _pkg_item('Empty Cups', on_hand=Decimal('0.000'))
        InventoryItem.objects.create(
            name='Empty Rice', item_type='ingredient', base_unit='g',
            on_hand_quantity=Decimal('0.000'),
            low_stock_threshold=Decimal('10.000'),
        )
        InventoryItem.objects.create(
            name='Inactive Rice', item_type='ingredient', base_unit='g',
            is_active=False,
        )
        response = self.client.get(PKG_LIST_URL)
        self.assertEqual(response.context['total_count'], 2)
        self.assertEqual(response.context['low_stock_count'], 1)
        self.assertEqual(response.context['inactive_count'], 0)

    def test_results_region_present(self):
        response = self.client.get(PKG_LIST_URL)
        self.assertContains(response, 'id="results-region"')

    def test_no_match_empty_state(self):
        _pkg_item('Known Cup')
        response = self.client.get(PKG_LIST_URL, {'q': 'xyz_no_match'})
        self.assertContains(response, 'No items match the current filters')


# ─────────────────────────────────────────────────────────────────────────────
# 13. Packaging creation — authorization
# ─────────────────────────────────────────────────────────────────────────────

class PackagingCreateAuthTests(TestCase):
    def test_admin_can_access_form(self):
        self.client.force_login(_pc_user('pkg_cr_admin', 'admin'))
        self.assertEqual(self.client.get(PKG_CREATE_URL).status_code, 200)

    def test_cashier_can_access_form(self):
        self.client.force_login(_pc_user('pkg_cr_cashier', 'cashier'))
        self.assertEqual(self.client.get(PKG_CREATE_URL).status_code, 200)

    def test_kitchen_staff_denied(self):
        self.client.force_login(_pc_user('pkg_cr_kitchen', 'kitchen_staff'))
        self.assertEqual(self.client.get(PKG_CREATE_URL).status_code, 302)

    def test_customer_denied(self):
        self.client.force_login(_pc_user('pkg_cr_customer', 'customer'))
        self.assertEqual(self.client.get(PKG_CREATE_URL).status_code, 302)

    def test_unauthenticated_redirected_to_login(self):
        response = self.client.get(PKG_CREATE_URL)
        self.assertIn('/login', response['Location'])


# ─────────────────────────────────────────────────────────────────────────────
# 14. Packaging creation — POST behavior
# ─────────────────────────────────────────────────────────────────────────────

class PackagingCreatePostTests(TestCase):

    def setUp(self):
        self.admin = _pc_user('pkg_post_admin', 'admin')
        self.client.force_login(self.admin)

    def _valid_data(self, **overrides):
        data = {
            'name': 'Takeout Box',
            'base_unit': 'pcs',
            'on_hand_quantity': '100.000',
            'low_stock_threshold': '20.000',
            'description': '',
            'is_active': 'on',
        }
        data.update(overrides)
        return data

    def test_admin_can_create(self):
        response = self.client.post(PKG_CREATE_URL, self._valid_data())
        self.assertRedirects(response, PKG_LIST_URL)
        self.assertTrue(InventoryItem.objects.filter(name='Takeout Box').exists())

    def test_cashier_can_create(self):
        cashier = _pc_user('pkg_cash_cr', 'cashier')
        self.client.force_login(cashier)
        self.client.post(PKG_CREATE_URL, self._valid_data(name='Paper Straw'))
        self.assertTrue(InventoryItem.objects.filter(name='Paper Straw').exists())

    def test_created_item_is_packaging(self):
        """item_type must always be 'packaging' regardless of submitted data."""
        self.client.post(PKG_CREATE_URL, self._valid_data(name='Secure Cup'))
        item = InventoryItem.objects.get(name='Secure Cup')
        self.assertEqual(item.item_type, 'packaging')

    def test_cannot_create_ingredient_via_packaging_endpoint(self):
        """Even if item_type=ingredient is injected in the POST body, the
        created record must still be packaging because the view forces it."""
        data = self._valid_data(name='Sneaky Item')
        data['item_type'] = 'ingredient'  # attacker-injected field
        self.client.post(PKG_CREATE_URL, data)
        item = InventoryItem.objects.filter(name='Sneaky Item').first()
        # Either the item was created as packaging, or not created at all —
        # but it must NOT be an ingredient.
        if item is not None:
            self.assertEqual(item.item_type, 'packaging',
                'item_type must remain packaging even if injected in POST')

    def test_reserved_quantity_starts_at_zero(self):
        self.client.post(PKG_CREATE_URL, self._valid_data(name='Zero Res Cup'))
        item = InventoryItem.objects.get(name='Zero Res Cup')
        self.assertEqual(item.reserved_quantity, Decimal('0.000'))

    def test_fields_saved_correctly(self):
        self.client.post(PKG_CREATE_URL, self._valid_data(
            name='Field Test Cup',
            base_unit='pcs',
            on_hand_quantity='50.000',
            low_stock_threshold='10.000',
        ))
        item = InventoryItem.objects.get(name='Field Test Cup')
        self.assertEqual(item.base_unit, 'pcs')
        self.assertEqual(item.on_hand_quantity, Decimal('50.000'))
        self.assertEqual(item.low_stock_threshold, Decimal('10.000'))

    def test_missing_name_rejected(self):
        response = self.client.post(PKG_CREATE_URL, self._valid_data(name=''))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(InventoryItem.objects.exists())

    def test_missing_base_unit_rejected(self):
        response = self.client.post(PKG_CREATE_URL, self._valid_data(base_unit=''))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(InventoryItem.objects.exists())

    def test_negative_on_hand_rejected(self):
        response = self.client.post(PKG_CREATE_URL, self._valid_data(
            on_hand_quantity='-1.000',
        ))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(InventoryItem.objects.exists())

    def test_inactive_item_can_be_created(self):
        data = self._valid_data(name='Inactive Box')
        data.pop('is_active', None)
        self.client.post(PKG_CREATE_URL, data)
        item = InventoryItem.objects.get(name='Inactive Box')
        self.assertFalse(item.is_active)

    def test_duplicate_name_rejected(self):
        InventoryItem.objects.create(
            name='Dup Cup', item_type='packaging', base_unit='pcs',
        )
        response = self.client.post(PKG_CREATE_URL, self._valid_data(name='dup cup'))
        self.assertNotEqual(response.status_code, 302)
        self.assertEqual(
            InventoryItem.objects.filter(name__iexact='dup cup').count(), 1
        )


# ─────────────────────────────────────────────────────────────────────────────
# 15. Packaging adjustment — authorization
# ─────────────────────────────────────────────────────────────────────────────

class PackagingAdjustAuthTests(TestCase):
    def setUp(self):
        self.item = _pkg_item('Adj Auth Cup')

    def _url(self):
        return reverse(PKG_ADJUST_URL_NAME, kwargs={'pk': self.item.pk})

    def test_admin_can_access(self):
        self.client.force_login(_pc_user('pkg_adj_admin', 'admin'))
        self.assertEqual(self.client.get(self._url()).status_code, 200)

    def test_cashier_can_access(self):
        self.client.force_login(_pc_user('pkg_adj_cashier', 'cashier'))
        self.assertEqual(self.client.get(self._url()).status_code, 200)

    def test_kitchen_staff_denied(self):
        self.client.force_login(_pc_user('pkg_adj_kitchen', 'kitchen_staff'))
        self.assertEqual(self.client.get(self._url()).status_code, 302)

    def test_customer_denied(self):
        self.client.force_login(_pc_user('pkg_adj_customer', 'customer'))
        self.assertEqual(self.client.get(self._url()).status_code, 302)

    def test_unauthenticated_redirected_to_login(self):
        response = self.client.get(self._url())
        self.assertIn('/login', response['Location'])

    def test_ingredient_item_returns_404(self):
        """packaging_adjust must return 404 for ingredient items."""
        ingredient = InventoryItem.objects.create(
            name='Sneaky Ingredient', item_type='ingredient', base_unit='g',
            on_hand_quantity=Decimal('100.000'),
        )
        self.client.force_login(_pc_user('pkg_adj_admin2', 'admin'))
        url = reverse(PKG_ADJUST_URL_NAME, kwargs={'pk': ingredient.pk})
        self.assertEqual(self.client.get(url).status_code, 404)


# ─────────────────────────────────────────────────────────────────────────────
# 16. Packaging adjustment — POST behavior
# ─────────────────────────────────────────────────────────────────────────────

class PackagingAdjustPostTests(TestCase):

    def setUp(self):
        self.admin = _pc_user('pkg_adj_post', 'admin')
        self.client.force_login(self.admin)
        self.item = _pkg_item('Adj Post Cup', on_hand=Decimal('200.000'))

    def _url(self):
        return reverse(PKG_ADJUST_URL_NAME, kwargs={'pk': self.item.pk})

    def _post(self, new_on_hand='250.000', reason='stock_count_correction', note=''):
        return self.client.post(self._url(), {
            'new_on_hand': new_on_hand,
            'expected_on_hand': str(self.item.on_hand_quantity),
            'reason_code': reason,
            'note': note,
        })

    def test_valid_upward_adjustment(self):
        response = self._post('250.000')
        self.assertRedirects(response, PKG_LIST_URL)
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('250.000'))

    def test_valid_downward_adjustment(self):
        response = self._post('150.000')
        self.assertRedirects(response, PKG_LIST_URL)
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('150.000'))

    def test_adjustment_creates_movement_log(self):
        self._post('250.000')
        log = IngredientMovementLog.objects.get(inventory_item=self.item)
        self.assertEqual(log.movement_type, 'adjustment')
        self.assertEqual(log.on_hand_before, Decimal('200.000'))
        self.assertEqual(log.on_hand_after, Decimal('250.000'))
        self.assertEqual(log.reserved_delta, Decimal('0.000'))

    def test_reserved_quantity_unchanged(self):
        item_r = _pkg_item('Reserved Cup', on_hand=Decimal('100.000'),
                           reserved=Decimal('30.000'))
        self.client.post(reverse(PKG_ADJUST_URL_NAME, kwargs={'pk': item_r.pk}), {
            'new_on_hand': '120.000',
            'expected_on_hand': '100.000',
            'reason_code': 'stock_count_correction',
            'note': '',
        })
        item_r.refresh_from_db()
        self.assertEqual(item_r.reserved_quantity, Decimal('30.000'))

    def test_adjustment_below_reserved_rejected(self):
        item_r = _pkg_item('Res Cup', on_hand=Decimal('100.000'),
                           reserved=Decimal('50.000'))
        response = self.client.post(reverse(PKG_ADJUST_URL_NAME, kwargs={'pk': item_r.pk}), {
            'new_on_hand': '30.000',
            'expected_on_hand': '100.000',
            'reason_code': 'stock_count_correction',
            'note': '',
        })
        self.assertEqual(response.status_code, 200)
        item_r.refresh_from_db()
        self.assertEqual(item_r.on_hand_quantity, Decimal('100.000'))

    def test_stale_expected_on_hand_rejected(self):
        response = self.client.post(self._url(), {
            'new_on_hand': '250.000',
            'expected_on_hand': '999.000',  # wrong
            'reason_code': 'stock_count_correction',
            'note': '',
        })
        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('200.000'))
        self.assertEqual(IngredientMovementLog.objects.count(), 0)

    def test_no_op_adjustment_rejected(self):
        response = self._post('200.000')  # same as current
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IngredientMovementLog.objects.count(), 0)

    def test_negative_new_on_hand_rejected(self):
        response = self._post('-1.000')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IngredientMovementLog.objects.count(), 0)

    def test_missing_reason_code_rejected(self):
        response = self.client.post(self._url(), {
            'new_on_hand': '250.000',
            'expected_on_hand': '200.000',
            'reason_code': '',
            'note': '',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IngredientMovementLog.objects.count(), 0)

    def test_failed_adjustment_creates_no_movement_log(self):
        self._post('-5.000')
        self.assertEqual(IngredientMovementLog.objects.count(), 0)


# ─────────────────────────────────────────────────────────────────────────────
# 17. Packaging wastage — authorization
# ─────────────────────────────────────────────────────────────────────────────

class PackagingWastageAuthTests(TestCase):
    def setUp(self):
        self.item = _pkg_item('Waste Auth Cup')

    def _url(self):
        return reverse(PKG_WASTAGE_URL_NAME, kwargs={'pk': self.item.pk})

    def test_admin_can_access(self):
        self.client.force_login(_pc_user('pkg_wst_admin', 'admin'))
        self.assertEqual(self.client.get(self._url()).status_code, 200)

    def test_cashier_can_access(self):
        self.client.force_login(_pc_user('pkg_wst_cashier', 'cashier'))
        self.assertEqual(self.client.get(self._url()).status_code, 200)

    def test_kitchen_staff_denied(self):
        self.client.force_login(_pc_user('pkg_wst_kitchen', 'kitchen_staff'))
        self.assertEqual(self.client.get(self._url()).status_code, 302)

    def test_customer_denied(self):
        self.client.force_login(_pc_user('pkg_wst_customer', 'customer'))
        self.assertEqual(self.client.get(self._url()).status_code, 302)

    def test_unauthenticated_redirected_to_login(self):
        response = self.client.get(self._url())
        self.assertIn('/login', response['Location'])

    def test_ingredient_item_returns_404(self):
        ingredient = InventoryItem.objects.create(
            name='Sneaky Ing 2', item_type='ingredient', base_unit='g',
            on_hand_quantity=Decimal('100.000'),
        )
        self.client.force_login(_pc_user('pkg_wst_admin2', 'admin'))
        url = reverse(PKG_WASTAGE_URL_NAME, kwargs={'pk': ingredient.pk})
        self.assertEqual(self.client.get(url).status_code, 404)


# ─────────────────────────────────────────────────────────────────────────────
# 18. Packaging wastage — POST behavior
# ─────────────────────────────────────────────────────────────────────────────

class PackagingWastagePostTests(TestCase):

    def setUp(self):
        self.admin = _pc_user('pkg_wst_post', 'admin')
        self.client.force_login(self.admin)
        # 200 pcs on hand, 50 pcs reserved → 150 available
        self.item = _pkg_item('Wst Cup', on_hand=Decimal('200.000'),
                              reserved=Decimal('50.000'))

    def _url(self):
        return reverse(PKG_WASTAGE_URL_NAME, kwargs={'pk': self.item.pk})

    def _post(self, qty='20.000', reason='damaged', note=''):
        return self.client.post(self._url(), {
            'quantity': qty, 'reason_code': reason, 'note': note,
        })

    def test_valid_wastage_succeeds(self):
        response = self._post('20.000')
        self.assertRedirects(response, PKG_LIST_URL)
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('180.000'))

    def test_wastage_creates_movement_log(self):
        self._post('20.000')
        log = IngredientMovementLog.objects.get(inventory_item=self.item)
        self.assertEqual(log.movement_type, 'wastage')
        self.assertEqual(log.on_hand_delta, Decimal('-20.000'))
        self.assertEqual(log.reserved_delta, Decimal('0.000'))

    def test_reserved_quantity_unchanged(self):
        self._post('20.000')
        self.item.refresh_from_db()
        self.assertEqual(self.item.reserved_quantity, Decimal('50.000'))

    def test_zero_quantity_rejected(self):
        response = self._post('0.000')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IngredientMovementLog.objects.count(), 0)

    def test_negative_quantity_rejected(self):
        response = self._post('-5.000')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IngredientMovementLog.objects.count(), 0)

    def test_wastage_exceeding_available_rejected(self):
        # available = 200 - 50 = 150; trying to waste 151
        response = self._post('151.000')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IngredientMovementLog.objects.count(), 0)
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('200.000'))

    def test_wastage_exactly_available_succeeds(self):
        response = self._post('150.000')
        self.assertRedirects(response, PKG_LIST_URL)
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('50.000'))
        self.assertEqual(self.item.reserved_quantity, Decimal('50.000'))

    def test_reserved_stock_protected(self):
        # Try to waste 180 (> 150 available)
        response = self._post('180.000')
        self.assertEqual(response.status_code, 200)
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('200.000'))

    def test_missing_reason_rejected(self):
        response = self.client.post(self._url(), {
            'quantity': '20.000', 'reason_code': '', 'note': '',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IngredientMovementLog.objects.count(), 0)

    def test_failed_wastage_no_movement_log(self):
        self._post('0.000')
        self.assertEqual(IngredientMovementLog.objects.count(), 0)


# ─────────────────────────────────────────────────────────────────────────────
# 19. Phase 2B-C regression — Phase 2B-A and 2B-B still work
# ─────────────────────────────────────────────────────────────────────────────

class Phase2BCRegressionTests(TestCase):
    """Phase 2B-C must not break Phase 2B-A/B or legacy inventory."""

    def setUp(self):
        self.admin   = _pc_user('pc_reg_admin', 'admin')
        self.client.force_login(self.admin)
        self.pkg_item = _pkg_item('Reg Cup', on_hand=Decimal('100.000'))
        self.ing_item = InventoryItem.objects.create(
            name='Reg Coffee', item_type='ingredient', base_unit='g',
            on_hand_quantity=Decimal('500.000'),
        )
        cat = Category.objects.create(name='PC Reg Drinks', slug='pc-reg-drinks')
        self.product = Product.objects.create(
            inventory_tracking_mode='legacy',
            category=cat, name='PC Reg Coffee',
            price=Decimal('60.00'), stock_quantity=30,
        )

    def test_ingredient_list_still_accessible(self):
        response = self.client.get(reverse('inventory:ingredient_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Reg Coffee')

    def test_ingredient_list_does_not_show_packaging(self):
        """ingredient_list is the ingredient-only page: packaging is excluded
        at the queryset level, and the packaging_list page is the only place
        packaging materials are listed."""
        response = self.client.get(reverse('inventory:ingredient_list'))
        self.assertContains(response, 'Reg Coffee')
        self.assertNotContains(response, 'Reg Cup')

    def test_packaging_list_does_not_show_ingredients(self):
        """packaging_list must not leak ingredient items."""
        response = self.client.get(PKG_LIST_URL)
        self.assertNotContains(response, 'Reg Coffee')

    def test_ingredient_create_still_works(self):
        self.assertEqual(
            self.client.get(reverse('inventory:ingredient_create')).status_code, 200
        )

    def test_ingredient_adjust_still_works(self):
        url = reverse('inventory:ingredient_adjust', kwargs={'pk': self.ing_item.pk})
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_ingredient_wastage_still_works(self):
        url = reverse('inventory:ingredient_wastage', kwargs={'pk': self.ing_item.pk})
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_legacy_inventory_list_still_accessible(self):
        # Phase 7B-3A: retired page — the URL redirects to ingredients.
        response = self.client.get(reverse('inventory:list'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'],
                         reverse('inventory:ingredient_list'))

    def test_legacy_restock_still_works(self):
        # Phase 7B-3A: retired — must not mutate (setUp baseline is 30).
        url = reverse('inventory:restock', kwargs={'pk': self.product.pk})
        response = self.client.post(url, {'quantity': '5', 'notes': 'regression'})
        self.assertEqual(response.status_code, 410)
        self.assertFalse(response.json()['success'])
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 30)

    def test_packaging_and_legacy_logs_independent(self):
        """A packaging adjustment must not write to InventoryLog."""
        self.client.post(
            reverse(PKG_ADJUST_URL_NAME, kwargs={'pk': self.pkg_item.pk}), {
                'new_on_hand': '120.000',
                'expected_on_hand': '100.000',
                'reason_code': 'stock_count_correction',
                'note': '',
            }
        )
        self.assertEqual(InventoryLog.objects.count(), 0)
        self.assertEqual(IngredientMovementLog.objects.count(), 1)

    def test_nav_active_state_no_collision(self):
        """Packaging nav must be active and Ingredients nav inactive on packaging pages."""
        response = self.client.get(PKG_LIST_URL)
        content  = response.content.decode()
        import re
        # Packaging link should be active
        pkg_active = re.search(
            r'href="/inventory/packaging/"[^>]*class="[^"]*active[^"]*"', content
        )
        self.assertIsNotNone(pkg_active,
            'Packaging nav link should be active on packaging_list URL')
        # Ingredient link should NOT be active
        ing_active = re.search(
            r'href="/inventory/ingredients/"[^>]*class="[^"]*active[^"]*"', content
        )
        self.assertIsNone(ing_active,
            'Ingredients nav link should NOT be active on packaging_list URL')


# ═════════════════════════════════════════════════════════════════════════════
# Phase 2B-D tests — integration, type isolation, and final regression
# ═════════════════════════════════════════════════════════════════════════════

_PD_PASSWORD = 'test-pd-pass-1'


def _pd_user(username, role):
    return CustomUser.objects.create_user(
        username=username, password=_PD_PASSWORD, role=role,
    )


def _pd_ingredient(name='PD Coffee', base_unit='g',
                   on_hand=Decimal('500.000'), reserved=Decimal('0.000')):
    return InventoryItem.objects.create(
        name=name, item_type='ingredient', base_unit=base_unit,
        on_hand_quantity=on_hand, reserved_quantity=reserved,
    )


def _pd_packaging(name='PD Cup', base_unit='pcs',
                  on_hand=Decimal('100.000'), reserved=Decimal('0.000')):
    return InventoryItem.objects.create(
        name=name, item_type='packaging', base_unit=base_unit,
        on_hand_quantity=on_hand, reserved_quantity=reserved,
    )


# ─────────────────────────────────────────────────────────────────────────────
# M1 fix: ingredient adjust/wastage endpoints now enforce item_type=ingredient
# ─────────────────────────────────────────────────────────────────────────────

class IngredientTypeIsolationTests(TestCase):
    """
    ingredient_adjust and ingredient_record_wastage must:
    - Accept ingredient item PKs (200 / redirect).
    - Return 404 for packaging item PKs.
    Mirrors the isolation already present on packaging endpoints.
    """

    def setUp(self):
        self.admin = _pd_user('pd_iso_admin', 'admin')
        self.client.force_login(self.admin)
        self.ingredient = _pd_ingredient('ISO Coffee')
        self.packaging  = _pd_packaging('ISO Cup')

    # ── ingredient_adjust ─────────────────────────────────────────────────

    def test_ingredient_adjust_accepts_ingredient_pk(self):
        url = reverse('inventory:ingredient_adjust', kwargs={'pk': self.ingredient.pk})
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_ingredient_adjust_returns_404_for_packaging_pk(self):
        url = reverse('inventory:ingredient_adjust', kwargs={'pk': self.packaging.pk})
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_ingredient_adjust_post_accepts_ingredient(self):
        url = reverse('inventory:ingredient_adjust', kwargs={'pk': self.ingredient.pk})
        response = self.client.post(url, {
            'new_on_hand': '600.000',
            'expected_on_hand': '500.000',
            'reason_code': 'stock_count_correction',
            'note': '',
        })
        self.assertRedirects(response, reverse('inventory:ingredient_list'))
        self.ingredient.refresh_from_db()
        self.assertEqual(self.ingredient.on_hand_quantity, Decimal('600.000'))

    def test_ingredient_adjust_post_returns_404_for_packaging(self):
        url = reverse('inventory:ingredient_adjust', kwargs={'pk': self.packaging.pk})
        response = self.client.post(url, {
            'new_on_hand': '150.000',
            'expected_on_hand': '100.000',
            'reason_code': 'stock_count_correction',
            'note': '',
        })
        self.assertEqual(response.status_code, 404)
        self.packaging.refresh_from_db()
        self.assertEqual(self.packaging.on_hand_quantity, Decimal('100.000'))

    # ── ingredient_record_wastage ─────────────────────────────────────────

    def test_ingredient_wastage_accepts_ingredient_pk(self):
        url = reverse('inventory:ingredient_wastage', kwargs={'pk': self.ingredient.pk})
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_ingredient_wastage_returns_404_for_packaging_pk(self):
        url = reverse('inventory:ingredient_wastage', kwargs={'pk': self.packaging.pk})
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_ingredient_wastage_post_accepts_ingredient(self):
        url = reverse('inventory:ingredient_wastage', kwargs={'pk': self.ingredient.pk})
        response = self.client.post(url, {
            'quantity': '50.000',
            'reason_code': 'expired',
            'note': '',
        })
        self.assertRedirects(response, reverse('inventory:ingredient_list'))
        self.ingredient.refresh_from_db()
        self.assertEqual(self.ingredient.on_hand_quantity, Decimal('450.000'))

    def test_ingredient_wastage_post_returns_404_for_packaging(self):
        url = reverse('inventory:ingredient_wastage', kwargs={'pk': self.packaging.pk})
        response = self.client.post(url, {
            'quantity': '10.000',
            'reason_code': 'damaged',
            'note': '',
        })
        self.assertEqual(response.status_code, 404)
        self.packaging.refresh_from_db()
        self.assertEqual(self.packaging.on_hand_quantity, Decimal('100.000'))

    def test_ingredient_adjust_existing_behavior_unchanged(self):
        """Verify the type filter does not break normal ingredient adjustment."""
        url = reverse('inventory:ingredient_adjust', kwargs={'pk': self.ingredient.pk})
        self.client.post(url, {
            'new_on_hand': '750.000',
            'expected_on_hand': '500.000',
            'reason_code': 'found_unrecorded_stock',
            'note': '',
        })
        log = IngredientMovementLog.objects.get(inventory_item=self.ingredient)
        self.assertEqual(log.movement_type, 'adjustment')
        self.assertEqual(log.on_hand_after, Decimal('750.000'))

    def test_ingredient_wastage_existing_behavior_unchanged(self):
        """Verify the type filter does not break normal ingredient wastage."""
        url = reverse('inventory:ingredient_wastage', kwargs={'pk': self.ingredient.pk})
        self.client.post(url, {
            'quantity': '100.000',
            'reason_code': 'spilled',
            'note': '',
        })
        log = IngredientMovementLog.objects.get(inventory_item=self.ingredient)
        self.assertEqual(log.movement_type, 'wastage')
        self.assertEqual(log.on_hand_after, Decimal('400.000'))


# ─────────────────────────────────────────────────────────────────────────────
# Each creation endpoint is locked to its own item type
# ─────────────────────────────────────────────────────────────────────────────

class TypeLockedCreationTests(TestCase):
    """
    ingredient_create creates ingredients only and packaging_create creates
    packaging only. Neither endpoint exposes item_type, and a submitted value
    of the wrong type is rejected rather than silently coerced.
    """

    def setUp(self):
        self.admin = _pd_user('pd_m2_admin', 'admin')
        self.client.force_login(self.admin)

    def test_ingredient_form_can_create_ingredient(self):
        response = self.client.post(reverse('inventory:ingredient_create'), {
            'name': 'M2 Ingredient',
            'base_unit': 'g',
            'on_hand_quantity': '100.000',
            'low_stock_threshold': '10.000',
            'description': '',
            'is_active': 'on',
        })
        self.assertRedirects(response, reverse('inventory:ingredient_list'))
        item = InventoryItem.objects.get(name='M2 Ingredient')
        self.assertEqual(item.item_type, 'ingredient')

    def test_ingredient_form_rejects_packaging_item_type(self):
        """A manipulated POST cannot create a packaging item here."""
        response = self.client.post(reverse('inventory:ingredient_create'), {
            'name': 'M2 Packaging',
            'item_type': 'packaging',
            'base_unit': 'pcs',
            'on_hand_quantity': '50.000',
            'low_stock_threshold': '5.000',
            'description': '',
            'is_active': 'on',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(InventoryItem.objects.filter(name='M2 Packaging').exists())
        self.assertContains(response, 'only creates ingredient items')

    def test_ingredient_form_does_not_expose_item_type(self):
        response = self.client.get(reverse('inventory:ingredient_create'))
        self.assertNotIn('item_type', response.context['form'].fields)
        self.assertNotContains(response, 'name="item_type"')

    def test_ingredient_created_without_item_type_field(self):
        """Omitting item_type entirely (as the UI does) still yields an ingredient."""
        self.client.post(reverse('inventory:ingredient_create'), {
            'name': 'M2 Default Type',
            'base_unit': 'ml',
            'on_hand_quantity': '10.000',
            'low_stock_threshold': '0.000',
        })
        item = InventoryItem.objects.get(name='M2 Default Type')
        self.assertEqual(item.item_type, 'ingredient')

    def test_packaging_form_rejects_ingredient_item_type(self):
        response = self.client.post(reverse('inventory:packaging_create'), {
            'name': 'M2 Sneaky Ingredient',
            'item_type': 'ingredient',
            'base_unit': 'g',
            'on_hand_quantity': '20.000',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            InventoryItem.objects.filter(name='M2 Sneaky Ingredient').exists()
        )
        self.assertContains(response, 'only creates packaging items')

    def test_packaging_create_endpoint_still_forces_packaging_type(self):
        """The dedicated packaging endpoint always creates packaging, regardless."""
        self.client.post(reverse('inventory:packaging_create'), {
            'name': 'M2 Forced Cup',
            'base_unit': 'pcs',
            'on_hand_quantity': '20.000',
            'low_stock_threshold': '2.000',
            'description': '',
            'is_active': 'on',
        })
        item = InventoryItem.objects.get(name='M2 Forced Cup')
        self.assertEqual(item.item_type, 'packaging')

    def test_packaging_adjust_still_rejects_ingredient_pk(self):
        """Cross-check: dedicated packaging adjust/wastage type filter unaffected."""
        ingredient = _pd_ingredient('M2 Cross Ingredient')
        url = reverse('inventory:packaging_adjust', kwargs={'pk': ingredient.pk})
        self.assertEqual(self.client.get(url).status_code, 404)


# ─────────────────────────────────────────────────────────────────────────────
# Integration: combined overview, correct listing page membership
# ─────────────────────────────────────────────────────────────────────────────

class IntegrationOverviewTests(TestCase):
    """
    Cross-feature integration: ingredient_list shows ingredients only and
    packaging_list shows packaging only; each page's adjust/wastage actions
    stay scoped to their own item type.
    """

    def setUp(self):
        self.admin = _pd_user('pd_int_admin', 'admin')
        self.client.force_login(self.admin)
        self.ingredient = _pd_ingredient('Int Coffee', on_hand=Decimal('300.000'))
        self.packaging  = _pd_packaging('Int Cup', on_hand=Decimal('50.000'))

    def test_ingredient_list_shows_ingredients_only(self):
        response = self.client.get(reverse('inventory:ingredient_list'))
        self.assertContains(response, 'Int Coffee')
        self.assertNotContains(response, 'Int Cup')

    def test_packaging_list_shows_packaging_only(self):
        response = self.client.get(reverse('inventory:packaging_list'))
        self.assertContains(response, 'Int Cup')
        self.assertNotContains(response, 'Int Coffee')

    def test_ingredient_adjust_action_reaches_ingredient(self):
        url = reverse('inventory:ingredient_adjust', kwargs={'pk': self.ingredient.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Int Coffee')

    def test_packaging_adjust_action_reaches_packaging(self):
        url = reverse('inventory:packaging_adjust', kwargs={'pk': self.packaging.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Int Cup')

    def test_adjustment_on_ingredient_does_not_affect_packaging(self):
        adj_url = reverse('inventory:ingredient_adjust', kwargs={'pk': self.ingredient.pk})
        self.client.post(adj_url, {
            'new_on_hand': '350.000',
            'expected_on_hand': '300.000',
            'reason_code': 'stock_count_correction',
            'note': '',
        })
        self.packaging.refresh_from_db()
        self.assertEqual(self.packaging.on_hand_quantity, Decimal('50.000'),
            'Packaging stock must not be affected by ingredient adjustment')

    def test_adjustment_on_packaging_does_not_affect_ingredient(self):
        adj_url = reverse('inventory:packaging_adjust', kwargs={'pk': self.packaging.pk})
        self.client.post(adj_url, {
            'new_on_hand': '80.000',
            'expected_on_hand': '50.000',
            'reason_code': 'stock_count_correction',
            'note': '',
        })
        self.ingredient.refresh_from_db()
        self.assertEqual(self.ingredient.on_hand_quantity, Decimal('300.000'),
            'Ingredient stock must not be affected by packaging adjustment')

    def test_movement_logs_correctly_identify_items(self):
        """Each adjustment produces a log row identifying the correct item."""
        # Adjust ingredient
        self.client.post(
            reverse('inventory:ingredient_adjust', kwargs={'pk': self.ingredient.pk}), {
                'new_on_hand': '310.000', 'expected_on_hand': '300.000',
                'reason_code': 'stock_count_correction', 'note': '',
            }
        )
        # Adjust packaging
        self.client.post(
            reverse('inventory:packaging_adjust', kwargs={'pk': self.packaging.pk}), {
                'new_on_hand': '60.000', 'expected_on_hand': '50.000',
                'reason_code': 'stock_count_correction', 'note': '',
            }
        )
        ing_log = IngredientMovementLog.objects.get(inventory_item=self.ingredient)
        pkg_log = IngredientMovementLog.objects.get(inventory_item=self.packaging)
        self.assertEqual(ing_log.inventory_item_id, self.ingredient.pk)
        self.assertEqual(pkg_log.inventory_item_id, self.packaging.pk)
        self.assertEqual(IngredientMovementLog.objects.count(), 2)

    def test_legacy_inventory_log_remains_independent(self):
        """IngredientMovementLog operations must not write to InventoryLog."""
        self.client.post(
            reverse('inventory:ingredient_adjust', kwargs={'pk': self.ingredient.pk}), {
                'new_on_hand': '310.000', 'expected_on_hand': '300.000',
                'reason_code': 'stock_count_correction', 'note': '',
            }
        )
        self.assertEqual(InventoryLog.objects.count(), 0)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2B-D full regression
# ─────────────────────────────────────────────────────────────────────────────

class Phase2BDRegressionTests(TestCase):
    """Final regression confirming all Phase 2B-A/B/C features remain intact
    after the M1 type-filter fix."""

    def setUp(self):
        self.admin = _pd_user('pd_reg_admin', 'admin')
        self.client.force_login(self.admin)
        self.ingredient = _pd_ingredient('Reg Ingredient')
        self.packaging  = _pd_packaging('Reg Cup')
        cat = Category.objects.create(name='PD Reg Cat', slug='pd-reg-cat')
        self.product = Product.objects.create(
            inventory_tracking_mode='legacy',
            category=cat, name='PD Reg Coffee',
            price=Decimal('60.00'), stock_quantity=20,
        )

    # 2B-A regression
    def test_ingredient_list_accessible(self):
        self.assertEqual(
            self.client.get(reverse('inventory:ingredient_list')).status_code, 200
        )

    def test_ingredient_create_accessible(self):
        self.assertEqual(
            self.client.get(reverse('inventory:ingredient_create')).status_code, 200
        )

    # 2B-B regression
    def test_ingredient_adjust_accessible(self):
        url = reverse('inventory:ingredient_adjust', kwargs={'pk': self.ingredient.pk})
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_ingredient_wastage_accessible(self):
        url = reverse('inventory:ingredient_wastage', kwargs={'pk': self.ingredient.pk})
        self.assertEqual(self.client.get(url).status_code, 200)

    # 2B-C regression
    def test_packaging_list_accessible(self):
        self.assertEqual(
            self.client.get(reverse('inventory:packaging_list')).status_code, 200
        )

    def test_packaging_create_accessible(self):
        self.assertEqual(
            self.client.get(reverse('inventory:packaging_create')).status_code, 200
        )

    def test_packaging_adjust_accessible(self):
        url = reverse('inventory:packaging_adjust', kwargs={'pk': self.packaging.pk})
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_packaging_wastage_accessible(self):
        url = reverse('inventory:packaging_wastage', kwargs={'pk': self.packaging.pk})
        self.assertEqual(self.client.get(url).status_code, 200)

    # Legacy regression
    def test_legacy_inventory_list_accessible(self):
        # Phase 7B-3A: retired page — the URL redirects to ingredients.
        response = self.client.get(reverse('inventory:list'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'],
                         reverse('inventory:ingredient_list'))

    def test_legacy_restock_still_works(self):
        # Phase 7B-3A: retired — must not mutate (setUp baseline is 20).
        url = reverse('inventory:restock', kwargs={'pk': self.product.pk})
        response = self.client.post(url, {'quantity': '5', 'notes': 'pd regression'})
        self.assertEqual(response.status_code, 410)
        self.assertFalse(response.json()['success'])
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 20)

    def test_legacy_log_accessible(self):
        self.assertEqual(
            self.client.get(reverse('inventory:legacy_log')).status_code, 200
        )

    # Cashier access for all Phase 2B routes
    def test_cashier_can_access_all_2b_routes(self):
        cashier = _pd_user('pd_cashier_all', 'cashier')
        self.client.force_login(cashier)
        routes = [
            reverse('inventory:ingredient_list'),
            reverse('inventory:ingredient_create'),
            reverse('inventory:ingredient_adjust', kwargs={'pk': self.ingredient.pk}),
            reverse('inventory:ingredient_wastage', kwargs={'pk': self.ingredient.pk}),
            reverse('inventory:packaging_list'),
            reverse('inventory:packaging_create'),
            reverse('inventory:packaging_adjust', kwargs={'pk': self.packaging.pk}),
            reverse('inventory:packaging_wastage', kwargs={'pk': self.packaging.pk}),
        ]
        for url in routes:
            self.assertEqual(self.client.get(url).status_code, 200,
                f'Cashier should have access to {url}')

    def test_kitchen_staff_denied_all_2b_routes(self):
        kitchen = _pd_user('pd_kitchen_all', 'kitchen_staff')
        self.client.force_login(kitchen)
        routes = [
            reverse('inventory:ingredient_list'),
            reverse('inventory:ingredient_create'),
            reverse('inventory:ingredient_adjust', kwargs={'pk': self.ingredient.pk}),
            reverse('inventory:ingredient_wastage', kwargs={'pk': self.ingredient.pk}),
            reverse('inventory:packaging_list'),
            reverse('inventory:packaging_create'),
            reverse('inventory:packaging_adjust', kwargs={'pk': self.packaging.pk}),
            reverse('inventory:packaging_wastage', kwargs={'pk': self.packaging.pk}),
        ]
        for url in routes:
            self.assertEqual(self.client.get(url).status_code, 302,
                f'Kitchen staff should be denied {url}')
