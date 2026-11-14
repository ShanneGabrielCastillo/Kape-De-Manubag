"""
Phase 7.5 — Inventory Ledger Reconciliation tests (admin-only, all-time,
per-item ledger-internal consistency report plus its Excel export).

Cases follow the approved Phase 7.5 blueprint test matrix. Fixtures create
InventoryItems with their FINAL balances and build movement chains via
IngredientMovementLog.record() (shape-CHECK-safe); deliberately broken
chains/mismatches are created the same way, because record() accepts
caller-supplied before/after values and only the DB CHECKs constrain them.

Rows that would violate DB CHECK constraints (negative levels, wrong
shapes, arithmetic disagreement) are represented as UNSAVED
IngredientMovementLog instances and fed directly to
apps.reports.views._reconcile_item() — the actual production
reconciliation logic — so malformed-data detection is exercised without
inserting CHECK-violating rows into ANY database. This keeps the suite
portable across SQLite and PostgreSQL (no engine-specific commands, no
raw SQL). Database-backed integration tests through the report view
remain in place for valid ledger records and normal report behaviour.
"""

import datetime
import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.inventory.models import IngredientMovementLog, InventoryItem
from apps.reports.views import _reconcile_item

User = get_user_model()

REC_URL = reverse('reports:ingredient_reconciliation')
EXPORT_URL = reverse('reports:ingredient_reconciliation_export')
ZERO = Decimal("0.000")


def _user(username, role='admin'):
    u = User.objects.create_user(username=username, password='pass123')
    u.role = role
    u.save()
    return u


def _item(name='TEST Reconcile Item', item_type='ingredient', base_unit='g',
          on_hand=ZERO, reserved=ZERO, is_active=True):
    return InventoryItem.objects.create(
        name=name, item_type=item_type, base_unit=base_unit,
        on_hand_quantity=Decimal(str(on_hand)),
        reserved_quantity=Decimal(str(reserved)),
        is_active=is_active,
    )


def _movement(item, mtype, on_delta, res_delta, on_before, res_before,
              reason='', days_ago=0):
    m = IngredientMovementLog.record(
        inventory_item=item,
        movement_type=mtype,
        on_hand_delta=Decimal(str(on_delta)),
        reserved_delta=Decimal(str(res_delta)),
        on_hand_before=Decimal(str(on_before)),
        reserved_before=Decimal(str(res_before)),
        reason_code=reason,
    )
    if days_ago:
        moved = timezone.now() - datetime.timedelta(days=days_ago)
        IngredientMovementLog.objects.filter(pk=m.pk).update(created_at=moved)
    return m


def _download(client):
    resp = client.get(EXPORT_URL)
    content = b''.join(
        resp.streaming_content if hasattr(resp, 'streaming_content')
        else [resp.content]
    )
    return resp, content


def _load(content):
    import openpyxl
    return openpyxl.load_workbook(io.BytesIO(content))


class ReconciliationTestBase(TestCase):

    def setUp(self):
        self.admin = _user('rec_admin')
        self.client = Client()
        self.client.force_login(self.admin)

    def _page(self):
        resp = self.client.get(REC_URL)
        self.assertEqual(resp.status_code, 200)
        return resp


class ReconciliationAccessTests(ReconciliationTestBase):

    def test_admin_only(self):
        # Anonymous -> login redirect; cashier -> redirected away; admin OK.
        self.client.logout()
        resp = self.client.get(REC_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.url)

        cashier = _user('rec_cashier', role='cashier')
        self.client.force_login(cashier)
        resp = self.client.get(REC_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertNotEqual(resp.url, REC_URL)

        self.client.force_login(self.admin)
        self.assertEqual(self._page().status_code, 200)

        # Export is equally protected.
        self.client.logout()
        resp = self.client.get(EXPORT_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.url)
        cashier = _user('rec_cashier2', role='cashier')
        self.client.force_login(cashier)
        self.assertEqual(self.client.get(EXPORT_URL).status_code, 302)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(EXPORT_URL).status_code, 200)


