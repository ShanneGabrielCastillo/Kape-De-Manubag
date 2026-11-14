"""
Phase 7.4 Sales Reports hardening tests (decision record §10, H1–H6).

Covers:
  H1  Excel formula-injection fix — user-controlled text cells are forced to
      data_type='s' via _safe_text_cell; numeric Total cells stay numeric.
      Regression proof saves + reopens generated workbooks.
  H2  Maximum date range (92 days) with visible clamp notice on the page and
      the export inheriting the clamped range.
  H3  Export row cap (5,000) — HTTP 400 with a clear message, never truncation.
  H4  Summary sub-label "Paid & completed orders".
  H5  Packaging-fee footnotes on Top Products and Sales by Category.
  H6  Export header "Items" → "Line items".
  SH-12  Existing page content intact after the hardening edits.

Workbook assertions follow apps/reports/tests_export.py conventions
(io.BytesIO + openpyxl.load_workbook round-trip).
"""

import datetime
import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.orders.models import Order

User = get_user_model()

TODAY = timezone.localdate()


# ── Helpers (mirror tests_export.py conventions) ─────────────────────────────

def _user(username, role='admin'):
    u = User.objects.create_user(username=username, password='pass123')
    u.role = role
    u.save()
    return u


def _order(date=None, total=Decimal('100.00'), payment_method='cash',
           status='completed', is_paid=True, customer='Test',
           order_type='dine_in'):
    o = Order.objects.create(
        customer_name=customer,
        status=status, is_paid=is_paid,
        payment_method=payment_method,
        order_type=order_type,
        total=total, subtotal=total,
    )
    if date is not None and date != TODAY:
        dt = timezone.make_aware(
            datetime.datetime.combine(date, datetime.time(10, 0))
        )
        Order.objects.filter(pk=o.pk).update(created_at=dt)
        o.refresh_from_db()
    return o


def _export_url(start=None, end=None):
    s = start if start is not None else TODAY
    e = end if end is not None else TODAY
    return reverse('reports:export_excel') + f'?start={s}&end={e}'


def _load_workbook_bytes(content):
    import openpyxl
    return openpyxl.load_workbook(io.BytesIO(content))


class SalesHardeningTestBase(TestCase):
    """Shared admin login + export/download helpers."""

    def setUp(self):
        self.admin = _user('sh_admin')
        self.client = Client()
        self.client.force_login(self.admin)

    def _download(self, start=None, end=None):
        """Return (status, content, content_type) of an export request."""
        resp = self.client.get(_export_url(start, end))
        content = b''.join(
            resp.streaming_content
            if hasattr(resp, 'streaming_content')
            else [resp.content]
        )
        return resp.status_code, content, resp.get('Content-Type', '')

    def _wb(self, start=None, end=None):
        status, content, _ct = self._download(start, end)
        self.assertEqual(status, 200)
        return _load_workbook_bytes(content)


# ── H1: formula injection stays text (SH-1..SH-3) ────────────────────────────

class FormulaInjectionTests(SalesHardeningTestBase):

    def test_formula_customer_name_stays_text(self):
        # SH-1
        _order(customer='=HYPERLINK("http://x","y")')
        wb = self._wb()
        ws = wb.active
        # Data row 2, Customer column 3.
        cell = ws.cell(row=2, column=3)
        self.assertEqual(cell.data_type, 's')
        self.assertEqual(cell.value, '=HYPERLINK("http://x","y")')

    def test_formula_prefix_variants_stay_text(self):
        # SH-2 — every formula-leading character stays a text cell.
        for prefix in ('+', '-', '@', '='):
            customer = f'{prefix}evil'
            _order(customer=customer)
        wb = self._wb()
        ws = wb.active
        customer_values = [ws.cell(row=r, column=3) for r in range(2, 6)]
        for cell in customer_values:
            self.assertEqual(cell.data_type, 's', f'{cell.value!r} not text')
            self.assertTrue(str(cell.value).startswith(('+', '-', '@', '=')))
        # Every display-text column of every data row is a text cell
        # (Order #, Date, Customer, Type, Line items, Payment, Cashier).
        for text_col in (1, 2, 3, 4, 5, 7, 8):
            for r in range(2, 6):
                self.assertEqual(
                    ws.cell(row=r, column=text_col).data_type, 's',
                    f'text column {text_col} row {r} is not data_type "s"',
                )

    def test_numeric_cells_stay_numeric(self):
        # SH-3 — legitimate numeric cells (Total) stay numeric, values exact.
        _order(total=Decimal('123.10'))
        _order(total=Decimal('276.90'))
        wb = self._wb()
        ws = wb.active
        total_cells = [ws.cell(row=r, column=6) for r in range(2, 4)]
        for cell in total_cells:
            self.assertEqual(cell.data_type, 'n')
        total_sum = sum(cell.value for cell in total_cells)
        self.assertEqual(total_sum, Decimal('400.00'))


# ── H6 / H4 / H5: labels (SH-4..SH-6) ────────────────────────────────────────

