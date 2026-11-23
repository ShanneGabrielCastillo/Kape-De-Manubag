"""Phase 2.3/2.4 — GCash rejection × ingredient reservation regression.

Verifies the complete lifecycle proposed in
docs/phase2.3-verification-gap-closure-plan.md (Part 1):

  1. An ingredient-tracked order is placed (reservation created at
     checkout-time placement, status ``awaiting_payment``, GCash pending).
  2. Staff rejects the GCash payment → the order STAYS
     ``awaiting_payment`` and the reservation STAYS ``reserved`` with
     reserved quantities unchanged; on-hand inventory unchanged; no
     release/consumption movements appear.
  3. Staff cancels the rejected unpaid order through the real
     ``update_order_status`` endpoint → the reservation is released
     exactly once (reserved → 0, on-hand unchanged), one release
     movement per component, and the order is cancelled.
  4. Repeating the cancellation does not duplicate releases or restore
     anything.
  5. A MIXED order (legacy + ingredient lines) is also verified: rejection
     leaves both ledgers untouched; cancellation restores the legacy
     deduction and releases the ingredient reservation on their own
     ledgers.

Production entry points exercised (no bypasses):
  - ``place_order_inventory`` (apps/orders/services.py) — placement.
  - ``reject_gcash_payment`` view (apps/orders/views.py:1535) — rejection.
  - ``update_order_status`` view (apps/orders/views.py:1046) — staff
    cancellation, which drives ``on_status_changed`` → ``settle``.

Isolated test database only (TestCase); the live development database is
never touched. No PostgreSQL access.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import Client, TestCase
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.inventory.models import IngredientMovementLog, InventoryItem
from apps.orders.services import place_order_inventory
from apps.orders.models import (
    Order,
    OrderIngredientReservation,
    OrderIngredientReservationItem,
    OrderItem,
)

# Established Phase 5B factories (same convention as tests_phase8d.py).
from apps.orders.tests_phase5b import (  # noqa: E402
    _ingredient_item,
    _ingredient_product,
    _product,
    _uid,
)

PASSWORD = 'phase12-gcash-pass'


def _gcash_staff():
    u = get_user_model().objects.create_user(
        username=f'p12_staff_{_uid()}', password=PASSWORD, role='admin')
    return u


def _place_gcash_ingredient_order(prod, quantity=1):
    """Create + place an awaiting_payment GCash order with one ingredient
    line, exactly as the checkout flow would (reservation included)."""
    order = Order.objects.create(
        customer_name=f'P12 GCash {_uid()}',
        order_type='dine_in',
        payment_method='gcash',
        gcash_status='pending',
    )
    OrderItem.objects.create(
        order=order, product=prod, product_name=prod.name,
        category_name=prod.category.name, packaging_eligible=False,
        size='none', quantity=quantity, unit_price=Decimal('60.00'),
        subtotal=Decimal('60.00') * quantity,
        inventory_mode=OrderItem.INVENTORY_MODE_INGREDIENT,
    )
    with transaction.atomic():
        place_order_inventory(order.pk)
    return order


class _GcashStaffClientMixin:
    """Shared staff client + the two production endpoints under test."""

    def setUp(self):
        super().setUp()
        self.staff = _gcash_staff()
        self.client = Client(HTTP_HOST='testserver')
        self.client.force_login(self.staff)

    def _reject(self, order):
        resp = self.client.post(
            reverse('orders:reject_gcash_payment', args=[order.pk]),
            {'rejection_note': 'Phase 2.4 reference not found'},
        )
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertTrue(resp.json()['success'])
        return resp

    def _cancel(self, order):
        resp = self._cancel_raw(order)
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertTrue(resp.json()['success'])
        return resp

    def _cancel_raw(self, order):
        return self.client.post(
            reverse('orders:update_status', args=[order.pk]),
            {'status': 'cancelled'},
        )


class GcashRejectionReservationLifecycleTests(_GcashStaffClientMixin,
                                               TestCase):
    """Rejected GCash payment: reservation retained; cancel releases
    exactly once; repeat cancel is a no-op on every ledger."""

    def setUp(self):
        self.staff = _gcash_staff()
        self.client = Client(HTTP_HOST='testserver')
        self.client.force_login(self.staff)

    def _reject(self, order):
        return self.client.post(
            reverse('orders:reject_gcash_payment', args=[order.pk]),
            {'rejection_note': 'Phase 1.2 reference not found'},
        )

    def _cancel(self, order):
        return self.client.post(
            reverse('orders:update_status', args=[order.pk]),
            {'status': 'cancelled'},
        )

    def _ingredient_case(self, on_hand=Decimal('100.000'), quantity=1):
        ingr = _ingredient_item(on_hand=on_hand)
        prod, ingr, _, _, _ = _ingredient_product(
            ingr_item=ingr, qty_per_unit=Decimal('10.000')
        )
        order = _place_gcash_ingredient_order(prod, quantity=quantity)
        return prod, ingr, order

    def test_reject_retains_reservation_and_cancel_releases_exactly_once(self):
        prod, ingr, order = self._ingredient_case(
            on_hand=Decimal('100.000'), quantity=1)

        # 1-2. Reservation created at placement: awaiting_payment GCash
        #      order with reserved = 10 g, on-hand untouched.
        self.assertEqual(order.status, 'awaiting_payment')
        res = OrderIngredientReservation.objects.get(order=order)
        self.assertEqual(res.status, OrderIngredientReservation.STATUS_RESERVED)
        line = res.items.get()
        self.assertEqual(line.reserved_quantity, Decimal('10.000'))
        self.assertEqual(line.component_source, 'recipe')
        ingr.refresh_from_db()
        self.assertEqual(ingr.reserved_quantity, Decimal('10.000'))
        self.assertEqual(ingr.on_hand_quantity, Decimal('100.000'))

        # 3. Staff rejects the GCash payment.
        self._reject(order)
        order.refresh_from_db()
        self.assertEqual(order.status, 'awaiting_payment')
        self.assertEqual(order.gcash_status, 'rejected')
        self.assertFalse(order.is_paid)
        res.refresh_from_db()
        self.assertEqual(res.status, OrderIngredientReservation.STATUS_RESERVED)
        line.refresh_from_db()
        self.assertEqual(line.reserved_quantity, Decimal('10.000'))
        ingr.refresh_from_db()
        self.assertEqual(ingr.reserved_quantity, Decimal('10.000'))
        self.assertEqual(ingr.on_hand_quantity, Decimal('100.000'))
        # No release/consumption movements appeared on rejection.
        self.assertFalse(IngredientMovementLog.objects.exclude(
            movement_type=IngredientMovementLog.MOVEMENT_RESERVATION,
        ).filter(order=order).exists())

        # 4. Staff cancels the rejected unpaid order through the real
        #    staff endpoint → RELEASE_ALL settlement.
        self._cancel(order)
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        res.refresh_from_db()
        self.assertEqual(res.status, OrderIngredientReservation.STATUS_RELEASED)
        self.assertIsNotNone(res.settled_at)
        line.refresh_from_db()
        self.assertEqual(line.reserved_quantity, Decimal('10.000'))
        self.assertEqual(line.released_quantity, Decimal('10.000'))
        self.assertEqual(line.consumed_quantity, Decimal('0.000'))
        ingr.refresh_from_db()
        self.assertEqual(ingr.reserved_quantity, Decimal('0.000'))
        self.assertEqual(ingr.on_hand_quantity, Decimal('100.000'))

        movements = IngredientMovementLog.objects.filter(order=order)
        self.assertEqual(movements.filter(
            movement_type=IngredientMovementLog.MOVEMENT_RESERVATION).count(), 1)
        self.assertEqual(movements.filter(
            movement_type=IngredientMovementLog.MOVEMENT_RESERVATION_RELEASE).count(), 1)
        self.assertEqual(movements.filter(
            movement_type=IngredientMovementLog.MOVEMENT_CONSUMPTION).count(), 0)

        # 5. Repeating the cancellation must not duplicate anything
        #    (the repeat is an invalid cancelled→cancelled transition and
        #    must fail cleanly, leaving every ledger untouched).
        self._cancel_raw(order)
        self.assertEqual(
            IngredientMovementLog.objects.filter(order=order).count(),
            movements.count(),
            'repeat cancel created duplicate movements')
        ingr.refresh_from_db()
        self.assertEqual(ingr.reserved_quantity, Decimal('0.000'))
        self.assertEqual(ingr.on_hand_quantity, Decimal('100.000'))
        self.assertEqual(ingr.reserved_quantity + ingr.on_hand_quantity,
                         Decimal('100.000'))   # no stock created or destroyed

    def test_reject_keeps_reserved_even_across_resubmission_state(self):
        # After rejection the customer may re-submit; until payment is
        # verified the reservation must remain held.
        prod, ingr, order = self._ingredient_case(
            on_hand=Decimal('50.000'), quantity=2)
        self._reject(order)
        order.refresh_from_db()
        self.assertEqual(order.status, 'awaiting_payment')
        res = OrderIngredientReservation.objects.get(order=order)
        self.assertEqual(res.status, OrderIngredientReservation.STATUS_RESERVED)
        line = res.items.get()
        self.assertEqual(line.reserved_quantity, Decimal('20.000'))
        ingr.refresh_from_db()
        self.assertEqual(ingr.reserved_quantity, Decimal('20.000'))
        self.assertEqual(ingr.on_hand_quantity, Decimal('50.000'))


class GcashRejectionMixedOrderTests(_GcashStaffClientMixin, TestCase):
    """Mixed order (legacy + ingredient lines): rejection partitions the
    ledgers; cancellation restores legacy stock and releases the
    ingredient reservation on their own books."""

    def _mixed_case(self):
        ingr = _ingredient_item(on_hand=Decimal('100.000'))
        prod_ingr, ingr, _, _, _ = _ingredient_product(
            ingr_item=ingr, qty_per_unit=Decimal('10.000')
        )
        legacy = _product(stock=10)
        order = Order.objects.create(
            customer_name=f'P12 Mixed {_uid()}',
            order_type='dine_in',
            payment_method='gcash',
            gcash_status='pending',
        )
        OrderItem.objects.create(
            order=order, product=prod_ingr, product_name=prod_ingr.name,
            category_name=prod_ingr.category.name, packaging_eligible=False,
            size='none', quantity=1, unit_price=Decimal('60.00'),
            subtotal=Decimal('60.00'),
            inventory_mode=OrderItem.INVENTORY_MODE_INGREDIENT,
        )
        OrderItem.objects.create(
            order=order, product=legacy, product_name=legacy.name,
            category_name=legacy.category.name, packaging_eligible=False,
            size='none', quantity=2, unit_price=Decimal('60.00'),
            subtotal=Decimal('120.00'),
            inventory_mode=OrderItem.INVENTORY_MODE_LEGACY,
        )
        with transaction.atomic():
            place_order_inventory(order.pk)
        return prod_ingr, ingr, legacy, order

    def test_reject_touches_neither_ledger_and_cancel_partitions(self):
        prod_ingr, ingr, legacy, order = self._mixed_case()

        # Placement: legacy deducted, ingredient reserved.
        ingr.refresh_from_db(); legacy.refresh_from_db()
        self.assertEqual(legacy.stock_quantity, 8)          # 10 - 2
        self.assertEqual(ingr.reserved_quantity, Decimal('10.000'))

        # 3. Rejection: BOTH ledgers untouched.
        self._reject(order)
        order.refresh_from_db()
        self.assertEqual(order.status, 'awaiting_payment')
        self.assertEqual(order.gcash_status, 'rejected')
        ingr.refresh_from_db(); legacy.refresh_from_db()
        self.assertEqual(legacy.stock_quantity, 8)
        self.assertEqual(ingr.reserved_quantity, Decimal('10.000'))
        self.assertEqual(ingr.on_hand_quantity, Decimal('100.000'))

        # 4. Cancellation: legacy stock restored, ingredient released.
        self._cancel(order)
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        legacy.refresh_from_db()
        self.assertEqual(legacy.stock_quantity, 10)         # restored
        ingr.refresh_from_db()
        self.assertEqual(ingr.reserved_quantity, Decimal('0.000'))
        self.assertEqual(ingr.on_hand_quantity, Decimal('100.000'))
        res = OrderIngredientReservation.objects.get(order=order)
        self.assertEqual(res.status, OrderIngredientReservation.STATUS_RELEASED)

        # Exactly one release movement; no consumption anywhere.
        movements = IngredientMovementLog.objects.filter(order=order)
        self.assertEqual(movements.filter(
            movement_type=IngredientMovementLog.MOVEMENT_RESERVATION_RELEASE).count(), 1)
        self.assertEqual(movements.filter(
            movement_type=IngredientMovementLog.MOVEMENT_CONSUMPTION).count(), 0)

        # 5. Repeat cancellation: zero additional ledger effects
        #    (invalid cancelled→cancelled transition must fail cleanly).
        self._cancel_raw(order)
        legacy.refresh_from_db(); ingr.refresh_from_db()
        self.assertEqual(legacy.stock_quantity, 10)
        self.assertEqual(ingr.reserved_quantity, Decimal('0.000'))
        self.assertEqual(ingr.on_hand_quantity, Decimal('100.000'))
        self.assertEqual(IngredientMovementLog.objects.filter(order=order).count(),
                         movements.count())
