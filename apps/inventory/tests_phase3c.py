"""
tests_phase3c.py
================
Phase 3-3 UI tests for purchase management views.

Covers:
  - Purchase creation form: auth, valid submission, validation.
  - Purchase history list: auth, content, pagination, empty state.
  - Purchase detail: auth, content, historical accuracy.
  - Regression: existing Phase 3-1, 3-2, and Phase 2B tests unaffected.
"""

import datetime
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import CustomUser
from apps.inventory.models import (
    IngredientMovementLog,
    InventoryItem,
    StockPurchase,
    StockPurchaseItem,
    UnitConversion,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

_PC_PWD = 'test-33-pass'


def _user(username, role='cashier'):
    return CustomUser.objects.create_user(
        username=username, password=_PC_PWD, role=role,
    )


def _item(name='Coffee', base_unit='g', item_type='ingredient',
          on_hand=Decimal('0.000'), is_active=True):
    return InventoryItem.objects.create(
        name=name, item_type=item_type, base_unit=base_unit,
        on_hand_quantity=on_hand, is_active=is_active,
    )


def _uc(item, purchase_unit='kg', factor=Decimal('1000.0000'), is_active=True):
    return UnitConversion.objects.create(
        inventory_item=item,
        purchase_unit=purchase_unit,
        conversion_factor=factor,
        is_active=is_active,
    )


def _purchase(idempotency_key='test-key-001'):
    return StockPurchase.objects.create(
        purchase_date=datetime.date(2026, 11, 1),
        idempotency_key=idempotency_key,
    )


LIST_URL   = reverse('inventory:purchase_list')
CREATE_URL = reverse('inventory:purchase_create')


def _detail_url(pk):
    return reverse('inventory:purchase_detail', kwargs={'pk': pk})


# ═════════════════════════════════════════════════════════════════════════════
# 1. Purchase list — authorization
# ═════════════════════════════════════════════════════════════════════════════

class PurchaseListAuthTests(TestCase):
    def test_admin_can_access(self):
        self.client.force_login(_user('pl_admin', 'admin'))
        self.assertEqual(self.client.get(LIST_URL).status_code, 200)

    def test_cashier_can_access(self):
        self.client.force_login(_user('pl_cashier', 'cashier'))
        self.assertEqual(self.client.get(LIST_URL).status_code, 200)

    def test_kitchen_denied(self):
        self.client.force_login(_user('pl_kitchen', 'kitchen_staff'))
        self.assertEqual(self.client.get(LIST_URL).status_code, 302)

    def test_customer_denied(self):
        self.client.force_login(_user('pl_customer', 'customer'))
        self.assertEqual(self.client.get(LIST_URL).status_code, 302)

    def test_unauthenticated_redirected_to_login(self):
        response = self.client.get(LIST_URL)
        self.assertIn('/login', response['Location'])


# ═════════════════════════════════════════════════════════════════════════════
# 2. Purchase list — content
# ═════════════════════════════════════════════════════════════════════════════

class PurchaseListContentTests(TestCase):
    def setUp(self):
        self.user = _user('plc_admin', 'admin')
        self.client.force_login(self.user)

    def test_empty_state_shown(self):
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No purchases yet')

    def test_purchase_appears_in_list(self):
        _purchase('list-test-001')
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '#1')

    def test_pagination_present_when_over_20(self):
        for i in range(25):
            _purchase(f'list-page-{i:03d}')
        response = self.client.get(LIST_URL)
        self.assertContains(response, 'page-link')

    def test_second_page_accessible(self):
        for i in range(25):
            _purchase(f'list-page2-{i:03d}')
        response = self.client.get(LIST_URL + '?page=2')
        self.assertEqual(response.status_code, 200)


# ═════════════════════════════════════════════════════════════════════════════
# 3. Purchase detail — authorization and content
# ═════════════════════════════════════════════════════════════════════════════