class ReconciliationStatusTests(ReconciliationTestBase):
    """Per-item statuses and findings on the reconciliation page."""

    def test_fully_reconciled_on_hand_chain(self):
        # purchase -> reservation -> consumption, closing onto the persisted
        # balance (consumption always settles a prior reservation).
        item = _item(on_hand=850)
        _movement(item, 'purchase', 1000, 0, 0, 0)
        _movement(item, 'reservation', 0, 150, 1000, 0)
        _movement(item, 'consumption', -150, -150, 1000, 150,
                  reason='order_ready')
        resp = self._page()
        statuses = [(r['on_hand_status'], r['reserved_status'])
                    for r in resp.context['results']]
        self.assertEqual(statuses, [('RECONCILED', 'RECONCILED')])
        self.assertIn('RECONCILED', resp.content.decode())

    def test_fully_reconciled_reserved_chain(self):
        # reservation -> release closing onto persisted reserved balance
        # (after a release the reserved balance is back to zero).
        item = _item(on_hand=1000, reserved=0)
        _movement(item, 'reservation', 0, 300, 1000, 0)
        _movement(item, 'reservation_release', 0, -300, 1000, 300)
        resp = self._page()
        # Both ledgers reconcile (context assertions — the mobile fallback
        # repeats status strings in the HTML).
        statuses = [(r['on_hand_status'], r['reserved_status'])
                    for r in resp.context['results']]
        self.assertEqual(statuses, [('RECONCILED', 'RECONCILED')])
        self.assertIn('RECONCILED', resp.content.decode())

    def test_implicit_opening_balances_displayed(self):
        # Nonzero first-movement before values are informational openings.
        item = _item(on_hand=850)
        _movement(item, 'purchase', 1000, 0, 0, 0)
        _movement(item, 'consumption', -150, -150, 1000, 150)
        resp = self._page()
        content = resp.content.decode()
        self.assertIn('Implicit opening', content)
        self.assertIn('from first movement', content)

    def test_nonzero_implicit_opening_reconciles(self):
        # A single consumption row as the FIRST movement: its before-values
        # are the implicit openings (900 on-hand / 150 reserved) and the
        # chain closes onto the persisted balances — reconciled with a
        # nonzero implicit opening and no other rows.
        item = _item(on_hand=750, reserved=0)
        _movement(item, 'consumption', -150, -150, 900, 150,
                  reason='order_ready')
        resp = self.client.get(REC_URL)
        statuses = [(r['on_hand_status'], r['reserved_status'])
                    for r in resp.context['results']]
        self.assertEqual(statuses, [('RECONCILED', 'RECONCILED')])
        content = resp.content.decode()
        self.assertNotIn('CHAIN DISCONTINUITY', content)
        self.assertNotIn('BALANCE MISMATCH', content)

    def test_zero_movement_item(self):
        # NO MOVEMENTS: balances displayed with the explanation; not
        # counted as reconciled. Assertions use the view context — the
        # mobile fallback legitimately repeats the status strings in the
        # rendered HTML.
        _item(on_hand=500, reserved=25)
        resp = self._page()
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn('No movement history to reconcile against', content)
        self.assertIn('500', content)
        self.assertIn('25', content)
        statuses = [(r['on_hand_status'], r['reserved_status'])
                    for r in resp.context['results']]
        self.assertEqual(statuses, [('NO MOVEMENTS', 'NO MOVEMENTS')])
        self.assertNotIn('RECONCILED', content)

    def test_inactive_items_included(self):
        # The ledger is historical: inactive items are reconciled too.
        _item(name='TEST Inactive', item_type='ingredient', base_unit='g',
              on_hand=100, is_active=False)
        resp = self._page()
        content = resp.content.decode()
        self.assertIn('TEST Inactive', content)
        self.assertIn('No', content)  # Active column

    def test_on_hand_chain_discontinuity(self):
        # Second movement's before does not continue the first after.
        item = _item(on_hand=120)
        _movement(item, 'purchase', 150, 0, 100, 0)     # 100 -> 250
        _movement(item, 'consumption', -20, -20, 140, 20)  # before 140 != 250
        resp = self._page()
        content = resp.content.decode()
        self.assertIn('CHAIN DISCONTINUITY', content)
        self.assertIn('250', content)   # expected
        self.assertIn('140', content)   # actual

    def test_reserved_chain_discontinuity(self):
        # Reserved chain breaks independently of the on-hand chain. The
        # consumption row is CHECK-valid (res_before 150 - delta 150 = 0)
        # but does not continue the reservation's after-value (300).
        item = _item(on_hand=850, reserved=0)
        _movement(item, 'reservation', 0, 300, 1000, 0)      # res 0 -> 300
        _movement(item, 'consumption', -150, -150, 1000, 150,
                  reason='order_ready')                      # res before 150 != 300
        resp = self._page()
        content = resp.content.decode()
        self.assertIn('CHAIN DISCONTINUITY', content)
        self.assertIn('300', content)

    def test_final_on_hand_mismatch(self):
        # On-hand chain closes at 900 but the persisted balance is 850.
        # The reserved chain (reservation -> consumption) reconciles.
        item = _item(on_hand=850)
        _movement(item, 'purchase', 1000, 0, 0, 0)
        _movement(item, 'reservation', 0, 100, 1000, 0)
        _movement(item, 'consumption', -100, -100, 1000, 100,
                  reason='order_ready')
        resp = self._page()
        content = resp.content.decode()
        self.assertIn('BALANCE MISMATCH', content)
        self.assertIn('900', content)
        self.assertIn('850', content)

    def test_final_reserved_mismatch(self):
        # Reserved chain closes at 50 but persisted reserved is 0.
        item = _item(on_hand=850, reserved=0)
        _movement(item, 'purchase', 1000, 0, 0, 0)
        _movement(item, 'reservation', 0, 150, 1000, 0)
        _movement(item, 'consumption', -100, -100, 1000, 150,
                  reason='order_ready')   # res 150 -> 50
        resp = self._page()
        content = resp.content.decode()
        self.assertIn('BALANCE MISMATCH', content)
        self.assertIn('50', content)

    def test_matching_final_balance_with_intermediate_discontinuity(self):
        # The owner's example: a broken intermediate chain that still ends
        # on the persisted balance must keep BOTH findings — a passing final
        # balance never conceals a discontinuity.
        item = _item(on_hand=120)
        _movement(item, 'adjustment', 150, 0, 100, 0)    # 100 -> 250
        _movement(item, 'consumption', -20, -20, 140, 20)  # 140 -> 120 (before != 250)
        resp = self._page()
        content = resp.content.decode()
        self.assertIn('CHAIN DISCONTINUITY', content)
        # The final balance matches (120 == 120): no mismatch finding, but
        # the discontinuity MUST remain — one passing check never conceals
        # another failing one.
        self.assertNotIn('BALANCE MISMATCH', content)

    def test_multiple_discontinuities_preserved(self):
        # An item with TWO chain discontinuities must report both; the
        # final balance matches the persisted balance, so no mismatch
        # finding exists — but neither discontinuity may be concealed.
        item = _item(on_hand=230)
        _movement(item, 'purchase', 150, 0, 100, 0)        # 100 -> 250
        _movement(item, 'consumption', -20, -20, 140, 20)  # disc 1: 140 != 250 -> 120
        _movement(item, 'purchase', 30, 0, 200, 0)         # disc 2: 200 != 120 -> 230
        resp = self._page()
        content = resp.content.decode()
        findings = [f for r in resp.context['results'] for f in r['all_findings']]
        on_hand_disc = [f for f in findings
                        if f['kind'] == 'CHAIN DISCONTINUITY'
                        and f['ledger'] == 'On-hand']
        self.assertEqual(len(on_hand_disc), 2)
        # Both broken transitions are reported with their expected values.
        self.assertIn('120', content)
        self.assertIn('200', content)
        # Final balance matches (230 == 230): no mismatch finding.
        self.assertNotIn('BALANCE MISMATCH', content)

    def test_consumption_settles_both_chains(self):
        # Consumption decreases on-hand AND reserved (shape rule).
        item = _item(on_hand=850, reserved=0)
        _movement(item, 'purchase', 1000, 0, 0, 0)
        _movement(item, 'reservation', 0, 150, 1000, 0)
        _movement(item, 'consumption', -150, -150, 1000, 150,
                  reason='order_ready')
        resp = self._page()
        statuses = [(r['on_hand_status'], r['reserved_status'])
                    for r in resp.context['results']]
        self.assertEqual(statuses, [('RECONCILED', 'RECONCILED')])
        content = resp.content.decode()
        self.assertNotIn('CHAIN DISCONTINUITY', content)
        self.assertNotIn('BALANCE MISMATCH', content)

    def test_cancelled_prepared_remains_consumption(self):
        # cancelled_prepared consumption is consumption (never wastage) and
        # reconciles normally (A11): both rows settle the placement
        # reservation.
        item = _item(on_hand=800)
        _movement(item, 'purchase', 1000, 0, 0, 0)
        _movement(item, 'reservation', 0, 200, 1000, 0)
        _movement(item, 'consumption', -150, -150, 1000, 200,
                  reason='order_ready')
        _movement(item, 'consumption', -50, -50, 850, 50,
                  reason='cancelled_prepared')
        resp = self._page()
        content = resp.content.decode()
        self.assertIn('RECONCILED', content)
        self.assertNotIn('Wastage', content)
        self.assertNotIn('wastage must decrease', content)

    def test_signed_adjustments_reconcile(self):
        # Positive and negative adjustments are signed on-hand links.
        item = _item(on_hand=950)
        _movement(item, 'purchase', 1000, 0, 0, 0)
        _movement(item, 'adjustment', 100, 0, 1000, 0)     # 1000 -> 1100
        _movement(item, 'adjustment', -150, 0, 1100, 0)    # 1100 -> 950
        resp = self._page()
        content = resp.content.decode()
        self.assertIn('RECONCILED', content)
        self.assertNotIn('CHAIN DISCONTINUITY', content)

    def test_summary_counts(self):
        # Summary cards: counts reflect the seeded scenarios without mixing
        # units into quantities.
        _item(name='TEST OK', on_hand=850)
        it_ok = InventoryItem.objects.get(name='TEST OK')
        _movement(it_ok, 'purchase', 1000, 0, 0, 0)
        _movement(it_ok, 'reservation', 0, 150, 1000, 0)
        _movement(it_ok, 'consumption', -150, -150, 1000, 150)
        broken = _item(name='TEST Broken', on_hand=120)
        _movement(broken, 'purchase', 150, 0, 100, 0)
        _movement(broken, 'consumption', -20, -20, 140, 20)
        _item(name='TEST NoMovements', on_hand=10)
        resp = self._page()
        self.assertEqual(resp.context['summary']['total_items'], 3)
        self.assertEqual(resp.context['summary']['reconciled_items'], 1)
        self.assertEqual(resp.context['summary']['discontinuity_items'], 1)
        self.assertEqual(resp.context['summary']['no_movement_items'], 1)


