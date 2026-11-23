"""
Phase 8F-2 — audit trail & Django Admin hardening tests.

Covers the two pre-cutover weaknesses found in Phase 8F-1:

1. ``ProductAdmin`` must not expose ``inventory_tracking_mode`` as an editable
   field, and a crafted admin POST must not be able to change it.
2. ``apps.menu.services.change_inventory_tracking_mode()`` is the single
   audited path that may switch a product's tracking mode. The service is
   readiness-gated; these fixtures provide complete recipe/packaging
   configuration so the authorization, audit and rollback tests can run.

Everything here runs against Django's isolated test database; the local
development database is never touched.
"""

from decimal import Decimal
from unittest import mock

from django.contrib.admin.sites import AdminSite
from django.db import DatabaseError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import CustomUser
from apps.audit.models import AuditLog
from apps.inventory.models import (
    InventoryItem,
    ProductPackagingRule,
    Recipe,
    RecipeItem,
)
from apps.menu.admin import ProductAdmin
from apps.menu.models import Category, Product
from apps.menu.services import (
    CUTOVER_AUDIT_ACTION,
    TrackingModeChangeError,
    change_inventory_tracking_mode,
)

PASSWORD = 'phase8f2-pass'

#: Four products used by the service tests.
CUTOVER_IDS = (2, 10, 11, 34)

#: A product that exists but is not configured for cutover until a test needs it.
UNRELATED_ID = 35


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user(username='p8f2_admin', role='admin'):
    return CustomUser.objects.create_user(
        username=username, password=PASSWORD, role=role,
    )


def _superuser(username='p8f2_root'):
    return CustomUser.objects.create_superuser(
        username=username, password=PASSWORD, role='admin',
    )


def _category(name='Drinks'):
    return Category.objects.create(name=name, slug=name.lower())


def _product(pk, name, category, mode='legacy', price='65.00', stock=92):
    return Product.objects.create(
        id=pk, category=category, name=name, price=Decimal(price),
        stock_quantity=stock, inventory_tracking_mode=mode,
    )


def _configure_product(product, label):
    """Give a product complete recipe + packaging configuration.

    All fixture products use the single 'none' size, so one approved
    all-scope recipe and two no-packaging rules are sufficient.
    """
    ingredient = InventoryItem.objects.create(
        name=f'Ingredient {label}', item_type='ingredient', base_unit='g',
        on_hand_quantity=Decimal('1000.000'),
    )
    recipe = Recipe.objects.create(
        product=product, size='none', order_type_scope='all',
        status=Recipe.STATUS_APPROVED, revision=1,
        approved_at=timezone.now(), approval_note='Test approval',
    )
    RecipeItem.objects.create(
        recipe=recipe, inventory_item=ingredient,
        quantity=Decimal('50.000'),
    )
    for order_type in ('dine_in', 'takeout'):
        ProductPackagingRule.objects.create(
            product=product, size='none', order_type=order_type,
            profile=None, no_packaging=True,
        )
    return ingredient, recipe


def _cutover_products(category):
    """Create four fully configured products plus one unconfigured product."""
    products = {
        2: _product(2, 'Mocha', category, stock=92),
        10: _product(10, 'Wintermelon', category, stock=80),
        11: _product(11, 'Okinawa', category, stock=79),
        34: _product(34, 'Special Pork Burger', category, stock=49),
        UNRELATED_ID: _product(UNRELATED_ID, 'Iced Tea', category, stock=60),
    }
    for product_id in (2, 10, 11, 34):
        _configure_product(products[product_id], f'p{product_id}')
    return products


# ═════════════════════════════════════════════════════════════════════════════
# 1. Service — authorization and validation (no writes on rejection)
# ═════════════════════════════════════════════════════════════════════════════

