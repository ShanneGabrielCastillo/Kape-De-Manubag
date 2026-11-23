"""
Regression tests for audit finding C-4 — admin order mutation protection.

Two fields decide which inventory ledger settled an order and whether legacy
stock is currently deducted:

* ``OrderItem.inventory_mode`` — placement-time snapshot of the ledger used for
  the line. Cancellation and restoration select lines by this value.
* ``Order.stock_deducted`` — idempotency flag for legacy restoration.

Both are written only by the inventory services. These tests prove the Django
Admin cannot change them after order creation, while ordinary metadata and
status management still work, and the inline formset cannot add or remove
lines on a placed order.

All tests use Django's isolated test database.
"""

from decimal import Decimal

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse

from apps.menu.models import Category, Product
from apps.orders.admin import OrderAdmin, OrderItemInline
from apps.orders.models import Order, OrderItem

PASSWORD = 'order-inv-guard-pass'


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _superuser(username='order_inv_guard_root'):
    return get_user_model().objects.create_superuser(
        username=username, password=PASSWORD, role='admin',
    )


def _category(name='Order Guard Drinks'):
    return Category.objects.create(name=name, slug=name.lower().replace(' ', '-'))


def _product(category, *, name='Guard Latte', mode=Product.INVENTORY_TRACKING_LEGACY):
    return Product.objects.create(
        category=category,
        name=name,
        price=Decimal('60.00'),
        stock_quantity=100,
        inventory_tracking_mode=mode,
    )


def _order(*, status='preparing', stock_deducted=False, is_paid=True):
    order = Order.objects.create(
        customer_name='Guard Customer',
        order_type='dine_in',
        status=status,
        is_paid=is_paid,
        subtotal=Decimal('120.00'),
        total=Decimal('120.00'),
        amount_paid=Decimal('120.00'),
    )
    if stock_deducted:
        order.stock_deducted = True
        order.save(update_fields=['stock_deducted'])
    return order


def _item(order, product, *, mode=OrderItem.INVENTORY_MODE_LEGACY, quantity=2):
    return OrderItem.objects.create(
        order=order,
        product=product,
        product_name=product.name,
        category_name=product.category.name,
        packaging_eligible=False,
        size='none',
        quantity=quantity,
        unit_price=Decimal('60.00'),
        subtotal=Decimal('120.00'),
        inventory_mode=mode,
    )


def _order_payload(order, **overrides):
    """A complete, valid admin change-form payload for the given order."""
    data = {
        'customer_name': order.customer_name,
        'customer_phone': order.customer_phone,
        'table_number': order.table_number,
        'order_type': order.order_type,
        'status': order.status,
        'notes': order.notes,
        'subtotal': str(order.subtotal),
        'discount': str(order.discount),
        'packaging_fee': str(order.packaging_fee),
        'total': str(order.total),
        'payment_method': order.payment_method,
        'amount_paid': str(order.amount_paid),
        'change_amount': str(order.change_amount),
        'gcash_status': order.gcash_status,
        'gcash_reference': order.gcash_reference,
        'gcash_notes': order.gcash_notes,
        'request_token': order.request_token or '',
        'tracking_token': order.tracking_token or '',
        'queue_number': order.queue_number if order.queue_number is not None else '',
    }
    if order.is_paid:
        data['is_paid'] = 'on'
    data.update(overrides)
    return data


def _inline_payload(order, items, **overrides):
    data = {
        'items-TOTAL_FORMS': str(len(items)),
        'items-INITIAL_FORMS': str(len(items)),
        'items-MIN_NUM_FORMS': '0',
        'items-MAX_NUM_FORMS': '1000',
    }
    for index, item in enumerate(items):
        data[f'items-{index}-id'] = str(item.pk)
        data[f'items-{index}-order'] = str(order.pk)
    data.update(overrides)
    return data


def _admin_request(user):
    request = RequestFactory().get('/admin/orders/order/')
    request.user = user
    return request