class PurchaseDetailAuthTests(TestCase):
    def setUp(self):
        self.purchase = _purchase('detail-auth-001')

    def test_admin_can_access(self):
        self.client.force_login(_user('pd_admin', 'admin'))
        self.assertEqual(self.client.get(_detail_url(self.purchase.pk)).status_code, 200)

    def test_cashier_can_access(self):
        self.client.force_login(_user('pd_cashier', 'cashier'))
        self.assertEqual(self.client.get(_detail_url(self.purchase.pk)).status_code, 200)

    def test_kitchen_denied(self):
        self.client.force_login(_user('pd_kitchen', 'kitchen_staff'))
        self.assertEqual(self.client.get(_detail_url(self.purchase.pk)).status_code, 302)

    def test_404_for_nonexistent_purchase(self):
        self.client.force_login(_user('pd_404', 'admin'))
        self.assertEqual(self.client.get(_detail_url(99999)).status_code, 404)


class PurchaseDetailContentTests(TestCase):
    def setUp(self):
        self.admin    = _user('pdc_admin', 'admin')
        self.client.force_login(self.admin)
        self.item     = _item('PD Coffee', 'g')
        self.uc       = _uc(self.item, 'kg', Decimal('1000.0000'))
        self.purchase = _purchase('detail-content-001')
        StockPurchaseItem.objects.create(
            purchase=self.purchase,
            inventory_item=self.item,
            purchase_unit='kg',
            purchase_quantity=Decimal('5.000'),
            conversion_factor=Decimal('1000.0000'),
            base_quantity=Decimal('5000.000'),
        )

    def test_purchase_date_shown(self):
        response = self.client.get(_detail_url(self.purchase.pk))
        self.assertContains(response, '2026')

    def test_item_name_shown(self):
        response = self.client.get(_detail_url(self.purchase.pk))
        self.assertContains(response, 'PD Coffee')

    def test_purchase_unit_shown(self):
        response = self.client.get(_detail_url(self.purchase.pk))
        self.assertContains(response, 'kg')

    def test_base_quantity_shown(self):
        response = self.client.get(_detail_url(self.purchase.pk))
        self.assertContains(response, '5000')

    def test_historical_snapshot_unchanged_after_conversion_edit(self):
        """Changing UnitConversion later must not alter historical snapshots."""
        self.uc.conversion_factor = Decimal('500.0000')
        self.uc.save()
        response = self.client.get(_detail_url(self.purchase.pk))
        # The stored snapshot must still show the original 1000.0000 factor.
        self.assertContains(response, '1000')
        # And the base_quantity must still be 5000 (not 2500).
        self.assertContains(response, '5000')


# ═════════════════════════════════════════════════════════════════════════════
# 4. Purchase create — authorization
# ═════════════════════════════════════════════════════════════════════════════

class PurchaseCreateAuthTests(TestCase):
    def test_admin_can_access_form(self):
        self.client.force_login(_user('pc_admin', 'admin'))
        self.assertEqual(self.client.get(CREATE_URL).status_code, 200)

    def test_cashier_can_access_form(self):
        self.client.force_login(_user('pc_cashier', 'cashier'))
        self.assertEqual(self.client.get(CREATE_URL).status_code, 200)

    def test_kitchen_denied(self):
        self.client.force_login(_user('pc_kitchen', 'kitchen_staff'))
        self.assertEqual(self.client.get(CREATE_URL).status_code, 302)

    def test_customer_denied(self):
        self.client.force_login(_user('pc_customer', 'customer'))
        self.assertEqual(self.client.get(CREATE_URL).status_code, 302)

    def test_unauthenticated_redirected(self):
        response = self.client.get(CREATE_URL)
        self.assertIn('/login', response['Location'])


# ═════════════════════════════════════════════════════════════════════════════
# 5. Purchase create — POST behavior
# ═════════════════════════════════════════════════════════════════════════════