class ReconciliationInvalidDataTests(TestCase):
    """Rows that would violate the ledger's DB CHECK constraints are
    represented as UNSAVED IngredientMovementLog instances and fed directly
    to _reconcile_item() — the actual production reconciliation logic — so
    the malformed-data detection is exercised without inserting
    CHECK-violating rows into ANY database (SQLite or PostgreSQL).

    _reconcile_item(item, movements) is a pure function of its arguments
    (verified: no database access inside), so unsaved in-memory rows are
    fully supported. Movements must be supplied in the intended
    (created_at, id)-equivalent order; unsaved rows carry their attributes
    exactly as assigned (auto_now_add never runs without a save)."""

    def setUp(self):
        self.admin = _user('rec_invalid_admin')
        self.client = Client()
        self.client.force_login(self.admin)

    def _unsaved(self, item, mtype, on_delta, res_delta, on_before,
                 res_before, on_after=None, res_after=None, reason='',
                 days_ago=0):
        """Build one UNSAVED IngredientMovementLog with explicit chain
        values (on_after/res_after default to before + delta)."""
        if on_after is None:
            on_after = Decimal(str(on_before)) + Decimal(str(on_delta))
        if res_after is None:
            res_after = Decimal(str(res_before)) + Decimal(str(res_delta))
        created = timezone.now() - datetime.timedelta(days=days_ago)
        return IngredientMovementLog(
            inventory_item=item,
            inventory_item_id=item.pk,
            item_name=item.name,
            base_unit=item.base_unit,
            movement_type=mtype,
            on_hand_delta=Decimal(str(on_delta)),
            reserved_delta=Decimal(str(res_delta)),
            on_hand_before=Decimal(str(on_before)),
            on_hand_after=Decimal(str(on_after)),
            reserved_before=Decimal(str(res_before)),
            reserved_after=Decimal(str(res_after)),
            reason_code=reason,
            created_at=created,
        )

    def _reconciled(self, item, movements):
        from apps.reports.views import _reconcile_item
        return _reconcile_item(item, movements)

    def test_unexpected_shape_flagged(self):
        # A purchase that also changes reserved (shape violation).
        item = _item(on_hand=1000, reserved=5)
        m = self._unsaved(item, 'purchase', 1000, 5, 0, 0)
        result = self._reconciled(item, [m])
        shapes = [f for f in result['all_findings']
                  if f['kind'] == 'UNEXPECTED MOVEMENT SHAPE']
        self.assertEqual(len(shapes), 1)
        self.assertEqual(shapes[0]['ledger'], 'Reserved')
        self.assertIn('purchase must not change reserved',
                      shapes[0]['message'])
        # On-hand ledger unaffected (the purchase delta itself is valid).
        self.assertEqual(
            [f['kind'] for f in result['on_hand_findings']],
            [])

    def test_negative_ledger_values_flagged(self):
        # A consumption that drives on-hand and reserved negative (CHECK
        # violations): on_before 50, delta -100 -> after -50.
        item = _item(on_hand=50, reserved=50)
        m = self._unsaved(item, 'consumption', -100, -100, 50, 50,
                          on_after=-50, res_after=-50)
        result = self._reconciled(item, [m])
        invalid = [f for f in result['all_findings']
                   if f['kind'] == 'INVALID LEDGER VALUES']
        on_messages = [f['message'] for f in invalid
                       if f['ledger'] == 'On-hand']
        res_messages = [f['message'] for f in invalid
                        if f['ledger'] == 'Reserved']
        self.assertTrue(any('negative on-hand level' in msg
                            for msg in on_messages))
        self.assertTrue(any('negative reserved level' in msg
                            for msg in res_messages))

    def test_arithmetic_disagreement_flagged(self):
        # after != before + delta (CHECK-violating row).
        item = _item(on_hand=999)
        m = self._unsaved(item, 'purchase', 500, 0, 0, 0, on_after=777)
        result = self._reconciled(item, [m])
        kinds = [f['kind'] for f in result['all_findings']]
        self.assertIn('INVALID LEDGER VALUES', kinds)
        # The arithmetic finding names the disagreeing value.
        invalid = [f for f in result['all_findings']
                   if f['kind'] == 'INVALID LEDGER VALUES']
        self.assertTrue(any('777' in f['message'] for f in invalid))
        # Coexisting final-balance mismatch (computed 777 vs persisted 999).
        self.assertIn('BALANCE MISMATCH', kinds)

    def test_arithmetic_violation_mid_chain(self):
        # Independent-audit fix: an arithmetic violation at position >= 1
        # (row whose `after` disagrees with its own `before + delta`) must
        # be reported even though its `before` continues the chain and a
        # BALANCE MISMATCH coexists.
        item = _item(on_hand=120)
        m1 = _movement(item, 'purchase', 150, 0, 100, 0)  # 100 -> 250 (valid, saved)
        m2 = self._unsaved(item, 'purchase', -20, 0, 250, 0,
                           on_after=999)
        result = self._reconciled(item, [m1, m2])
        kinds = [f['kind'] for f in result['all_findings']]
        self.assertIn('INVALID LEDGER VALUES', kinds)
        invalid = [f for f in result['all_findings']
                   if f['kind'] == 'INVALID LEDGER VALUES']
        self.assertTrue(any('999' in f['message'] for f in invalid))
        # No chain discontinuity: m2's before (250) continues m1 (250) —
        # the defect is purely the arithmetic, which the report must still
        # catch (this is the regression case for the audit fix).
        self.assertNotIn('CHAIN DISCONTINUITY', kinds)
        # Coexisting final-balance mismatch (999 vs persisted 120).
        self.assertIn('BALANCE MISMATCH', kinds)
        # The on-hand status names all three finding kinds: the tampered
        # row is arithmetically invalid AND shape-invalid (a purchase with
        # a negative delta), plus the coexisting balance mismatch.
        self.assertEqual(
            result['on_hand_status'],
            'INVALID LEDGER VALUES & UNEXPECTED MOVEMENT SHAPE & '
            'BALANCE MISMATCH')

    def test_adjustment_with_reserved_delta_flagged(self):
        # An adjustment that also changes reserved violates its shape rule
        # (CHECK-violating row) and must be flagged on the Reserved ledger.
        item = _item(on_hand=100, reserved=0)
        m = self._unsaved(item, 'adjustment', 100, 5, 0, 0)
        result = self._reconciled(item, [m])
        shapes = [f for f in result['all_findings']
                  if f['kind'] == 'UNEXPECTED MOVEMENT SHAPE']
        self.assertEqual(len(shapes), 1)
        self.assertEqual(shapes[0]['ledger'], 'Reserved')
        self.assertIn('adjustment must not change reserved',
                      shapes[0]['message'])


