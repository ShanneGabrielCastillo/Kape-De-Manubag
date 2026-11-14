"""
Phase 7.4 Report A — Ingredient Usage report tests (decision record §5, §6;
implementation plan §8.1, cases IU-1..IU-14).

Authoritative source: IngredientMovementLog. The report is read-only — tests
construct movement rows directly via IngredientMovementLog.record() (keeps the
DB shape CHECKs valid: consumption ⇒ reserved_delta == on_hand_delta) and
backdate created_at with update() where a specific date is needed
(auto_now_add ignores supplied values).
"""

import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.inventory.models import IngredientMovementLog, InventoryItem
from apps.orders.models import Order

User = get_user_model()

USAGE_URL = reverse('reports:ingredient_usage')
TODAY = timezone.localdate()


def _user(username, role='admin'):
    u = User.objects.create_user(username=username, password='pass123')
    u.role = role
    u.save()
    return u


def _item(name='Flour', item_type='ingredient', base_unit='g'):
    return InventoryItem.objects.create(
        name=name, item_type=item_type, base_unit=base_unit,
        on_hand_quantity=Decimal('1000.000'),
        reserved_quantity=Decimal('0.000'),
    )


def _consumption(item, delta, reason='order_ready', days_ago=0, order=None):
    """Write one consumption movement (physical: on_hand_delta < 0,
    reserved_delta == on_hand_delta per mv_shape_consumption) and backdate it."""
    magnitude = -delta
    m = IngredientMovementLog.record(
        inventory_item=item,
        movement_type=IngredientMovementLog.MOVEMENT_CONSUMPTION,
        on_hand_delta=delta,
        reserved_delta=delta,
        on_hand_before=Decimal('1000.000'),
        reserved_before=magnitude,
        reason_code=reason,
        order=order,
    )
    if days_ago:
        moved = timezone.now() - datetime.timedelta(days=days_ago)
        IngredientMovementLog.objects.filter(pk=m.pk).update(created_at=moved)
    return m


def _movement(item, movement_type, on_hand_delta, reserved_delta,
              on_hand_before, reserved_before, days_ago=0):
    """Write any movement type with explicit before-values (shape-safe)."""
    m = IngredientMovementLog.record(
        inventory_item=item,
        movement_type=movement_type,
        on_hand_delta=on_hand_delta,
        reserved_delta=reserved_delta,
        on_hand_before=on_hand_before,
        reserved_before=reserved_before,
    )
    if days_ago:
        moved = timezone.now() - datetime.timedelta(days=days_ago)
        IngredientMovementLog.objects.filter(pk=m.pk).update(created_at=moved)
    return m


class IngredientUsageTestBase(TestCase):

    def setUp(self):
        self.admin = _user('iu_admin')
        self.client = Client()
        self.client.force_login(self.admin)

    def _get(self, url=USAGE_URL):
        return self.client.get(url)