class PurchaseCreatePostTests(TestCase):
    def setUp(self):
        self.admin = _user('pcp_admin', 'admin')
        self.client.force_login(self.admin)
        self.item = _item('Post Coffee', 'g')
        self.uc   = _uc(self.item, 'kg', Decimal('1000.0000'))

    def _post(self, key=None, date='2026-11-01', qty='5.000',
              item_pk=None, uc_pk=None, **extra):
        import uuid
        item_pk = item_pk or self.item.pk
        uc_pk   = uc_pk   or self.uc.pk
        data = {
            'purchase_date':       date,
            'notes':               '',
            'idempotency_key':     key or str(uuid.uuid4()),
            'lines[0][inventory_item_id]': str(item_pk),
            'lines[0][purchase_unit_id]':  str(uc_pk),
            'lines[0][purchase_quantity]': qty,
            'lines[0][note]':              '',
        }
        data.update(extra)
        return self.client.post(CREATE_URL, data)

    def test_valid_single_line_purchase_redirects(self):
        response = self._post()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(StockPurchase.objects.count(), 1)

    def test_stock_increases_after_purchase(self):
        self._post(qty='5.000')
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('5000.000'))

    def test_exactly_one_movement_log_created(self):
        self._post()
        self.assertEqual(IngredientMovementLog.objects.count(), 1)

    def test_movement_log_type_is_purchase(self):
        self._post()
        log = IngredientMovementLog.objects.get()
        self.assertEqual(log.movement_type, 'purchase')

    def test_valid_multi_line_purchase(self):
        import uuid
        item2 = _item('Post Milk', 'ml')
        uc2   = _uc(item2, 'L', Decimal('1000.0000'))
        data = {
            'purchase_date':   '2026-11-01',
            'notes':           '',
            'idempotency_key': str(uuid.uuid4()),
            'lines[0][inventory_item_id]': str(self.item.pk),
            'lines[0][purchase_unit_id]':  str(self.uc.pk),
            'lines[0][purchase_quantity]': '5.000',
            'lines[0][note]':              '',
            'lines[1][inventory_item_id]': str(item2.pk),
            'lines[1][purchase_unit_id]':  str(uc2.pk),
            'lines[1][purchase_quantity]': '2.000',
            'lines[1][note]':              '',
        }
        response = self.client.post(CREATE_URL, data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(StockPurchaseItem.objects.count(), 2)

    def test_no_lines_rejected(self):
        response = self.client.post(CREATE_URL, {
            'purchase_date':   '2026-11-01',
            'notes':           '',
            'idempotency_key': 'noline-001',
        })
        # Redisplays the form with an error
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StockPurchase.objects.count(), 0)

    def test_zero_quantity_rejected(self):
        response = self._post(qty='0.000', key='zero-001')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StockPurchase.objects.count(), 0)

    def test_negative_quantity_rejected(self):
        response = self._post(qty='-1.000', key='neg-001')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StockPurchase.objects.count(), 0)

    def test_invalid_item_id_rejected(self):
        response = self._post(item_pk=99999, key='invitem-001')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StockPurchase.objects.count(), 0)

    def test_inactive_item_rejected(self):
        inactive = _item('Inactive Item', 'g', is_active=False)
        uc_i = _uc(inactive, 'kg', Decimal('1000.0000'))
        response = self._post(item_pk=inactive.pk, uc_pk=uc_i.pk, key='inactive-001')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StockPurchase.objects.count(), 0)
        inactive.refresh_from_db()
        self.assertEqual(inactive.on_hand_quantity, Decimal('0.000'))

    def test_inactive_unit_conversion_rejected(self):
        inactive_uc = _uc(self.item, 'box', Decimal('12.0000'), is_active=False)
        response = self._post(uc_pk=inactive_uc.pk, key='inactiveuc-001')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StockPurchase.objects.count(), 0)

    def test_conversion_factor_cannot_be_manipulated_via_post(self):
        """The view resolves conversion_factor from the DB. An injected factor
        field in POST must not affect the recorded purchase."""
        import uuid
        data = {
            'purchase_date':   '2026-11-01',
            'notes':           '',
            'idempotency_key': str(uuid.uuid4()),
            'lines[0][inventory_item_id]': str(self.item.pk),
            'lines[0][purchase_unit_id]':  str(self.uc.pk),
            'lines[0][purchase_quantity]': '1.000',
            'lines[0][note]':              '',
            # Attacker tries to inject a larger factor to inflate stock.
            'lines[0][conversion_factor]': '999999.0000',
        }
        self.client.post(CREATE_URL, data)
        spi = StockPurchaseItem.objects.filter(inventory_item=self.item).first()
        if spi:
            # Conversion factor must match the DB value, not the injected one.
            self.assertEqual(spi.conversion_factor, self.uc.conversion_factor)
            self.assertNotEqual(spi.conversion_factor, Decimal('999999.0000'))

    def test_duplicate_key_does_not_create_second_purchase(self):
        import uuid
        key = str(uuid.uuid4())
        self._post(key=key)
        self._post(key=key)
        self.assertEqual(StockPurchase.objects.count(), 1)
        self.assertEqual(StockPurchaseItem.objects.count(), 1)
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('5000.000'))

    def test_fractional_pcs_rejected(self):
        import uuid
        pcs_item = _item('Straw', 'pcs', 'packaging')
        # 2.5 box × 3.1 pcs/box = 7.75 pcs — fractional
        pcs_uc = UnitConversion.objects.create(
            inventory_item=pcs_item,
            purchase_unit='box',
            conversion_factor=Decimal('3.1000'),
        )
        data = {
            'purchase_date':   '2026-11-01',
            'notes':           '',
            'idempotency_key': str(uuid.uuid4()),
            'lines[0][inventory_item_id]': str(pcs_item.pk),
            'lines[0][purchase_unit_id]':  str(pcs_uc.pk),
            'lines[0][purchase_quantity]': '2.500',
            'lines[0][note]':              '',
        }
        response = self.client.post(CREATE_URL, data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StockPurchase.objects.count(), 0)

    def test_pcs_whole_number_accepted(self):
        import uuid
        pcs_item = _item('Cup', 'pcs', 'packaging')
        pcs_uc = UnitConversion.objects.create(
            inventory_item=pcs_item,
            purchase_unit='box',
            conversion_factor=Decimal('12.0000'),
        )
        data = {
            'purchase_date':   '2026-11-01',
            'notes':           '',
            'idempotency_key': str(uuid.uuid4()),
            'lines[0][inventory_item_id]': str(pcs_item.pk),
            'lines[0][purchase_unit_id]':  str(pcs_uc.pk),
            'lines[0][purchase_quantity]': '2.000',
            'lines[0][note]':              '',
        }
        response = self.client.post(CREATE_URL, data)
        self.assertEqual(response.status_code, 302)
        pcs_item.refresh_from_db()
        self.assertEqual(pcs_item.on_hand_quantity, Decimal('24.000'))

    def test_invalid_submission_does_not_change_stock(self):
        self._post(qty='-5.000', key='nostk-001')
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('0.000'))

    def test_purchase_form_renders_ok(self):
        response = self.client.get(CREATE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'idempotency_key')