class CutoverServiceValidationTests(TestCase):
    """Every validation runs before the first write."""

    def setUp(self):
        self.admin = _user()
        self.category = _category()
        self.products = _cutover_products(self.category)

    def _modes(self):
        return dict(
            Product.objects.filter(pk__in=CUTOVER_IDS)
            .values_list('pk', 'inventory_tracking_mode')
        )

    def _assert_nothing_changed(self):
        self.assertEqual(set(self._modes().values()), {'legacy'})
        self.assertEqual(AuditLog.objects.filter(
            action=CUTOVER_AUDIT_ACTION).count(), 0)

    def test_accepts_the_four_authorized_products(self):
        result = change_inventory_tracking_mode(
            CUTOVER_IDS, 'ingredient', actor=self.admin,
        )
        self.assertEqual(result.product_ids, tuple(sorted(CUTOVER_IDS)))

    def test_product_without_complete_configuration_is_rejected(self):
        """There is no allowlist; an unconfigured product is still blocked."""
        with self.assertRaises(TrackingModeChangeError):
            change_inventory_tracking_mode(
                list(CUTOVER_IDS) + [UNRELATED_ID], 'ingredient',
                actor=self.admin,
            )
        self._assert_nothing_changed()
        self.products[UNRELATED_ID].refresh_from_db()
        self.assertEqual(
            self.products[UNRELATED_ID].inventory_tracking_mode, 'legacy')

    def test_duplicate_ids_are_rejected(self):
        with self.assertRaises(TrackingModeChangeError):
            change_inventory_tracking_mode(
                [2, 2, 10, 11, 34], 'ingredient', actor=self.admin)
        self._assert_nothing_changed()

    def test_missing_product_is_rejected(self):
        with self.assertRaises(TrackingModeChangeError):
            change_inventory_tracking_mode(
                list(CUTOVER_IDS) + [999999], 'ingredient', actor=self.admin)
        self.assertEqual(set(self._modes().values()), {'legacy'})
        self.assertEqual(AuditLog.objects.filter(
            action=CUTOVER_AUDIT_ACTION).count(), 0)

    def test_invalid_target_mode_is_rejected(self):
        with self.assertRaises(TrackingModeChangeError):
            change_inventory_tracking_mode(
                CUTOVER_IDS, 'unknown', actor=self.admin)
        self._assert_nothing_changed()

    def test_empty_input_is_rejected(self):
        for empty in ([], (), set()):
            with self.assertRaises(TrackingModeChangeError):
                change_inventory_tracking_mode(
                    empty, 'ingredient', actor=self.admin)
        self._assert_nothing_changed()

    def test_non_iterable_and_non_integer_ids_are_rejected(self):
        for bad in (None, 2, '234', [2, '10']):
            with self.assertRaises(TrackingModeChangeError):
                change_inventory_tracking_mode(
                    bad, 'ingredient', actor=self.admin)
        self._assert_nothing_changed()

    def test_already_switched_product_is_rejected(self):
        Product.objects.filter(pk=10).update(
            inventory_tracking_mode='ingredient')
        with self.assertRaises(TrackingModeChangeError):
            change_inventory_tracking_mode(
                CUTOVER_IDS, 'ingredient', actor=self.admin)
        # The other three were not touched.
        modes = self._modes()
        self.assertEqual(modes[2], 'legacy')
        self.assertEqual(modes[11], 'legacy')
        self.assertEqual(modes[34], 'legacy')
        self.assertEqual(AuditLog.objects.filter(
            action=CUTOVER_AUDIT_ACTION).count(), 0)

    def test_no_hardcoded_allowlist_blocks_a_fully_ready_product(self):
        """A ready product outside the old four-id allowlist is accepted."""
        _configure_product(self.products[UNRELATED_ID], 'unrelated')
        result = change_inventory_tracking_mode(
            [UNRELATED_ID], 'ingredient', actor=self.admin)
        self.assertEqual(result.product_ids, (UNRELATED_ID,))
        self.products[UNRELATED_ID].refresh_from_db()
        self.assertEqual(
            self.products[UNRELATED_ID].inventory_tracking_mode, 'ingredient')


# ═════════════════════════════════════════════════════════════════════════════
# 2. Service — successful operation
# ═════════════════════════════════════════════════════════════════════════════