class ReconciliationSnapshotTests(ReconciliationTestBase):

    def test_renamed_item_grouped_once_with_snapshot_rows(self):
        # Historical movement rows keep their item_name snapshot; the report
        # groups by inventory_item FK so a rename never splits an item.
        item = _item(name='TEST Old Name', on_hand=850)
        _movement(item, 'purchase', 1000, 0, 0, 0)
        _movement(item, 'consumption', -150, -150, 1000, 150)
        item.name = 'TEST Renamed Item'
        item.save(update_fields=['name'])
        resp = self._page()
        content = resp.content.decode()
        self.assertIn('TEST Renamed Item', content)
        self.assertNotIn('TEST Old Name', content)
        self.assertEqual(content.count('RECONCILED'), 2)


class ReconciliationExcelTests(ReconciliationTestBase):

    def test_workbook_structure(self):
        # Exactly two worksheets: Summary and Item Reconciliation.
        item = _item(on_hand=850)
        _movement(item, 'purchase', 1000, 0, 0, 0)
        _movement(item, 'consumption', -150, -150, 1000, 150)
        resp, content = _download(self.client)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('spreadsheetml', resp.get('Content-Type', ''))
        wb = _load(content)
        self.assertEqual(wb.sheetnames, ['Summary', 'Item Reconciliation'])

    def test_item_sheet_rows_and_headers(self):
        # One row per item (including zero-movement and inactive); header
        # row present; no silent truncation.
        _item(name='TEST With Chain', on_hand=850)
        _item(name='TEST NoChain', on_hand=50, is_active=False)
        resp, content = _download(self.client)
        wb = _load(content)
        ws = wb['Item Reconciliation']
        headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        self.assertEqual(headers[0], 'Item ID')
        self.assertEqual(headers[1], 'Item Name')
        self.assertEqual(headers[-1], 'Findings')
        data_rows = sum(1 for row in ws.iter_rows(min_row=2)
                        if any(c.value is not None for c in row))
        self.assertEqual(data_rows, 2)

    def test_decimal_precision_and_numeric_cells(self):
        # Quantities reopen as numeric cells with exact 3-decimal values.
        # Deltas are chosen binary-exact (.5/.25) because SQLite stores
        # NUMERIC columns as REAL and the arithmetic CHECK compares floats
        # — 876.544-style values would fail mv_on_hand_arithmetic on
        # storage, not in the report.
        item = _item(name='TEST Precise', on_hand=123.750)
        _movement(item, 'purchase', 1000, 0, 0, 0)
        _movement(item, 'reservation', 0, 876.250, 1000, 0)
        _movement(item, 'consumption', -876.250, -876.250, 1000, 876.250)
        resp, content = _download(self.client)
        wb = _load(content)
        ws = wb['Item Reconciliation']
        persisted = ws.cell(row=2, column=8)   # Persisted On-Hand
        self.assertEqual(persisted.data_type, 'n')
        self.assertEqual(persisted.value, 123.75)
        opening = ws.cell(row=2, column=6)     # Opening On-Hand
        self.assertEqual(opening.data_type, 'n')
        self.assertEqual(opening.value, 0)     # implicit opening: purchase's before
        computed = ws.cell(row=2, column=7)    # Computed On-Hand
        self.assertEqual(computed.data_type, 'n')
        self.assertEqual(computed.value, 123.75)
        diff = ws.cell(row=2, column=9)        # On-Hand Difference
        self.assertEqual(diff.value, 0)
        opening_res = ws.cell(row=2, column=11)   # Opening Reserved
        self.assertEqual(opening_res.value, 0)
        persisted_res = ws.cell(row=2, column=13)  # Persisted Reserved
        self.assertEqual(persisted_res.value, 0)

    def test_formula_injection_protection(self):
        # Item names are text cells: formula-leading characters can never
        # execute.
        _item(name='=HYPERLINK("http://x","y")')
        _item(name='+plus item')
        resp, content = _download(self.client)
        wb = _load(content)
        ws = wb['Item Reconciliation']
        for row in ws.iter_rows(min_row=2):
            name_cell = row[1]
            self.assertEqual(name_cell.data_type, 's')
            self.assertTrue(str(name_cell.value).startswith(('=', '+')))

    def test_empty_report_export(self):
        # No items: both sheets exist, item sheet has headers only.
        resp, content = _download(self.client)
        self.assertEqual(resp.status_code, 200)
        wb = _load(content)
        self.assertEqual(wb.sheetnames, ['Summary', 'Item Reconciliation'])
        ws = wb['Item Reconciliation']
        data_rows = sum(1 for row in ws.iter_rows(min_row=2)
                        if any(c.value is not None for c in row))
        self.assertEqual(data_rows, 0)

    def test_summary_sheet_counts(self):
        # Summary sheet carries the metric counts (numeric), never mixed
        # unit quantities.
        _item(name='TEST OK', on_hand=850)
        it_ok = InventoryItem.objects.get(name='TEST OK')
        _movement(it_ok, 'purchase', 1000, 0, 0, 0)
        _movement(it_ok, 'reservation', 0, 150, 1000, 0)
        _movement(it_ok, 'consumption', -150, -150, 1000, 150)
        _item(name='TEST NoChain', on_hand=10)
        resp, content = _download(self.client)
        wb = _load(content)
        ws = wb['Summary']
        values = {}
        for row in ws.iter_rows(min_row=6):
            label = row[0].value
            if label:
                values[label] = row[1].value
        self.assertEqual(values['Total inventory items'], 2)
        self.assertEqual(values['Reconciled items'], 1)
        self.assertEqual(values['Items without movement history'], 1)
        self.assertEqual(values['Total findings'], 0)