# ═════════════════════════════════════════════════════════════════════════════
# 6. Regression: existing Phase 3-2 service tests unaffected
# ═════════════════════════════════════════════════════════════════════════════

class Phase33RegressionTests(TestCase):
    def setUp(self):
        self.admin = _user('p33reg_admin', 'admin')
        self.client.force_login(self.admin)

    def test_legacy_inventory_list_accessible(self):
        from django.urls import reverse as r
        self.assertEqual(self.client.get(r('inventory:list')).status_code, 200)

    def test_ingredient_list_accessible(self):
        from django.urls import reverse as r
        self.assertEqual(self.client.get(r('inventory:ingredient_list')).status_code, 200)

    def test_packaging_list_accessible(self):
        from django.urls import reverse as r
        self.assertEqual(self.client.get(r('inventory:packaging_list')).status_code, 200)

    def test_record_purchase_service_still_works_directly(self):
        """Service-level call unchanged by UI addition."""
        from apps.inventory.services import record_purchase
        item = _item('Reg Service Coffee', 'g')
        _uc(item, 'kg', Decimal('1000.0000'))
        uc = UnitConversion.objects.get(inventory_item=item)
        p = record_purchase(
            purchase_date=datetime.date(2026, 11, 1),
            lines=[{
                'inventory_item_id': item.pk,
                'purchase_unit':     uc.purchase_unit,
                'purchase_quantity': Decimal('5.000'),
                'conversion_factor': uc.conversion_factor,
            }],
            idempotency_key='reg-service-001',
        )
        item.refresh_from_db()
        self.assertEqual(item.on_hand_quantity, Decimal('5000.000'))
        self.assertIsNotNone(p.pk)


# ═════════════════════════════════════════════════════════════════════════════
# 7. Idempotency key — UUID validation and preservation
# ═════════════════════════════════════════════════════════════════════════════

