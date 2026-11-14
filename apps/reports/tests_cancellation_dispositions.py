"""
Phase 7.4 Report B — Cancellation Dispositions report tests (decision record
§7; implementation plan §8.2, cases CD-1..CD-12).

Authoritative source: OrderCancellationDisposition (append-only; the report is
read-only, so tests create Order + OrderItem + disposition rows directly with
valid post-H7 statuses). decided_at is backdated with update() where a
specific date is needed (auto_now_add ignores supplied values).
"""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.orders.models import Order, OrderCancellationDisposition, OrderItem

User = get_user_model()

CD_URL = reverse('reports:cancellation_dispositions')
TODAY = timezone.localdate()


def _user(username, role='admin'):
    u = User.objects.create_user(username=username, password='pass123')
    u.role = role
    u.save()
    return u


_seq = 0


def _cancelled_order_with_item(product_name='Latte',
                               inventory_mode='legacy', quantity=3):
    """Create a cancelled order + one order item, ready for a disposition."""
    global _seq
    _seq += 1
    order = Order.objects.create(
        customer_name=f'CD Customer {_seq}',
        status='cancelled', is_paid=False,
        order_type='dine_in',
        total=Decimal('100.00'), subtotal=Decimal('100.00'),
    )
    item = OrderItem.objects.create(
        order=order,
        product_name=product_name,
        size='none',
        quantity=quantity,
        unit_price=Decimal('50.00'),
        subtotal=Decimal('50.00') * quantity,
        inventory_mode=inventory_mode,
    )
    return order, item


def _disposition(item, *, ledger='legacy', source='system_unpaid',
                 order_item_quantity=3, prepared=0, unprepared=3,
                 discarded=0, days_ago=0):
    """Write one cancellation disposition (CHECK-valid by construction:
    prepared + unprepared == order_item_quantity; discarded <= prepared)."""
    disp = OrderCancellationDisposition.objects.create(
        order=item.order,
        order_item=item,
        ledger=ledger,
        order_item_quantity=order_item_quantity,
        prepared_units=prepared,
        unprepared_units=unprepared,
        discarded_units=discarded,
        source=source,
    )
    if days_ago:
        moved = timezone.now() - datetime.timedelta(days=days_ago)
        OrderCancellationDisposition.objects.filter(pk=disp.pk).update(
            decided_at=moved
        )
    return disp


class CancellationDispositionTestBase(TestCase):

    def setUp(self):
        self.admin = _user('cd_admin')
        self.client = Client()
        self.client.force_login(self.admin)

    def _get(self, url=CD_URL):
        return self.client.get(url)


