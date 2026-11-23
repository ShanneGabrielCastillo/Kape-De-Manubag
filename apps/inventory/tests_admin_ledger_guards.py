"""
Regression tests for audit finding C-3 — Django Admin inventory mutation bypass.

The two ledgers (InventoryLog for legacy product stock, IngredientMovementLog
for ingredient/packaging stock) are append-only: they are written only by the
inventory services, in the same transaction that changes the balance.  The
admin is a review surface, and InventoryItem's authoritative stock fields can
no longer be edited on an existing item.

These tests exercise the admin over HTTP (crafted POSTs, delete views, bulk
actions) instead of asserting that ``readonly_fields`` is populated.

Limits: the protections live in ``ModelAdmin``.  They stop admin-interface
mutation only; direct ORM/shell writes remain possible by design.
"""

import itertools
from datetime import date
from decimal import Decimal

from django.contrib import admin as django_admin
from django.db import transaction
from django.test import TestCase
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from apps.accounts.models import CustomUser
from apps.inventory.models import (
    IngredientMovementLog,
    InventoryItem,
    InventoryLog,
    PackagingProfile,
    PackagingProfileItem,
    Recipe,
    RecipeItem,
    StockPurchase,
    StockPurchaseItem,
    UnitConversion,
)
from apps.inventory.services import record_adjustment
from apps.menu.models import Category, Product
from apps.orders.models import (
    Order,
    OrderIngredientReservation,
    OrderIngredientReservationItem,
    OrderItem,
)

PASSWORD = 'kdm-admin-guard-pass-123'


def _superuser(username):
    return CustomUser.objects.create_superuser(
        username=username, password=PASSWORD, role='admin',
    )


def _item(name='Guard Coffee Beans', base_unit='g',
          on_hand=Decimal('0.000'), item_type=InventoryItem.ITEM_TYPE_INGREDIENT):
    return InventoryItem.objects.create(
        name=name,
        item_type=item_type,
        base_unit=base_unit,
        on_hand_quantity=on_hand,
        low_stock_threshold=Decimal('100.000'),
    )


_counter = itertools.count(1)


def _uid():
    return next(_counter)


def _product():
    n = f'Guard Product {_uid()}'
    category = Category.objects.create(name=f'Guard Cat {_uid()}', slug=n.lower())
    return Product.objects.create(
        category=category,
        name=n,
        slug=n.lower(),
        price='60.00',
        inventory_tracking_mode=Product.INVENTORY_TRACKING_INGREDIENT,
    )


def _recipe(status=Recipe.STATUS_RETIRED):
    return Recipe.objects.create(
        product=_product(),
        size='none',
        order_type_scope=Recipe.SCOPE_ALL,
        status=status,
        revision=1,
    )


def _packaging_profile(status=PackagingProfile.STATUS_RETIRED):
    return PackagingProfile.objects.create(
        name=f'Guard Profile {_uid()}',
        status=status,
        revision=1,
    )


def _order_item():
    order = Order.objects.create(customer_name='Guard Test Customer')
    return OrderItem.objects.create(
        order=order,
        product=None,
        product_name='Guard Order Line',
        category_name='',
        packaging_eligible=False,
        size='none',
        quantity=2,
        unit_price=Decimal('60.00'),
        subtotal=Decimal('120.00'),
        inventory_mode=OrderItem.INVENTORY_MODE_INGREDIENT,
    )


class _AdminTestCase(TestCase):
    def setUp(self):
        self.admin = _superuser(f'guard_root_{self.__class__.__name__.lower()}')
        self.client.force_login(self.admin)

    def _add_stock(self, item, new_on_hand='500.000', reason='stock_count'):
        """Legitimate stock change through the authoritative service."""
        with transaction.atomic():
            movement = record_adjustment(
                inventory_item_id=item.pk,
                new_on_hand=Decimal(new_on_hand),
                expected_on_hand=item.on_hand_quantity,
                reason_code=reason,
                performed_by=self.admin,
            )
        item.refresh_from_db()
        return movement


# ═════════════════════════════════════════════════════════════════════════════
# 1. InventoryItem — authoritative stock fields
# ═════════════════════════════════════════════════════════════════════════════