class PurchaseIdempotencyKeyTests(TestCase):
    """
    The idempotency key is now a field on PurchaseHeaderForm (hidden),
    validated as a UUID4. These tests cover server-side UUID enforcement
    and preservation across failed submissions.
    """

    def setUp(self):
        self.admin = _user('pk_admin', 'admin')
        self.client.force_login(self.admin)
        self.item = _item('IK Coffee', 'g')
        self.uc   = _uc(self.item, 'kg', Decimal('1000.0000'))

    def _valid_post(self, key):
        import uuid as _uuid
        return self.client.post(CREATE_URL, {
            'purchase_date':   '2026-11-01',
            'notes':           '',
            'idempotency_key': key,
            'lines[0][inventory_item_id]': str(self.item.pk),
            'lines[0][purchase_unit_id]':  str(self.uc.pk),
            'lines[0][purchase_quantity]': '5.000',
            'lines[0][note]':              '',
        })

    def test_valid_uuid4_accepted(self):
        import uuid
        key = str(uuid.uuid4())
        response = self._valid_post(key)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(StockPurchase.objects.count(), 1)

    def test_invalid_uuid_rejected(self):
        response = self._valid_post('not-a-uuid')
        # Form validation fails → re-renders with 200
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StockPurchase.objects.count(), 0)

    def test_empty_key_rejected(self):
        response = self._valid_post('')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StockPurchase.objects.count(), 0)

    def test_key_preserved_on_validation_failure(self):
        """The same UUID must appear in the re-rendered form after a
        validation error so a network retry carries the correct key."""
        import uuid
        key = str(uuid.uuid4())
        # Submit with a bad quantity — form should re-render preserving the key.
        response = self.client.post(CREATE_URL, {
            'purchase_date':   '2026-11-01',
            'notes':           '',
            'idempotency_key': key,
            'lines[0][inventory_item_id]': str(self.item.pk),
            'lines[0][purchase_unit_id]':  str(self.uc.pk),
            'lines[0][purchase_quantity]': '-1.000',  # invalid
            'lines[0][note]':              '',
        })
        self.assertEqual(response.status_code, 200)
        # The re-rendered page must contain the original key so the browser
        # can re-submit with it.
        self.assertContains(response, key)

    def test_get_form_contains_uuid_key(self):
        """GET must embed a fresh UUID4 in the hidden field."""
        import uuid, re
        response = self.client.get(CREATE_URL)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        uuids = re.findall(
            r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',
            content, re.IGNORECASE,
        )
        self.assertTrue(len(uuids) >= 1, 'GET form must contain at least one UUID4')


# ═════════════════════════════════════════════════════════════════════════════
# 8. Form data preservation on validation failure
# ═════════════════════════════════════════════════════════════════════════════

class PurchaseFormDataPreservationTests(TestCase):
    """
    Verify that purchase date, notes, line item IDs, unit IDs, quantities,
    and notes survive a server-side validation failure and appear in the
    re-rendered response (via line_initial_json injected for JS re-population).
    """

    def setUp(self):
        self.admin = _user('fdp_admin', 'admin')
        self.client.force_login(self.admin)
        self.item = _item('FDP Coffee', 'g')
        self.uc   = _uc(self.item, 'kg', Decimal('1000.0000'))

    def _post_bad_qty(self, key=None):
        import uuid
        return self.client.post(CREATE_URL, {
            'purchase_date':   '2026-11-15',
            'notes':           'Supplier XYZ',
            'idempotency_key': key or str(uuid.uuid4()),
            'lines[0][inventory_item_id]': str(self.item.pk),
            'lines[0][purchase_unit_id]':  str(self.uc.pk),
            'lines[0][purchase_quantity]': '-1.000',
            'lines[0][note]':              'bad line note',
        })

    def test_purchase_date_preserved(self):
        response = self._post_bad_qty()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '2026-11-15')

    def test_notes_preserved(self):
        response = self._post_bad_qty()
        self.assertContains(response, 'Supplier XYZ')

    def test_line_initial_json_contains_item_id(self):
        """line_initial_json is injected into the template context so JS can
        re-populate the item dropdown."""
        response = self._post_bad_qty()
        self.assertContains(response, str(self.item.pk))

    def test_line_initial_json_contains_unit_id(self):
        response = self._post_bad_qty()
        self.assertContains(response, str(self.uc.pk))

    def test_line_initial_json_contains_quantity(self):
        response = self._post_bad_qty()
        # The raw submitted value '-1.000' is in line_initial_json for JS.
        self.assertContains(response, '-1.000')

    def test_line_note_preserved(self):
        response = self._post_bad_qty()
        self.assertContains(response, 'bad line note')