class CancellationDispositionAccessTests(CancellationDispositionTestBase):

    def test_admin_required(self):
        # CD-1 — same access matrix as IU-1.
        self.client.logout()
        resp = self.client.get(CD_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.url)

        cashier = _user('cd_cashier', role='cashier')
        self.client.force_login(cashier)
        resp = self.client.get(CD_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertNotEqual(resp.url, CD_URL)

        self.client.force_login(self.admin)
        self.assertEqual(self._get().status_code, 200)


class CancellationDispositionContentTests(CancellationDispositionTestBase):

    def test_zero_quantity_dispositions_included(self):
        # CD-2 — A13: an all-zero system_unpaid row is an ordinary row with
        # its own line count; it is neither hidden nor merged away.
        _order, item = _cancelled_order_with_item('Latte')
        _disposition(item, ledger='legacy', source='system_unpaid',
                     order_item_quantity=3, prepared=0, unprepared=3,
                     discarded=0)
        resp = self._get()
        content = resp.content.decode()
        self.assertIn('Latte', content)
        self.assertIn('System (unpaid)', content)
        # Context: the row contributes line_count=1 with zero sums.
        legacy_group = resp.context['ledger_groups'][0]
        self.assertEqual(legacy_group['ledger'], 'legacy')
        rows = legacy_group['sources'][0]['rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['line_count'], 1)
        self.assertEqual(rows[0]['prepared'], 0)
        self.assertEqual(rows[0]['discarded'], 0)

    def test_prepared_unprepared_discarded_separate(self):
        # CD-3 — A14: three distinct figures; never one combined total.
        _order, item = _cancelled_order_with_item('Mocha')
        _disposition(item, ledger='legacy', source='staff_declared',
                     order_item_quantity=3, prepared=2, unprepared=1,
                     discarded=1)
        resp = self._get()
        content = resp.content.decode()
        # The approved column headers are present.
        self.assertIn('Prepared', content)
        self.assertIn('Unprepared', content)
        self.assertIn('Discarded', content)
        # Context: the row's three figures are separate values.
        rows = resp.context['ledger_groups'][0]['sources'][0]['rows']
        self.assertEqual(rows[0]['prepared'], 2)
        self.assertEqual(rows[0]['unprepared'], 1)
        self.assertEqual(rows[0]['discarded'], 1)

    def test_ledger_labels_present(self):
        # CD-4 — A15: approved group labels, rows land in the right group.
        _order, legacy_item = _cancelled_order_with_item('Legacy Prod')
        _disposition(legacy_item, ledger='legacy', source='system_ready',
                     order_item_quantity=2, prepared=2, unprepared=0)
        _order2, ingr_item = _cancelled_order_with_item(
            'Ingredient Prod', inventory_mode='ingredient')
        _disposition(ingr_item, ledger='ingredient', source='system_ready',
                     order_item_quantity=2, prepared=2, unprepared=0)
        resp = self._get()
        content = resp.content.decode()
        self.assertIn('Legacy stock ledger', content)
        self.assertIn('Ingredient ledger', content)
        self.assertIn('Legacy Prod', content)
        self.assertIn('Ingredient Prod', content)

    def test_product_units_only(self):
        # CD-5 — A16: product units only; ingredient base-unit figures from
        # the movement ledger never leak into this page.
        from apps.inventory.models import IngredientMovementLog, InventoryItem
        _order, item = _cancelled_order_with_item('Espresso',
                                                  inventory_mode='ingredient')
        _disposition(item, ledger='ingredient', source='system_ready',
                     order_item_quantity=2, prepared=2, unprepared=0)
        # A consumption movement for the same business event, in base units.
        ingr = InventoryItem.objects.create(
            name='Espresso beans', item_type='ingredient', base_unit='g',
            on_hand_quantity=Decimal('1000.000'),
            reserved_quantity=Decimal('0.000'),
        )
        IngredientMovementLog.record(
            inventory_item=ingr,
            movement_type=IngredientMovementLog.MOVEMENT_CONSUMPTION,
            on_hand_delta=Decimal('-36.000'),
            reserved_delta=Decimal('-36.000'),
            on_hand_before=Decimal('1000.000'),
            reserved_before=Decimal('36.000'),
            reason_code='cancelled_prepared',
            order=item.order,
        )
        resp = self._get()
        content = resp.content.decode()
        # Product name and product-unit quantities appear...
        self.assertIn('Espresso', content)
        # ...but the base-unit snapshot never does: no 'Espresso beans' row,
        # no movement-ledger label, and no 36-unit figure in any rendered
        # disposition group (context scan — digits in base-template markup
        # would false-positive a content scan).
        self.assertNotIn('Espresso beans', content)
        self.assertNotIn('Cancelled after prep', content)
        rendered_figures = set()
        for group in resp.context['ledger_groups']:
            for src in group['sources']:
                for row in src['rows']:
                    rendered_figures.update({
                        str(row['prepared']), str(row['unprepared']),
                        str(row['discarded']), str(row['quantity']),
                        str(row['line_count']),
                    })
        self.assertNotIn('36', rendered_figures)

    def test_discarded_is_disposition_only(self):
        # CD-6 — A12: discarded is a disposition figure; the page carries the
        # approved note, and the same period's Ingredient Usage report is
        # unchanged by the disposition's existence.
        _order, item = _cancelled_order_with_item('Cappuccino',
                                                  inventory_mode='ingredient')
        _disposition(item, ledger='ingredient', source='staff_declared',
                     order_item_quantity=3, prepared=2, unprepared=1,
                     discarded=2)
        resp = self._get()
        content = resp.content.decode()
        self.assertIn('Discarded units are a cancellation disposition',
                      content)
        self.assertIn('2', content)

        # Report A does not ingest the disposition's discarded units.
        usage = self.client.get(reverse('reports:ingredient_usage'))
        self.assertEqual(usage.status_code, 200)
        self.assertIn('No consumption recorded in this period.',
                      usage.content.decode())

    def test_date_basis_is_decided_at(self):
        # CD-7 — A6: decided_at drives the window, not the order's created_at.
        _order, item_out = _cancelled_order_with_item('OutsideWindow')
        _disposition(item_out, days_ago=40)  # decided long ago
        _order2, item_in = _cancelled_order_with_item('InsideWindow')
        _disposition(item_in)                # decided today
        # Reverse case: order created long ago, disposition decided today.
        Order.objects.filter(pk=_order2.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=45)
        )

        resp = self._get()
        content = resp.content.decode()
        self.assertIn('InsideWindow', content)
        self.assertNotIn('OutsideWindow', content)

    def test_source_labels(self):
        # CD-8 — all three sources render their human labels.
        _order, item_a = _cancelled_order_with_item('ProdA')
        _disposition(item_a, source='staff_declared')
        _order_b, item_b = _cancelled_order_with_item('ProdB')
        _disposition(item_b, source='system_ready')
        _order_c, item_c = _cancelled_order_with_item('ProdC')
        _disposition(item_c, source='system_unpaid')
        resp = self._get()
        content = resp.content.decode()
        self.assertIn('Staff declared', content)
        self.assertIn('System (ready)', content)
        self.assertIn('System (unpaid)', content)

    def test_legacy_and_ingredient_both_listed(self):
        # CD-9 — one disposition per ledger → both groups populated (A15).
        _order, legacy_item = _cancelled_order_with_item('Only Legacy')
        _disposition(legacy_item, ledger='legacy')
        _order2, ingr_item = _cancelled_order_with_item(
            'Only Ingredient', inventory_mode='ingredient')
        _disposition(ingr_item, ledger='ingredient')
        resp = self._get()
        content = resp.content.decode()
        self.assertIn('Only Legacy', content)
        self.assertIn('Only Ingredient', content)

    def test_empty_state(self):
        # CD-10 — audit correction #3: with no dispositions at all, BOTH
        # ledger headings still render, the page-level empty message appears,
        # and each group shows its per-ledger empty state.
        resp = self.client.get(CD_URL + '?start=2026-01-01&end=2026-01-05')
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn('No cancellations recorded in this period.', content)
        self.assertIn('Legacy stock ledger', content)
        self.assertIn('Ingredient ledger', content)
        self.assertIn('No legacy stock ledger cancellations recorded in this period.', content)
        self.assertIn('No ingredient ledger cancellations recorded in this period.', content)

    def test_one_populated_one_empty_ledger(self):
        # Audit correction #3 — a populated legacy group and an empty
        # ingredient group coexist: the populated one renders rows, the empty
        # one shows only its per-ledger empty state.
        _order, legacy_item = _cancelled_order_with_item('Legacy Only Prod')
        _disposition(legacy_item, ledger='legacy', source='system_unpaid')
        resp = self._get()
        content = resp.content.decode()
        self.assertIn('Legacy Only Prod', content)
        self.assertIn('Legacy stock ledger', content)
        self.assertIn('Ingredient ledger', content)
        # No page-level empty banner (the result set is not empty)...
        self.assertNotIn('No cancellations recorded in this period.', content)
        # ...but the empty ingredient group carries its per-ledger message.
        self.assertIn('No ingredient ledger cancellations recorded in this period.', content)
        self.assertNotIn('No legacy stock ledger cancellations recorded in this period.', content)

    def test_mobile_card_layout(self):
        # Audit correction #2 — plan §5.4 mobile fallback: one
        # report-card-item per product row carrying all three disposition
        # quantities separately (A14) plus the line count, alongside the
        # desktop table; ledger grouping preserved.
        _order, item = _cancelled_order_with_item('Mobile Prod')
        _disposition(item, ledger='legacy', source='staff_declared',
                     order_item_quantity=3, prepared=2, unprepared=1,
                     discarded=1)
        resp = self._get()
        content = resp.content.decode()
        self.assertIn('reports-mobile-cards', content)
        self.assertIn('report-card-item', content)
        # Desktop tables remain for wider screens.
        self.assertIn('reports-table-wrapper', content)
        # Card sub-line carries the three separate figures.
        self.assertIn('Prepared 2 &middot; Unprepared 1 &middot; Discarded 1', content)
        # Line count card value.
        self.assertIn('1 line', content)
        # Ledger grouping preserved in the card stream.
        self.assertIn('Legacy stock ledger', content)

    def test_deterministic_ordering(self):
        # CD-12 — same product name rows render in stable (name, source) order.
        _order, item_a = _cancelled_order_with_item('Same Name')
        _disposition(item_a, source='system_unpaid')
        _order_b, item_b = _cancelled_order_with_item('Same Name')
        _disposition(item_b, source='staff_declared')
        resp = self._get()
        content = resp.content.decode()
        staff_pos = content.find('Staff declared')
        unpaid_pos = content.find('System (unpaid)')
        self.assertGreater(min(staff_pos, unpaid_pos), 0)
        # Same-name rows must both render, deterministically ordered by
        # product name then source — 'Staff declared' sorts before
        # 'System (unpaid)' alphabetically.
        self.assertLess(staff_pos, unpaid_pos)


class CancellationDispositionQueryBudgetTests(CancellationDispositionTestBase):

    def test_query_budget(self):
        # CD-11 — plan §3.4: maximum 3 queries per request (2 middleware:
        # django_session + accounts_customuser, plus the single grouped
        # aggregation). Uses the project's canonical non-scaling pattern
        # (warm-up request first, then compare captures while doubling the
        # data) — see tests_query_performance.py
        # test_query_count_stable_with_many_orders.
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        def _seed_dispositions(count, start_index):
            for i in range(start_index, start_index + count):
                order = Order.objects.create(
                    customer_name=f'Bulk CD {i}',
                    status='cancelled', is_paid=False,
                    order_type='dine_in',
                    total=Decimal('150.00'), subtotal=Decimal('150.00'),
                )
                for j in range(3):
                    item = OrderItem.objects.create(
                        order=order,
                        product_name=f'Bulk Prod {i}-{j}',
                        size='none', quantity=2,
                        unit_price=Decimal('50.00'),
                        subtotal=Decimal('100.00'),
                        inventory_mode='legacy',
                    )
                    _disposition(item, source='system_ready',
                                 order_item_quantity=2, prepared=2,
                                 unprepared=0)

        _seed_dispositions(40, 0)

        # Warm-up request — establishes baseline incl. one-time middleware
        # laziness before any counting assertion.
        with CaptureQueriesContext(connection) as warm_ctx:
            resp = self.client.get(CD_URL)
        self.assertEqual(resp.status_code, 200)
        count_120 = len(warm_ctx.captured_queries)

        # Double the data — query count must not increase (O(1), no N+1).
        _seed_dispositions(40, 40)
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(CD_URL)
        self.assertEqual(resp.status_code, 200)
        count_240 = len(ctx.captured_queries)

        # Plan §3.4 authoritative budget: maximum 3 queries.
        self.assertLessEqual(count_240, 3,
                             f'cancellation_dispositions executed '
                             f'{count_240} queries; budget is 3')
        # Data-volume independence (plan §3.4 "no N+1").
        self.assertLessEqual(count_240, count_120,
                             'query count increased with data volume')