class InventoryItemAdminStockTests(_AdminTestCase):
    def setUp(self):
        super().setUp()
        self.item = _item()
        self.movement = self._add_stock(self.item)

    def _change_url(self, item):
        return reverse('admin:inventory_inventoryitem_change', args=[item.pk])

    def _payload(self, item, **overrides):
        data = {
            'name': item.name,
            'item_type': item.item_type,
            'base_unit': item.base_unit,
            'low_stock_threshold': str(item.low_stock_threshold),
            'description': item.description,
            'is_active': 'on',
        }
        data.update(overrides)
        return data

    def test_crafted_post_cannot_change_on_hand_balance(self):
        response = self.client.post(
            self._change_url(self.item),
            self._payload(self.item, on_hand_quantity='1.000'),
        )

        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('500.000'))
        # No stock movement was recorded, because no stock changed.
        self.assertEqual(IngredientMovementLog.objects.count(), 1)

    def test_crafted_post_cannot_change_reserved_quantity(self):
        response = self.client.post(
            self._change_url(self.item),
            self._payload(self.item, reserved_quantity='250.000'),
        )

        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.reserved_quantity, Decimal('0.000'))

    def test_crafted_post_cannot_reinterpret_base_unit_after_history(self):
        response = self.client.post(
            self._change_url(self.item),
            self._payload(
                self.item,
                base_unit='ml',
                name='Guard Coffee Beans Renamed',
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.base_unit, 'g')
        # Metadata in the same POST did save — the lock is field-level.
        self.assertEqual(self.item.name, 'Guard Coffee Beans Renamed')

    def test_base_unit_locked_when_starting_stock_exists_without_movements(self):
        item = _item(name='Guard Initial Stock', base_unit='g',
                     on_hand=Decimal('2500.000'))

        response = self.client.post(
            self._change_url(item), self._payload(item, base_unit='ml'),
        )

        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.base_unit, 'g')

    def test_base_unit_still_correctable_for_empty_item_without_history(self):
        item = _item(name='Guard Empty Item', base_unit='g')

        response = self.client.post(
            self._change_url(item), self._payload(item, base_unit='ml'),
        )

        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.base_unit, 'ml')

    def test_descriptive_metadata_stays_editable(self):
        response = self.client.post(
            self._change_url(self.item),
            self._payload(
                self.item,
                name='Guard Renamed Beans',
                description='Roasted medium.',
                low_stock_threshold='250.000',
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.name, 'Guard Renamed Beans')
        self.assertEqual(self.item.description, 'Roasted medium.')
        self.assertEqual(self.item.low_stock_threshold, Decimal('250.000'))
        self.assertEqual(self.item.on_hand_quantity, Decimal('500.000'))

    def test_item_creation_with_starting_stock_still_works(self):
        response = self.client.post(
            reverse('admin:inventory_inventoryitem_add'),
            {
                'name': 'Guard New Item',
                'item_type': InventoryItem.ITEM_TYPE_PACKAGING,
                'base_unit': 'pcs',
                'on_hand_quantity': '1200.000',
                'low_stock_threshold': '50.000',
                'description': '',
                'is_active': 'on',
            },
        )

        self.assertEqual(response.status_code, 302)
        created = InventoryItem.objects.get(name='Guard New Item')
        self.assertEqual(created.on_hand_quantity, Decimal('1200.000'))
        self.assertEqual(created.reserved_quantity, Decimal('0.000'))


# ═════════════════════════════════════════════════════════════════════════════
# 1b. InventoryItem — base unit locked by dependent configuration records
# ═════════════════════════════════════════════════════════════════════════════

class InventoryItemAdminBaseUnitDependencyTests(_AdminTestCase):
    """A reference from recipe/packaging/conversion/purchase/reservation data
    locks ``base_unit`` even when the item has no stock and no movements.

    Each test asserts the dependency row really exists and that the item is
    otherwise empty, so the lock cannot be attributed to the stock, reservation
    balance, or movement-history guards.
    """

    def _change_url(self, item):
        return reverse('admin:inventory_inventoryitem_change', args=[item.pk])

    def _payload(self, item, **overrides):
        data = {
            'name': item.name,
            'item_type': item.item_type,
            'base_unit': item.base_unit,
            'low_stock_threshold': str(item.low_stock_threshold),
            'description': item.description,
            'is_active': 'on',
        }
        data.update(overrides)
        return data

    def _assert_empty_item(self, item):
        item.refresh_from_db()
        self.assertEqual(item.on_hand_quantity, Decimal('0.000'))
        self.assertEqual(item.reserved_quantity, Decimal('0.000'))
        self.assertFalse(item.movements.exists())

    def _post_base_unit_change(self, item, new_unit='ml'):
        response = self.client.post(
            self._change_url(item),
            self._payload(
                item,
                base_unit=new_unit,
                description='Dependency guard description.',
            ),
        )
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        return item

    def test_base_unit_locked_by_recipe_item(self):
        item = _item(name='Guard Recipe Item', base_unit='g')
        recipe = _recipe(status=Recipe.STATUS_RETIRED)
        RecipeItem.objects.create(
            recipe=recipe, inventory_item=item, quantity=Decimal('25.000'))

        self.assertTrue(item.recipe_items.exists())
        self._assert_empty_item(item)

        item = self._post_base_unit_change(item)

        self.assertEqual(item.base_unit, 'g')
        self.assertEqual(item.description, 'Dependency guard description.')

    def test_base_unit_locked_by_packaging_profile_item(self):
        item = _item(name='Guard Packaging Item', base_unit='pcs',
                     item_type=InventoryItem.ITEM_TYPE_PACKAGING)
        profile = _packaging_profile(status=PackagingProfile.STATUS_DRAFT)
        PackagingProfileItem.objects.create(
            profile=profile, inventory_item=item, quantity=Decimal('1.000'))

        self.assertTrue(item.packaging_profile_items.exists())
        self._assert_empty_item(item)

        item = self._post_base_unit_change(item, new_unit='g')

        self.assertEqual(item.base_unit, 'pcs')
        self.assertEqual(item.description, 'Dependency guard description.')

    def test_base_unit_locked_by_inactive_unit_conversion(self):
        item = _item(name='Guard Conversion Item', base_unit='g')
        conversion = UnitConversion.objects.create(
            inventory_item=item,
            purchase_unit='kg',
            conversion_factor=Decimal('1000.0000'),
            is_active=False,
        )

        self.assertTrue(item.unit_conversions.exists())
        self.assertFalse(conversion.is_active)
        self._assert_empty_item(item)

        item = self._post_base_unit_change(item)

        self.assertEqual(item.base_unit, 'g')
        self.assertEqual(item.description, 'Dependency guard description.')

    def test_base_unit_locked_by_historical_stock_purchase_item(self):
        item = _item(name='Guard Purchase Item', base_unit='g')
        purchase = StockPurchase.objects.create(
            purchase_date=date(2026, 1, 15),
            idempotency_key=f'guard-purchase-{_uid()}',
            performed_by=self.admin,
        )
        StockPurchaseItem.objects.create(
            purchase=purchase,
            inventory_item=item,
            purchase_unit='kg',
            purchase_quantity=Decimal('2.000'),
            conversion_factor=Decimal('1000.0000'),
            base_quantity=Decimal('2000.000'),
        )

        self.assertTrue(item.purchase_items.exists())
        self._assert_empty_item(item)

        item = self._post_base_unit_change(item)

        self.assertEqual(item.base_unit, 'g')
        self.assertEqual(item.description, 'Dependency guard description.')

    def test_base_unit_locked_by_closed_consumed_reservation_item(self):
        item = _item(name='Guard Reservation Item', base_unit='g')
        order_item = _order_item()
        reservation = OrderIngredientReservation.objects.create(
            order=order_item.order,
            status=OrderIngredientReservation.STATUS_CONSUMED,
            order_type_snapshot='dine_in',
            settled_at=timezone.now(),
        )
        OrderIngredientReservationItem.objects.create(
            reservation=reservation,
            order_item=order_item,
            inventory_item=item,
            item_name=item.name,
            base_unit=item.base_unit,
            component_source=OrderIngredientReservationItem.COMPONENT_SOURCE_RECIPE,
            source_revision=1,
            quantity_per_unit=Decimal('10.000'),
            order_item_quantity=order_item.quantity,
            reserved_quantity=Decimal('20.000'),
            consumed_quantity=Decimal('20.000'),
            released_quantity=Decimal('0.000'),
            consume_reason=OrderIngredientReservationItem.CONSUME_REASON_ORDER_READY,
        )

        self.assertTrue(item.reservation_items.exists())
        self._assert_empty_item(item)

        item = self._post_base_unit_change(item)

        self.assertEqual(item.base_unit, 'g')
        self.assertEqual(item.description, 'Dependency guard description.')


# ═════════════════════════════════════════════════════════════════════════════
# 2. IngredientMovementLog — append-only
# ═════════════════════════════════════════════════════════════════════════════

class IngredientMovementLogAdminTests(_AdminTestCase):
    def setUp(self):
        super().setUp()
        self.item = _item()
        self.movement = self._add_stock(self.item)

    def _change_url(self):
        return reverse(
            'admin:inventory_ingredientmovementlog_change',
            args=[self.movement.pk],
        )

    def test_crafted_post_cannot_rewrite_a_movement(self):
        response = self.client.post(self._change_url(), {
            'inventory_item': self.item.pk,
            'item_name': 'Fabricated Name',
            'base_unit': 'ml',
            'movement_type': 'wastage',
            'on_hand_delta': '-1.000',
            'reserved_delta': '0.000',
            'on_hand_before': '0.000',
            'on_hand_after': '1.000',
            'reserved_before': '0.000',
            'reserved_after': '0.000',
            'reason_code': 'forged',
            'note': 'forged',
        })

        self.assertEqual(response.status_code, 403)
        self.movement.refresh_from_db()
        self.assertEqual(self.movement.item_name, 'Guard Coffee Beans')
        self.assertEqual(self.movement.base_unit, 'g')
        self.assertEqual(self.movement.movement_type, 'adjustment')
        self.assertEqual(self.movement.on_hand_delta, Decimal('500.000'))
        self.assertEqual(self.movement.on_hand_after, Decimal('500.000'))
        self.assertEqual(self.movement.reason_code, 'stock_count')

    def test_movement_can_still_be_viewed(self):
        response = self.client.get(self._change_url())

        self.assertEqual(response.status_code, 200)

    def test_movement_cannot_be_deleted_from_admin(self):
        response = self.client.post(
            reverse(
                'admin:inventory_ingredientmovementlog_delete',
                args=[self.movement.pk],
            ),
            {'post': 'yes'},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(IngredientMovementLog.objects.count(), 1)

    def test_bulk_delete_action_removes_nothing(self):
        response = self.client.post(
            reverse('admin:inventory_ingredientmovementlog_changelist'),
            {
                'action': 'delete_selected',
                '_selected_action': [str(self.movement.pk)],
            },
        )

        self.assertIn(response.status_code, (200, 302))
        self.assertEqual(IngredientMovementLog.objects.count(), 1)

    def test_movement_cannot_be_created_from_admin(self):
        response = self.client.get(
            reverse('admin:inventory_ingredientmovementlog_add'))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(IngredientMovementLog.objects.count(), 1)

    def test_legitimate_adjustment_service_still_records_stock_and_movement(self):
        movement = self._add_stock(self.item, new_on_hand='750.000',
                                   reason='recount')

        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('750.000'))
        self.assertEqual(IngredientMovementLog.objects.count(), 2)
        self.assertEqual(movement.movement_type, 'adjustment')
        self.assertEqual(movement.on_hand_before, Decimal('500.000'))
        self.assertEqual(movement.on_hand_after, Decimal('750.000'))
        self.assertEqual(movement.reason_code, 'recount')


# ═════════════════════════════════════════════════════════════════════════════
# 3. InventoryLog (legacy product-stock ledger) — append-only
# ═════════════════════════════════════════════════════════════════════════════

class InventoryLogAdminTests(_AdminTestCase):
    def setUp(self):
        super().setUp()
        self.log = InventoryLog.objects.create(
            product=None,
            product_name='',
            action='restock',
            source='inventory_update',
            reason='historical row',
            quantity_change=5,
            quantity_before=0,
            quantity_after=5,
            notes='',
        )

    def _change_url(self):
        return reverse('admin:inventory_inventorylog_change',
                       args=[self.log.pk])

    def test_crafted_post_cannot_rewrite_a_ledger_row(self):
        response = self.client.post(self._change_url(), {
            'product': '',
            'product_name': 'Fabricated',
            'action': 'sale',
            'source': 'customer_order',
            'reason': 'forged',
            'quantity_change': '-5',
            'quantity_before': '5',
            'quantity_after': '0',
            'notes': 'forged',
        })

        self.assertEqual(response.status_code, 403)
        self.log.refresh_from_db()
        self.assertEqual(self.log.action, 'restock')
        self.assertEqual(self.log.quantity_change, 5)
        self.assertEqual(self.log.quantity_after, 5)
        self.assertEqual(self.log.reason, 'historical row')

    def test_ledger_row_cannot_be_deleted_from_admin(self):
        response = self.client.post(
            reverse('admin:inventory_inventorylog_delete', args=[self.log.pk]),
            {'post': 'yes'},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(InventoryLog.objects.count(), 1)

    def test_ledger_row_cannot_be_created_from_admin(self):
        response = self.client.get(reverse('admin:inventory_inventorylog_add'))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(InventoryLog.objects.count(), 1)


# ═════════════════════════════════════════════════════════════════════════════
# 4. Purchase history — no admin surface
# ═════════════════════════════════════════════════════════════════════════════

class PurchaseHistoryAdminExposureTests(_AdminTestCase):
    def test_purchase_records_are_not_registered_in_admin(self):
        """Purchases are append-only and have no admin add/change/delete path.

        If this ever changes, the admin must be made read-only first, or an
        edit could invalidate the movement rows that the purchase produced.
        """
        self.assertFalse(django_admin.site.is_registered(StockPurchase))
        self.assertFalse(django_admin.site.is_registered(StockPurchaseItem))
        with self.assertRaises(NoReverseMatch):
            reverse('admin:inventory_stockpurchase_changelist')
        with self.assertRaises(NoReverseMatch):
            reverse('admin:inventory_stockpurchaseitem_changelist')