# ═════════════════════════════════════════════════════════════════════════════
# 1. OrderItem.inventory_mode
# ═════════════════════════════════════════════════════════════════════════════

class OrderItemInventoryModeAdminTests(TestCase):
    def setUp(self):
        self.root = _superuser()
        self.client.force_login(self.root)
        self.category = _category()
        self.product = _product(self.category)
        self.order = _order(stock_deducted=True)
        self.item = _item(self.order, self.product)
        self.url = reverse('admin:orders_order_change', args=[self.order.pk])

    def test_inventory_mode_is_read_only_in_inline_form(self):
        inline = OrderItemInline(Order, AdminSite())
        request = _admin_request(self.root)

        self.assertIn(
            'inventory_mode',
            inline.get_readonly_fields(request, self.order),
        )
        formset = inline.get_formset(request, obj=self.order)
        self.assertNotIn('inventory_mode', formset.form.base_fields)

    def test_crafted_inline_post_cannot_change_inventory_mode(self):
        response = self.client.post(
            self.url,
            _order_payload(
                self.order,
                customer_name='Guard Customer Renamed',
            ) | _inline_payload(
                self.order,
                [self.item],
                **{'items-0-inventory_mode': 'ingredient'},
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(
            self.item.inventory_mode, OrderItem.INVENTORY_MODE_LEGACY)
        self.order.refresh_from_db()
        # The parent form did save, proving the POST was accepted.
        self.assertEqual(self.order.customer_name, 'Guard Customer Renamed')
        self.assertTrue(self.order.stock_deducted)

    def test_crafted_inline_post_cannot_delete_a_line(self):
        response = self.client.post(
            self.url,
            _order_payload(self.order) | _inline_payload(
                self.order, [self.item], **{'items-0-DELETE': 'on'},
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(OrderItem.objects.filter(order=self.order).count(), 1)

    def test_crafted_inline_post_cannot_add_a_line(self):
        extra = _inline_payload(
            self.order,
            [self.item],
            **{
                'items-TOTAL_FORMS': '2',
                'items-1-id': '',
                'items-1-order': str(self.order.pk),
                'items-1-inventory_mode': 'legacy',
            },
        )
        response = self.client.post(self.url, _order_payload(self.order) | extra)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(OrderItem.objects.filter(order=self.order).count(), 1)

    def test_terminal_order_post_cannot_change_inventory_mode(self):
        terminal = _order(status='completed', stock_deducted=True)
        terminal_item = _item(terminal, self.product)
        url = reverse('admin:orders_order_change', args=[terminal.pk])

        response = self.client.post(
            url,
            _order_payload(
                terminal,
                customer_name='Should Not Change',
            ) | _inline_payload(
                terminal,
                [terminal_item],
                **{'items-0-inventory_mode': 'ingredient'},
            ),
        )

        self.assertEqual(response.status_code, 302)
        terminal_item.refresh_from_db()
        terminal.refresh_from_db()
        self.assertEqual(
            terminal_item.inventory_mode, OrderItem.INVENTORY_MODE_LEGACY)
        self.assertEqual(terminal.customer_name, 'Guard Customer')
        self.assertTrue(terminal.stock_deducted)


# ═════════════════════════════════════════════════════════════════════════════
# 2. Order.stock_deducted
# ═════════════════════════════════════════════════════════════════════════════

class OrderStockDeductedAdminTests(TestCase):
    def setUp(self):
        self.root = _superuser()
        self.client.force_login(self.root)
        self.category = _category('Order Guard Stock')

    def _change_url(self, order):
        return reverse('admin:orders_order_change', args=[order.pk])

    def test_stock_deducted_is_read_only_in_admin_form(self):
        order = _order(stock_deducted=True)
        model_admin = OrderAdmin(Order, AdminSite())
        request = _admin_request(self.root)

        self.assertIn(
            'stock_deducted',
            model_admin.get_readonly_fields(request, order),
        )
        form_class = model_admin.get_form(request, obj=order)
        self.assertNotIn('stock_deducted', form_class.base_fields)

    def test_crafted_post_cannot_clear_stock_deducted(self):
        order = _order(stock_deducted=True)
        product = _product(self.category)
        item = _item(order, product)

        response = self.client.post(
            self._change_url(order),
            _order_payload(
                order,
                customer_name='Stock Guard Renamed',
            ) | _inline_payload(order, [item]),
        )

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertTrue(order.stock_deducted)
        self.assertEqual(order.customer_name, 'Stock Guard Renamed')

    def test_crafted_post_cannot_set_stock_deducted(self):
        order = _order(stock_deducted=False)
        product = _product(self.category, name='Guard Ingredient Latte',
                           mode=Product.INVENTORY_TRACKING_INGREDIENT)
        item = _item(order, product, mode=OrderItem.INVENTORY_MODE_INGREDIENT)

        response = self.client.post(
            self._change_url(order),
            _order_payload(order, stock_deducted='on')
            | _inline_payload(order, [item]),
        )

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertFalse(order.stock_deducted)

    def test_permitted_metadata_still_saves(self):
        order = _order(status='preparing', stock_deducted=True)
        product = _product(self.category, name='Guard Metadata Latte')
        item = _item(order, product)

        response = self.client.post(
            self._change_url(order),
            _order_payload(
                order,
                customer_name='Metadata Customer',
                notes='Allergy note.',
                table_number='12',
            ) | _inline_payload(order, [item]),
        )

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.customer_name, 'Metadata Customer')
        self.assertEqual(order.notes, 'Allergy note.')
        self.assertEqual(order.table_number, '12')
        self.assertEqual(order.status, 'preparing')
        self.assertTrue(order.stock_deducted)

    def test_legitimate_status_transition_still_works(self):
        order = _order(status='awaiting_payment', is_paid=True)
        product = _product(self.category, name='Guard Status Latte')
        item = _item(order, product)

        response = self.client.post(
            self._change_url(order),
            _order_payload(order, status='preparing')
            | _inline_payload(order, [item]),
        )

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, 'preparing')
        self.assertFalse(order.stock_deducted)
        self.assertEqual(
            item.inventory_mode, OrderItem.INVENTORY_MODE_LEGACY)

    def test_direct_cancellation_is_still_blocked(self):
        order = _order(status='preparing', stock_deducted=True)
        product = _product(self.category, name='Guard Cancel Latte')
        item = _item(order, product)

        response = self.client.post(
            self._change_url(order),
            _order_payload(order, status='cancelled')
            | _inline_payload(order, [item]),
        )

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, 'preparing')
        self.assertTrue(order.stock_deducted)

    def test_admin_delete_paths_cannot_remove_orders(self):
        order = _order()

        delete_response = self.client.post(
            reverse('admin:orders_order_delete', args=[order.pk]),
            {'post': 'yes'},
        )
        self.assertEqual(delete_response.status_code, 403)

        bulk_response = self.client.post(
            reverse('admin:orders_order_changelist'),
            {
                'action': 'delete_selected',
                '_selected_action': [str(order.pk)],
            },
        )
        self.assertIn(bulk_response.status_code, (200, 302))
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())

    def test_admin_exposes_no_custom_actions(self):
        # The only mutating custom admin path is the intermediate cancellation
        # view, which calls on_status_changed(); it is not a bulk action.
        self.assertFalse(OrderAdmin.actions)