class IngredientUsageAccessTests(IngredientUsageTestBase):

    def test_admin_required(self):
        # IU-1 — anonymous → login redirect; cashier → redirected away with
        # a denial; admin → 200.
        self.client.logout()
        resp = self.client.get(USAGE_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.url)

        cashier = _user('iu_cashier', role='cashier')
        self.client.force_login(cashier)
        resp = self.client.get(USAGE_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertNotEqual(resp.url, USAGE_URL)

        self.client.force_login(self.admin)
        self.assertEqual(self._get().status_code, 200)


class IngredientUsageContentTests(IngredientUsageTestBase):

    def _all_report_figures(self, resp):
        """Collect every formatted figure on the page from the view context
        (unit totals, row totals, per-reason figures, extras). Context-based
        absence checks are immune to digits appearing in the base template
        (e.g. rgba(255,255,255) markup)."""
        figures = set()
        for sections_key in ('ingredient_sections', 'packaging_sections'):
            for section in resp.context[sections_key]:
                figures.add(section['unit_total'])
                for row in section['rows']:
                    figures.add(row['total'])
                    figures.add(row['order_ready'])
                    figures.add(row['cancelled_prepared'])
                    for extra in row['other']:
                        figures.add(extra['qty'])
        return figures

    def test_only_consumption_included(self):
        # IU-2 — every non-consumption movement is invisible to the report.
        # Magnitudes chosen so no figure string collides with another.
        item = _item('Flour', 'ingredient', 'g')
        _movement(item, IngredientMovementLog.MOVEMENT_PURCHASE,
                  Decimal('301.000'), Decimal('0.000'),
                  Decimal('700.000'), Decimal('0.000'))
        _movement(item, IngredientMovementLog.MOVEMENT_RESERVATION,
                  Decimal('0.000'), Decimal('502.000'),
                  Decimal('1000.000'), Decimal('0.000'))
        _movement(item, IngredientMovementLog.MOVEMENT_RESERVATION_RELEASE,
                  Decimal('0.000'), Decimal('-502.000'),
                  Decimal('1000.000'), Decimal('502.000'))
        _movement(item, IngredientMovementLog.MOVEMENT_WASTAGE,
                  Decimal('-63.000'), Decimal('0.000'),
                  Decimal('1000.000'), Decimal('0.000'))
        _movement(item, IngredientMovementLog.MOVEMENT_ADJUSTMENT,
                  Decimal('74.000'), Decimal('0.000'),
                  Decimal('990.000'), Decimal('0.000'))
        _consumption(item, Decimal('-250.000'))
        resp = self._get()
        figures = self._all_report_figures(resp)
        self.assertEqual(figures, {'250', '0'})
        self.assertIn('Flour', resp.content.decode())

    def test_order_ready_vs_cancelled_prepared_separated(self):
        # IU-3 — the two reason codes render as separate figures (A4).
        item = _item('Flour', 'ingredient', 'g')
        _consumption(item, Decimal('-200.000'), reason='order_ready')
        _consumption(item, Decimal('-80.000'), reason='cancelled_prepared')
        resp = self._get()
        content = resp.content.decode()
        self.assertIn('200', content)
        self.assertIn('80', content)
        # Both approved labels render distinctly.
        self.assertIn('Prepared (order ready)', content)
        self.assertIn('Cancelled after prep', content)

    def test_cancelled_prepared_not_labelled_wastage(self):
        # IU-4 — cancelled-prepared consumption keeps its own label (A11);
        # the movement-type label "Wastage" never appears on this report.
        item = _item('Flour', 'ingredient', 'g')
        _consumption(item, Decimal('-80.000'), reason='cancelled_prepared')
        resp = self._get()
        content = resp.content.decode()
        self.assertIn('Cancelled after prep', content)
        self.assertNotIn('Wastage', content)

    def test_ingredient_and_packaging_separate(self):
        # IU-5 — item types render in their own sections; no combined total
        # exists anywhere in the report structure (A7).
        ingr = _item('Flour', 'ingredient', 'g')
        pack = _item('Cup sleeve', 'packaging', 'pcs')
        _consumption(ingr, Decimal('-250.000'))
        _consumption(pack, Decimal('-10.000'))
        resp = self._get()
        content = resp.content.decode()
        self.assertIn('Ingredients', content)
        self.assertIn('Packaging', content)
        self.assertIn('250', content)
        self.assertIn('10', content)
        # No combined 260 figure exists in any report figure (context scan).
        self.assertNotIn('260', self._all_report_figures(resp))
        # The two sections are structurally separate lists.
        self.assertEqual(len(resp.context['ingredient_sections']), 1)
        self.assertEqual(len(resp.context['packaging_sections']), 1)

    def test_units_never_combined(self):
        # IU-6 — items in different base units render as separate unit blocks
        # and their quantities are never summed together (A8). NOTE: the plan
        # drafted this case as "same item name in g and pcs", but
        # InventoryItem enforces case-insensitive unique names
        # (item_name_unique_ci, models.py:244), so identical names are
        # impossible — two distinct items in different units exercise the
        # same grouping invariant.
        g_item = _item('Sugar', 'ingredient', 'g')
        pcs_item = _item('Sugar Sticks', 'ingredient', 'pcs')
        _consumption(g_item, Decimal('-250.000'))
        _consumption(pcs_item, Decimal('-5.000'))
        resp = self._get()
        content = resp.content.decode()
        self.assertIn('Grams (g)', content)
        self.assertIn('Pieces (pcs)', content)
        self.assertIn('250', content)
        self.assertIn('5', content)
        # No combined 255 figure exists in any report figure (context scan —
        # the digit string '255' also appears in base-template CSS markup,
        # so a content scan would false-positive).
        self.assertNotIn('255', self._all_report_figures(resp))

    def test_reserved_delta_never_summed(self):
        # IU-7 — reservation/release rows (big reserved_delta) change nothing.
        item = _item('Flour', 'ingredient', 'g')
        _movement(item, IngredientMovementLog.MOVEMENT_RESERVATION,
                  Decimal('0.000'), Decimal('900.000'),
                  Decimal('1000.000'), Decimal('0.000'))
        _movement(item, IngredientMovementLog.MOVEMENT_RESERVATION_RELEASE,
                  Decimal('0.000'), Decimal('-900.000'),
                  Decimal('1000.000'), Decimal('900.000'))
        _consumption(item, Decimal('-100.000'))
        resp = self._get()
        figures = self._all_report_figures(resp)
        self.assertEqual(figures, {'100', '0'})
        self.assertIn('Flour', resp.content.decode())

    def test_date_range_respects_created_at(self):
        # IU-8 — A5: created_at drives the window. Context-based checks keep
        # the assertions immune to digits in base-template markup.
        item = _item('Flour', 'ingredient', 'g')
        _consumption(item, Decimal('-310.000'), days_ago=31)  # outside default
        _consumption(item, Decimal('-5.000'), days_ago=2)     # inside default
        resp = self._get()
        figures = self._all_report_figures(resp)
        self.assertIn('5', figures)
        self.assertNotIn('310', figures)

        # Explicit range boundary honors the requested window.
        start = TODAY - datetime.timedelta(days=32)
        end = TODAY - datetime.timedelta(days=30)
        resp = self.client.get(
            reverse('reports:ingredient_usage') + f'?start={start}&end={end}'
        )
        self.assertEqual(resp.status_code, 200)
        figures = self._all_report_figures(resp)
        self.assertIn('310', figures)
        self.assertNotIn('5', figures)

    def test_snapshot_name_and_unit_rendered(self):
        # IU-9 — renamed InventoryItem does not affect the rendered snapshot.
        item = _item('Old Name', 'ingredient', 'g')
        _consumption(item, Decimal('-100.000'))
        item.name = 'Renamed Item'
        item.save(update_fields=['name'])
        resp = self._get()
        content = resp.content.decode()
        self.assertIn('Old Name', content)
        self.assertNotIn('Renamed Item', content)

    def test_magnitudes_positive(self):
        # IU-10 — negative on_hand_delta renders as a positive magnitude.
        item = _item('Flour', 'ingredient', 'g')
        _consumption(item, Decimal('-250.000'))
        resp = self._get()
        content = resp.content.decode()
        self.assertIn('250', content)
        self.assertNotIn('-250', content)

    def test_empty_state(self):
        # IU-11 — with no consumption at all, BOTH sections (Ingredients and
        # Packaging) stay visible and each shows the approved empty-state
        # message (audit correction #3: never an unexplained empty table).
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertEqual(content.count('No consumption recorded in this period.'), 2)
        self.assertIn('Ingredients', content)
        self.assertIn('Packaging', content)
        # No empty desktop tables — the tables themselves are not rendered
        # when a section has no data (only the empty state is).
        self.assertNotIn('<th>Prepared (order ready)</th>', content)

    def test_mobile_card_layout(self):
        # Audit correction #2 — plan §5.3 mobile fallback: one
        # report-card-item per item plus a unit-total card, alongside the
        # desktop table; sections stay separate.
        ingr = _item('Flour', 'ingredient', 'g')
        pack = _item('Cup sleeve', 'packaging', 'pcs')
        _consumption(ingr, Decimal('-250.000'))
        _consumption(pack, Decimal('-10.000'))
        resp = self._get()
        content = resp.content.decode()
        self.assertIn('reports-mobile-cards', content)
        self.assertIn('report-card-item', content)
        # Desktop tables remain for wider screens.
        self.assertIn('reports-table-wrapper', content)
        # Card sub-lines carry the reason split (never combined).
        self.assertIn('Prepared 250 &middot; Cancelled after prep 0', content)
        self.assertIn('Prepared 10 &middot; Cancelled after prep 0', content)
        # Unit sections preserved in the card stream too.
        self.assertIn('Grams (g)', content)
        self.assertIn('Pieces (pcs)', content)

    def test_ingredient_only_result(self):
        # Audit correction #3 — ingredient records present, packaging empty:
        # packaging section shows the approved empty state, ingredient
        # section shows its table.
        ingr = _item('Flour', 'ingredient', 'g')
        _consumption(ingr, Decimal('-250.000'))
        resp = self._get()
        content = resp.content.decode()
        self.assertIn('Flour', content)
        self.assertEqual(
            content.count('No consumption recorded in this period.'), 1,
            'only the empty (packaging) section shows the empty state',
        )
        self.assertIn('Ingredients', content)
        self.assertIn('Packaging', content)

    def test_packaging_only_result(self):
        # Audit correction #3 — packaging records present, ingredients empty.
        pack = _item('Cup sleeve', 'packaging', 'pcs')
        _consumption(pack, Decimal('-10.000'))
        resp = self._get()
        content = resp.content.decode()
        self.assertIn('Cup sleeve', content)
        self.assertEqual(
            content.count('No consumption recorded in this period.'), 1,
            'only the empty (ingredients) section shows the empty state',
        )
        self.assertIn('Ingredients', content)
        self.assertIn('Packaging', content)

    def test_no_reconciliation_copy(self):
        # IU-12 — A10 copy present on every render.
        item = _item('Flour', 'ingredient', 'g')
        _consumption(item, Decimal('-100.000'))
        resp = self._get()
        self.assertIn('not a stock reconciliation', resp.content.decode())

    def test_unknown_reason_code_fail_visible(self):
        # IU-13 — an unexpected reason code renders under a visible,
        # capitalized raw-code label instead of disappearing (§5.2).
        item = _item('Flour', 'ingredient', 'g')
        _consumption(item, Decimal('-40.000'), reason='weird')
        resp = self._get()
        content = resp.content.decode()
        self.assertIn('Weird', content)
        self.assertIn('40', content)


class IngredientUsageQueryBudgetTests(IngredientUsageTestBase):

    def test_query_budget(self):
        # IU-14 — plan §3.4: maximum 3 queries per request (2 middleware:
        # django_session + accounts_customuser, plus the single grouped
        # aggregation). Uses the project's canonical non-scaling pattern
        # (warm-up request first, then compare captures while doubling the
        # data) — see tests_query_performance.py
        # test_query_count_stable_with_many_orders.
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        def _seed_items(count, start_index):
            for i in range(start_index, start_index + count):
                item = _item(f'Item {i}', 'ingredient', 'g')
                for j in range(30):
                    _consumption(item, Decimal('-1.000'))

        _seed_items(40, 0)

        # Warm-up request — establishes baseline incl. one-time middleware
        # laziness before any counting assertion.
        with CaptureQueriesContext(connection) as warm_ctx:
            resp = self.client.get(USAGE_URL)
        self.assertEqual(resp.status_code, 200)
        count_40 = len(warm_ctx.captured_queries)

        # Double the data — query count must not increase (O(1), no N+1).
        _seed_items(40, 40)
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(USAGE_URL)
        self.assertEqual(resp.status_code, 200)
        count_80 = len(ctx.captured_queries)

        # Plan §3.4 authoritative budget: maximum 3 queries.
        self.assertLessEqual(count_80, 3,
                             f'ingredient_usage executed {count_80} queries; '
                             f'budget is 3')
        # Data-volume independence (plan §3.4 "no N+1").
        self.assertLessEqual(count_80, count_40,
                             'query count increased with data volume')