class CutoverServiceSuccessTests(TestCase):
    """The happy path: four switches, four audit rows, nothing else."""

    def setUp(self):
        self.admin = _user()
        self.category = _category()
        self.products = _cutover_products(self.category)
        # Snapshot every non-mode field of every product.
        self.before = {
            row['id']: row for row in Product.objects.values()
        }

    def _run(self, **kwargs):
        return change_inventory_tracking_mode(
            CUTOVER_IDS, 'ingredient', actor=self.admin, **kwargs)

    def test_four_products_switch_to_ingredient(self):
        self._run()
        modes = dict(
            Product.objects.filter(pk__in=CUTOVER_IDS)
            .values_list('pk', 'inventory_tracking_mode')
        )
        self.assertEqual(modes, {2: 'ingredient', 10: 'ingredient',
                                 11: 'ingredient', 34: 'ingredient'})

    def test_unrelated_products_keep_their_mode(self):
        self._run()
        self.products[UNRELATED_ID].refresh_from_db()
        self.assertEqual(
            self.products[UNRELATED_ID].inventory_tracking_mode, 'legacy')
        self.assertEqual(
            Product.objects.filter(inventory_tracking_mode='ingredient').count(),
            4)

    def test_only_the_mode_and_timestamp_are_written(self):
        self._run()
        after = {row['id']: row for row in Product.objects.values()}
        for pk, before_row in self.before.items():
            for field, value in before_row.items():
                if field in ('inventory_tracking_mode', 'updated_at'):
                    continue
                self.assertEqual(
                    after[pk][field], value,
                    f'{field} on product {pk} must not change',
                )

    def test_exactly_four_audit_rows_with_old_and_new_mode(self):
        result = self._run()
        entries = AuditLog.objects.filter(action=CUTOVER_AUDIT_ACTION)
        self.assertEqual(entries.count(), 4)
        for entry in entries:
            self.assertEqual(entry.object_type, 'product')
            self.assertIn(int(entry.object_id), CUTOVER_IDS)
            self.assertIn('legacy', entry.detail)
            self.assertIn('ingredient', entry.detail)
            self.assertIn('->', entry.detail)
        # The audit rows the service reported are the ones persisted.
        self.assertEqual(
            sorted(change.audit_log_id for change in result.changes),
            sorted(entries.values_list('pk', flat=True)),
        )

    def test_actor_and_timestamp_are_recorded(self):
        self._run()
        for entry in AuditLog.objects.filter(action=CUTOVER_AUDIT_ACTION):
            self.assertEqual(entry.user, self.admin)
            self.assertIsNotNone(entry.created_at)

    def test_reason_is_appended_to_the_detail(self):
        self._run(reason='phase 8 cutover')
        self.assertTrue(AuditLog.objects.filter(
            action=CUTOVER_AUDIT_ACTION, detail__contains='phase 8 cutover',
        ).exists())

    def test_result_reports_old_and_new_values(self):
        result = self._run()
        self.assertEqual(result.target_mode, 'ingredient')
        self.assertEqual(result.product_ids, tuple(sorted(CUTOVER_IDS)))
        self.assertEqual(result.old_modes(), {
            2: 'legacy', 10: 'legacy', 11: 'legacy', 34: 'legacy',
        })
        for change in result.changes:
            self.assertEqual(change.new_mode, 'ingredient')
            self.assertEqual(change.old_mode, 'legacy')
            self.assertTrue(change.audit_log_id)
            self.assertTrue(change.product_name)

    def test_anonymous_actor_is_stored_as_null_user(self):
        change_inventory_tracking_mode(CUTOVER_IDS, 'ingredient')
        for entry in AuditLog.objects.filter(action=CUTOVER_AUDIT_ACTION):
            self.assertIsNone(entry.user)


# ═════════════════════════════════════════════════════════════════════════════
# 3. Service — failures and rollback
# ═════════════════════════════════════════════════════════════════════════════

class CutoverServiceFailureTests(TestCase):
    """A failure anywhere leaves the database exactly as it was."""

    def setUp(self):
        self.admin = _user()
        self.category = _category()
        self.products = _cutover_products(self.category)

    def _assert_all_legacy(self):
        self.assertEqual(
            Product.objects.filter(pk__in=CUTOVER_IDS)
            .exclude(inventory_tracking_mode='legacy').count(),
            0,
        )

    def test_audit_failure_rolls_back_every_mode_change(self):
        with mock.patch.object(
            AuditLog.objects, 'create', side_effect=DatabaseError('db down'),
        ), self.assertLogs('apps.audit', level='ERROR'):
            with self.assertRaises(DatabaseError):
                change_inventory_tracking_mode(
                    CUTOVER_IDS, 'ingredient', actor=self.admin)
        self._assert_all_legacy()
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_one_invalid_product_prevents_all_changes(self):
        with self.assertRaises(TrackingModeChangeError):
            change_inventory_tracking_mode(
                [2, 10, 11, UNRELATED_ID], 'ingredient', actor=self.admin)
        self._assert_all_legacy()

    def test_second_execution_is_rejected(self):
        change_inventory_tracking_mode(
            CUTOVER_IDS, 'ingredient', actor=self.admin)
        with self.assertRaises(TrackingModeChangeError):
            change_inventory_tracking_mode(
                CUTOVER_IDS, 'ingredient', actor=self.admin)
        # Audit rows are not duplicated by the rejected re-run.
        self.assertEqual(AuditLog.objects.filter(
            action=CUTOVER_AUDIT_ACTION).count(), 4)

    def test_invalid_second_execution_leaves_first_cutover_intact(self):
        change_inventory_tracking_mode(
            CUTOVER_IDS, 'ingredient', actor=self.admin)
        with self.assertRaises(TrackingModeChangeError):
            change_inventory_tracking_mode(
                [2, 10, 11, 34, UNRELATED_ID], 'ingredient', actor=self.admin)
        self.assertEqual(
            Product.objects.filter(pk__in=CUTOVER_IDS)
            .exclude(inventory_tracking_mode='ingredient').count(),
            0,
        )
        self.products[UNRELATED_ID].refresh_from_db()
        self.assertEqual(
            self.products[UNRELATED_ID].inventory_tracking_mode, 'legacy')