# ═════════════════════════════════════════════════════════════════════════════
# 9. Security — conversion factor cannot be inflated via POST
# ═════════════════════════════════════════════════════════════════════════════

class PurchaseSecurityTests(TestCase):
    """
    Additional security-focused tests for the purchase creation endpoint.
    """

    def setUp(self):
        self.admin = _user('sec_admin', 'admin')
        self.client.force_login(self.admin)
        self.item = _item('Sec Coffee', 'g')
        self.uc   = _uc(self.item, 'kg', Decimal('1000.0000'))

    def test_cross_item_uc_pk_rejected(self):
        """A UnitConversion that belongs to a different item must be rejected,
        even if its PK is submitted alongside the wrong inventory_item_id.
        This prevents an attacker from pairing a high-factor UC with a
        different item to inflate stock."""
        item2 = _item('Sec Milk', 'ml')
        uc2   = _uc(item2, 'L', Decimal('1000.0000'))

        import uuid
        response = self.client.post(CREATE_URL, {
            'purchase_date':   '2026-11-01',
            'notes':           '',
            'idempotency_key': str(uuid.uuid4()),
            # Submit item2's UC PK but claim it belongs to self.item
            'lines[0][inventory_item_id]': str(self.item.pk),
            'lines[0][purchase_unit_id]':  str(uc2.pk),  # ← belongs to item2, not self.item
            'lines[0][purchase_quantity]': '5.000',
            'lines[0][note]':              '',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StockPurchase.objects.count(), 0)
        self.item.refresh_from_db()
        self.assertEqual(self.item.on_hand_quantity, Decimal('0.000'))

    def test_string_item_id_rejected_gracefully(self):
        """Non-integer item IDs must not cause a 500."""
        import uuid
        response = self.client.post(CREATE_URL, {
            'purchase_date':   '2026-11-01',
            'notes':           '',
            'idempotency_key': str(uuid.uuid4()),
            'lines[0][inventory_item_id]': 'not-an-int',
            'lines[0][purchase_unit_id]':  str(self.uc.pk),
            'lines[0][purchase_quantity]': '5.000',
            'lines[0][note]':              '',
        })
        self.assertIn(response.status_code, (200, 400))
        self.assertEqual(StockPurchase.objects.count(), 0)

    def test_string_unit_id_rejected_gracefully(self):
        import uuid
        response = self.client.post(CREATE_URL, {
            'purchase_date':   '2026-11-01',
            'notes':           '',
            'idempotency_key': str(uuid.uuid4()),
            'lines[0][inventory_item_id]': str(self.item.pk),
            'lines[0][purchase_unit_id]':  'not-an-int',
            'lines[0][purchase_quantity]': '5.000',
            'lines[0][note]':              '',
        })
        self.assertIn(response.status_code, (200, 400))
        self.assertEqual(StockPurchase.objects.count(), 0)

    def test_conversion_factor_field_in_post_is_ignored(self):
        """An injected conversion_factor POST field must not affect the
        recorded StockPurchaseItem snapshot — the factor always comes from DB."""
        import uuid
        key = str(uuid.uuid4())
        self.client.post(CREATE_URL, {
            'purchase_date':   '2026-11-01',
            'notes':           '',
            'idempotency_key': key,
            'lines[0][inventory_item_id]': str(self.item.pk),
            'lines[0][purchase_unit_id]':  str(self.uc.pk),
            'lines[0][purchase_quantity]': '1.000',
            'lines[0][note]':              '',
            'lines[0][conversion_factor]': '9999999.0000',  # injected
        })
        spi = StockPurchaseItem.objects.filter(inventory_item=self.item).first()
        if spi:
            self.assertEqual(spi.conversion_factor, self.uc.conversion_factor)
            self.assertNotEqual(spi.conversion_factor, Decimal('9999999.0000'))
            self.assertEqual(spi.base_quantity, Decimal('1000.000'))
