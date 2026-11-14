"""
Phase 7.5 — Inventory Ledger Reconciliation tests (admin-only, all-time,
per-item ledger-internal consistency report plus its Excel export).

Cases follow the approved Phase 7.5 blueprint test matrix. Fixtures create
InventoryItems with their FINAL balances and build movement chains via
IngredientMovementLog.record() (shape-CHECK-safe); deliberately broken
chains/mismatches are created the same way, because record() accepts
caller-supplied before/after values and only the DB CHECKs constrain them.

Rows that violate DB CHECK constraints (negative levels, wrong shapes) are
inserted with PRAGMA ignore_check_constraints=ON via raw SQL on the ISOLATED
test database, restored immediately in a finally block. The local
development database is never touched.
"""

import datetime
import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.inventory.models import IngredientMovementLog, InventoryItem

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


def _raw_movement(item, mtype, on_delta, res_delta, on_before, res_before,
                  on_after=None, res_after=None):
    """Insert a movement row that VIOLATES the DB CHECK constraints (used to
    prove the report flags invalid ledger data instead of crashing). The
    check-constraint bypass is applied to the ISOLATED test database only
    and restored immediately. NOTE: the containing test class must be a
    TransactionTestCase — the pragma is a no-op inside a TestCase atomic
    transaction. order_number is supplied because the column is NOT NULL
    without a database-level default."""
    on_after = Decimal(str(on_after if on_after is not None
                            else Decimal(str(on_before))
                            + Decimal(str(on_delta))))
    res_after = Decimal(str(res_after if res_after is not None
                            else Decimal(str(res_before))
                            + Decimal(str(res_delta))))
    now = timezone.now()
    try:
        with connection.cursor() as cur:
            cur.execute("PRAGMA ignore_check_constraints=ON")
            cur.execute(
                "INSERT INTO inventory_ingredientmovementlog "
                "(inventory_item_id, item_name, base_unit, movement_type, "
                " on_hand_delta, reserved_delta, on_hand_before, "
                " on_hand_after, reserved_before, reserved_after, "
                " reason_code, order_number, note, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s)",
                [item.pk, item.name, item.base_unit, mtype,
                 str(Decimal(str(on_delta))), str(Decimal(str(res_delta))),
                 str(Decimal(str(on_before))), str(on_after),
                 str(Decimal(str(res_before))), str(res_after),
                 '', '', 'Phase 7.5 invalid-row test fixture', str(now)]
            )
    finally:
        with connection.cursor() as cur:
            cur.execute("PRAGMA ignore_check_constraints=OFF")


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


class ReconciliationInvalidDataTests(TransactionTestCase):
    """Rows that violate DB CHECK constraints, inserted raw on the isolated
    test database, must be flagged as findings without crashing.

    TransactionTestCase is REQUIRED here (standalone, not derived from the
    TestCase-based base): PRAGMA ignore_check_constraints is a no-op inside
    the transaction that a plain TestCase wraps every test in, but it works
    in TransactionTestCase's autocommit mode."""

    def setUp(self):
        self.admin = _user('rec_invalid_admin')
        self.client = Client()
        self.client.force_login(self.admin)

    def _page(self):
        resp = self.client.get(REC_URL)
        self.assertEqual(resp.status_code, 200)
        return resp

    def test_unexpected_shape_flagged(self):
        # A purchase that also changes reserved (shape violation).
        item = _item(on_hand=1000, reserved=5)
        _raw_movement(item, 'purchase', 1000, 5, 0, 0)
        resp = self._page()
        content = resp.content.decode()
        self.assertIn('UNEXPECTED MOVEMENT SHAPE', content)
        self.assertIn('purchase must not change reserved', content)

    def test_negative_ledger_values_flagged(self):
        # A consumption that drives on-hand and reserved negative (CHECK
        # violations): on_before 50, delta -100 -> after -50.
        item = _item(on_hand=50, reserved=50)
        _raw_movement(item, 'consumption', -100, -100, 50, 50,
                      on_after=-50, res_after=-50)
        resp = self._page()
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn('INVALID LEDGER VALUES', content)
        self.assertIn('negative on-hand level', content)
        self.assertIn('negative reserved level', content)

    def test_arithmetic_disagreement_flagged(self):
        # after != before + delta (CHECK-violating raw row).
        item = _item(on_hand=999)
        _raw_movement(item, 'purchase', 500, 0, 0, 0, on_after=777)
        resp = self._page()
        content = resp.content.decode()
        self.assertIn('INVALID LEDGER VALUES', content)
        self.assertIn('777', content)

    def test_arithmetic_violation_mid_chain(self):
        # Independent-audit fix: an arithmetic violation at position >= 1
        # (raw-SQL tampered row whose `after` disagrees with its own
        # `before + delta`) must be reported even though its `before`
        # continues the chain and a BALANCE MISMATCH coexists.
        item = _item(on_hand=120)
        _movement(item, 'purchase', 150, 0, 100, 0)     # 100 -> 250 (valid)
        # Tampered row: before 250 continues the chain, but after 999
        # != 250 - 20 = 230.
        _raw_movement(item, 'purchase', -20, 0, 250, 0, on_after=999)
        resp = self._page()
        content = resp.content.decode()
        self.assertIn('INVALID LEDGER VALUES', content)
        self.assertIn('999', content)
        # Findings coexist: the final-balance mismatch (999 vs persisted
        # 120) is preserved alongside the arithmetic finding.
        self.assertIn('BALANCE MISMATCH', content)

    def test_adjustment_with_reserved_delta_flagged(self):
        # An adjustment that also changes reserved violates its shape rule
        # (CHECK-violating raw row) and must be flagged on the Reserved
        # ledger.
        item = _item(on_hand=100, reserved=0)
        _raw_movement(item, 'adjustment', 100, 5, 0, 0)
        resp = self._page()
        content = resp.content.decode()
        self.assertIn('UNEXPECTED MOVEMENT SHAPE', content)
        self.assertIn('adjustment must not change reserved', content)


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