# ═════════════════════════════════════════════════════════════════════════════
# 3. Admin order creation — disabled
# ═════════════════════════════════════════════════════════════════════════════

class OrderAdminCreationDisabledTests(TestCase):
    """Orders must be created through checkout/POS, not the Admin add form.

    The add view is the only Admin path that can insert a new Order.  It must
    reject both GET and crafted POST requests, while existing change pages
    remain fully accessible.
    """

    def setUp(self):
        self.root = _superuser('order_create_guard_root')
        self.client.force_login(self.root)

    def test_add_page_is_forbidden(self):
        response = self.client.get(reverse('admin:orders_order_add'))

        self.assertEqual(response.status_code, 403)

    def test_crafted_add_post_creates_no_order(self):
        count_before = Order.objects.count()

        response = self.client.post(
            reverse('admin:orders_order_add'),
            {
                'customer_name': 'Crafted Admin Order',
                'order_type': 'dine_in',
                'status': 'awaiting_payment',
                'subtotal': '0.00',
                'discount': '0.00',
                'packaging_fee': '0.00',
                'total': '0.00',
                'amount_paid': '0.00',
                'change_amount': '0.00',
                'gcash_status': 'none',
                'gcash_reference': '',
                'gcash_notes': '',
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(Order.objects.count(), count_before)
        self.assertFalse(Order.objects.filter(customer_name='Crafted Admin Order').exists())

    def test_change_page_remains_accessible(self):
        order = _order(status='preparing')

        response = self.client.get(
            reverse('admin:orders_order_change', args=[order.pk]))

        self.assertEqual(response.status_code, 200)

    def test_changelist_has_no_add_control(self):
        response = self.client.get(reverse('admin:orders_order_changelist'))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse('admin:orders_order_add'))


# ═════════════════════════════════════════════════════════════════════════════
# 4. Order tokens — read-only in Admin
# ═════════════════════════════════════════════════════════════════════════════

class OrderAdminTokenProtectionTests(TestCase):
    """request_token and tracking_token must never be altered through Admin.

    request_token is the idempotency key for order placement; tracking_token is
    the customer's bearer credential for tracking, payment, and receipts.
    """

    def setUp(self):
        self.root = _superuser('order_token_guard_root')
        self.client.force_login(self.root)

    def _token_order(self, status='preparing', is_paid=True):
        order = _order(status=status, is_paid=is_paid)
        order.request_token = f'guard-request-token-{order.pk}'
        order.save(update_fields=['request_token'])
        order.refresh_from_db()
        return order

    def test_tokens_are_read_only_in_active_order_form(self):
        order = self._token_order()
        model_admin = OrderAdmin(Order, AdminSite())
        request = _admin_request(self.root)

        readonly = model_admin.get_readonly_fields(request, order)
        self.assertIn('request_token', readonly)
        self.assertIn('tracking_token', readonly)

        form_class = model_admin.get_form(request, obj=order)
        self.assertNotIn('request_token', form_class.base_fields)
        self.assertNotIn('tracking_token', form_class.base_fields)

    def test_crafted_post_cannot_change_active_order_tokens(self):
        order = self._token_order(status='preparing')
        original_request_token = order.request_token
        original_tracking_token = order.tracking_token

        response = self.client.post(
            reverse('admin:orders_order_change', args=[order.pk]),
            _order_payload(
                order,
                customer_name='Token Guard Renamed',
                request_token='forged-request-token',
                tracking_token='forged-tracking-token',
            ) | _inline_payload(order, []),
        )

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.request_token, original_request_token)
        self.assertEqual(order.tracking_token, original_tracking_token)
        # A permitted descriptive edit in the same POST still saved.
        self.assertEqual(order.customer_name, 'Token Guard Renamed')

    def test_crafted_post_cannot_change_terminal_order_tracking_token(self):
        for status in ('completed', 'cancelled'):
            with self.subTest(status=status):
                order = self._token_order(
                    status=status, is_paid=(status == 'completed'))
                original_request_token = order.request_token
                original_tracking_token = order.tracking_token

                response = self.client.post(
                    reverse('admin:orders_order_change', args=[order.pk]),
                    _order_payload(
                        order,
                        request_token='forged-request-token',
                        tracking_token='forged-tracking-token',
                    ) | _inline_payload(order, []),
                )

                self.assertEqual(response.status_code, 302)
                order.refresh_from_db()
                self.assertEqual(order.tracking_token, original_tracking_token)
                self.assertEqual(order.request_token, original_request_token)