class ReconciliationQueryBudgetTests(ReconciliationTestBase):

    def test_query_budget(self):
        # The view issues exactly two data queries (items + full ledger);
        # the query count never scales with data volume. Canonical
        # warm-up + double-the-data pattern (see
        # tests_query_performance.py).
        def _seed(count, start):
            for i in range(start, start + count):
                item = _item(f'TEST Budget Item {i}', on_hand=1000)
                _movement(item, 'purchase', 1000, 0, 0, 0)
                _movement(item, 'consumption', -100, -100, 1000, 100)

        _seed(40, 0)
        with CaptureQueriesContext(connection) as warm_ctx:
            resp = self._page()
        self.assertEqual(resp.status_code, 200)
        count_40 = len(warm_ctx.captured_queries)

        _seed(40, 40)
        with CaptureQueriesContext(connection) as ctx:
            resp = self._page()
        self.assertEqual(resp.status_code, 200)
        count_80 = len(ctx.captured_queries)

        self.assertLessEqual(count_80, count_40,
                             'query count increased with data volume')
        self.assertLessEqual(count_80, 8,
                             f'reconciliation executed {count_80} queries')

        # Export adds no per-row queries beyond the same two data queries.
        with CaptureQueriesContext(connection) as export_ctx:
            resp = self.client.get(EXPORT_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertLessEqual(len(export_ctx.captured_queries), 8)