# ═════════════════════════════════════════════════════════════════════════════
# 4. ProductAdmin protection
# ═════════════════════════════════════════════════════════════════════════════

class ProductAdminTrackingModeTests(TestCase):
    """``inventory_tracking_mode`` is read-only in the admin — server-side."""

    def setUp(self):
        self.root = _superuser()
        self.client.force_login(self.root)
        self.category = _category('Admin Drinks')
        self.product = _product(
            2, 'Mocha', self.category, price='99.00', stock=92)
        self.url = reverse(
            'admin:menu_product_change', args=[self.product.pk])

    def _admin_request(self):
        request = RequestFactory().get('/admin/menu/product/')
        request.user = self.root
        return request

    def _post_data(self, **overrides):
        data = {
            'category': self.category.pk,
            'name': 'Mocha',
            'slug': 'mocha',
            'description': '',
            'price': '99.00',
            'price_medium': '',
            'price_large': '',
            'price_hot': '',
            'has_sizes': '',
            'is_available': 'on',
            'is_active': 'on',
            'is_featured': '',
            'stock_quantity': '92',
            'low_stock_threshold': '10',
        }
        data.update(overrides)
        return data

    def test_admin_form_does_not_expose_editable_tracking_mode(self):
        model_admin = ProductAdmin(Product, AdminSite())
        form_class = model_admin.get_form(
            self._admin_request(), obj=self.product)
        self.assertNotIn('inventory_tracking_mode', form_class.base_fields)
        # Stock fields are read-only server-side and therefore excluded from
        # the bound form, for legacy products too.
        for field in ('stock_quantity', 'low_stock_threshold'):
            self.assertNotIn(field, form_class.base_fields)
        # Ordinary product fields are still editable.
        for field in ('name', 'price', 'is_available'):
            self.assertIn(field, form_class.base_fields)

    def test_change_form_renders_no_tracking_mode_input(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="inventory_tracking_mode"')
        # The current mode is still visible, read-only.
        self.assertContains(response, 'Inventory tracking')
        self.assertContains(response, 'Legacy')

    def test_crafted_post_cannot_change_tracking_mode(self):
        response = self.client.post(
            self.url,
            self._post_data(inventory_tracking_mode='ingredient'),
        )
        self.assertEqual(response.status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual(self.product.inventory_tracking_mode, 'legacy')

    def test_other_product_fields_are_still_editable_in_admin(self):
        response = self.client.post(self.url, self._post_data(
            name='Mocha Grande',
            slug='mocha-grande',
            price='115.00',
            stock_quantity='77',
            low_stock_threshold='1',
        ))
        self.assertEqual(response.status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual(self.product.name, 'Mocha Grande')
        self.assertEqual(self.product.price, Decimal('115.00'))
        # Crafted stock values are ignored: legacy stock changes must go through
        # the staff product form, which writes an InventoryLog row.
        self.assertEqual(self.product.stock_quantity, 92)
        self.assertEqual(self.product.low_stock_threshold, 10)
        self.assertEqual(self.product.inventory_tracking_mode, 'legacy')

    def test_existing_admin_permissions_and_options_are_intact(self):
        model_admin = ProductAdmin(Product, AdminSite())
        request = self._admin_request()
        self.assertTrue(model_admin.has_add_permission(request))
        self.assertTrue(model_admin.has_change_permission(
            request, obj=self.product))
        # Product deletion stays disabled (soft-deactivate only).
        self.assertFalse(model_admin.has_delete_permission(request))
        self.assertEqual(model_admin.list_editable, ['is_available'])
        self.assertIn('name', model_admin.search_fields)
        self.assertIn('deactivate_products', model_admin.actions)
        self.assertIn('activate_products', model_admin.actions)

    def test_admin_never_calls_the_cutover_service(self):
        """The audited service is not reachable from the admin surface."""
        model_admin = ProductAdmin(Product, AdminSite())
        self.assertFalse(hasattr(model_admin, 'change_inventory_tracking_mode'))
        self.assertNotIn(
            'change_inventory_tracking_mode',
            [getattr(action, '__name__', action)
             for action in (model_admin.actions or [])],
        )