class LabelTests(SalesHardeningTestBase):

    def test_header_renamed_line_items(self):
        # SH-4 — header renamed, column count and order unchanged.
        _order()
        wb = self._wb()
        ws = wb.active
        headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        self.assertEqual(headers, ['Order #', 'Date', 'Customer', 'Type',
                                   'Line items', 'Total', 'Payment', 'Cashier'])

    def test_summary_label_paid_completed(self):
        # SH-5 — H4 label fix on the Total Orders summary card.
        resp = self.client.get(reverse('reports:index'))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn('Paid &amp; completed orders', content)
        self.assertNotIn('>Paid orders<', content)

    def test_packaging_fee_footnotes(self):
        # SH-6 — H5 footnotes on Top Products AND Sales by Category.
        resp = self.client.get(reverse('reports:index'))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertEqual(content.count('Excludes packaging fees.'), 2)


# ── H2: maximum date range (SH-7..SH-9) ──────────────────────────────────────

class RangeClampTests(SalesHardeningTestBase):

    def test_range_clamped_over_limit(self):
        # SH-7 — 200-day request is clamped to the most recent 92 days.
        start = TODAY - datetime.timedelta(days=200)
        resp = self.client.get(
            reverse('reports:index') + f'?start={start}&end={TODAY}'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context['was_clamped'])
        expected_start = TODAY - datetime.timedelta(days=91)
        self.assertEqual(resp.context['start_date'], expected_start.isoformat())
        self.assertIn('Date range limited to 92 days', resp.content.decode())

    def test_range_within_limit_not_clamped(self):
        # SH-8 — a 10-day window passes through untouched, no notice.
        start = TODAY - datetime.timedelta(days=10)
        resp = self.client.get(
            reverse('reports:index') + f'?start={start}&end={TODAY}'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context['was_clamped'])
        self.assertEqual(resp.context['start_date'], start.isoformat())
        self.assertNotIn('Date range limited to', resp.content.decode())

    def test_export_uses_clamped_range(self):
        # SH-9 — an oversized export request operates on the clamped window:
        # the order far outside the clamp is absent, the in-window one present.
        _order(date=TODAY - datetime.timedelta(days=150),
               customer='OldOutsideClamp')
        _order(customer='RecentInsideClamp')
        status, content, ct = self._download(
            start=TODAY - datetime.timedelta(days=200), end=TODAY,
        )
        self.assertEqual(status, 200)
        wb = _load_workbook_bytes(content)
        ws = wb.active
        customers = [ws.cell(row=r, column=3).value
                     for r in range(2, ws.max_row + 1)]
        self.assertIn('RecentInsideClamp', customers)
        self.assertNotIn('OldOutsideClamp', customers)


# ── H3: export row cap (SH-10..SH-11) ────────────────────────────────────────

class ExportRowCapTests(SalesHardeningTestBase):

    def _bulk_orders(self, count):
        # bulk_create for speed; all land today (inside the default window).
        # order_number is generated in Order.save(), which bulk_create
        # bypasses — assign unique values explicitly (queue_number is
        # nullable and unconstrained).
        Order.objects.bulk_create([
            Order(
                order_number=f'SHBULK{i:05d}',
                customer_name=f'Bulk {i}',
                status='completed', is_paid=True,
                payment_method='cash', order_type='dine_in',
                total=Decimal('10.00'), subtotal=Decimal('10.00'),
            )
            for i in range(count)
        ])

    def test_export_row_cap_rejects(self):
        # SH-10 — 5,001 orders → HTTP 400, clear message, no workbook bytes.
        self._bulk_orders(5001)
        status, content, ct = self._download()
        self.assertEqual(status, 400)
        self.assertIn('Export too large', content.decode())
        self.assertIn('5,000', content.decode())
        self.assertNotIn('spreadsheetml', ct)
        # Not a valid xlsx (zip) file — the response carries HTML, no workbook.
        self.assertFalse(content.startswith(b'PK'))

    def test_export_row_cap_boundary_ok(self):
        # SH-11 — exactly at the cap (5,000) the export succeeds normally.
        self._bulk_orders(5000)
        status, content, ct = self._download()
        self.assertEqual(status, 200)
        self.assertIn('spreadsheetml', ct)
        wb = _load_workbook_bytes(content)
        ws = wb.active
        data_rows = sum(
            1 for row in ws.iter_rows(min_row=2)
            if any(cell.value is not None for cell in row)
        )
        self.assertEqual(data_rows, 5000)


# ── SH-12: existing page content intact ──────────────────────────────────────

class IndexContentIntactTests(SalesHardeningTestBase):

    def test_index_content_intact(self):
        # SH-12 — daily chart, both summary cards, and the report tab strip
        # coexist after the hardening edits.
        resp = self.client.get(reverse('reports:index'))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn('id="daily-chart"', content)
        self.assertIn('Total Revenue', content)
        self.assertIn('Total Orders', content)
        self.assertIn('Sales Reports', content)
        self.assertIn('Ingredient Usage', content)
        self.assertIn('Cancellation Dispositions', content)
        # Existing sales aggregation context is still served.
        self.assertIn('total_revenue', resp.context)
        self.assertIn('daily_sales', resp.context)
