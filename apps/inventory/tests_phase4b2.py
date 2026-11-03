"""
tests_phase4b2.py
=================
Phase 4B-2 — PackagingProfile, PackagingProfileItem, and ProductPackagingRule
model tests.

Covers (Rev 3 §D.6):

  PackagingProfile
  ─────────────────
  1.  Creation / field defaults
  2.  All 3 valid STATUS_CHOICES accepted
  3.  DB constraint: pp_revision_unique        (name, revision unique)
  4.  DB constraint: pp_one_approved_per_name  (partial unique, case-insensitive)
  5.  DB constraint: pp_approved_has_approval  (approved → approved_at not null)
  6.  DB constraint: pp_status_valid           (unknown status rejected)
  7.  DB constraint: pp_revision_positive      (revision 0 rejected)
  8.  Draft + retired coexist freely
  9.  Two approved profiles with DIFFERENT names coexist
  10. __str__ smoke-test
  11. All 5 constraint names present on the model

  PackagingProfileItem
  ─────────────────────
  12. Creation / fields
  13. Positive quantity accepted
  14. Zero quantity rejected (ppi_quantity_positive)
  15. Negative quantity rejected
  16. Duplicate item in same profile rejected (ppi_unique_item)
  17. Different items in same profile accepted
  18. Same item in different profiles accepted
  19. CASCADE on PackagingProfile delete
  20. PROTECT on InventoryItem delete
  21. Both constraint names present

  ProductPackagingRule
  ─────────────────────
  22. Creation with profile (no_packaging=False)
  23. Creation with no_packaging=True (profile=None)
  24. All 5 valid SIZE_CHOICES accepted
  25. Both valid ORDER_TYPE_CHOICES accepted (dine_in, takeout)
  26. DB constraint: ppr_unique_combination    (product, size, order_type unique)
  27. DB constraint: ppr_profile_xor_none      (profile null ↔ no_packaging True)
       a. profile set + no_packaging=False → accepted
       b. profile null + no_packaging=True → accepted
       c. profile null + no_packaging=False → rejected (silence)
       d. profile set + no_packaging=True → rejected (contradiction)
  28. DB constraint: ppr_size_valid            (unknown size rejected)
  29. DB constraint: ppr_order_type_valid      (unknown / "all" order_type rejected)
  30. Optional confirmation fields (confirmed_by / confirmed_at / updated_by / updated_at)
  31. PROTECT on Product delete when a rule references it
  32. PROTECT on PackagingProfile delete when a rule references it
  33. SET_NULL on User delete (confirmed_by, updated_by)
  34. All 4 constraint names present

  Cross-model
  ────────────
  35. Resolved plan structure: one Profile + one Rule pointing to it, verified
      through ORM relationships (no services — models only)

All tests use synthetic fixtures.
DB constraint tests use transaction.atomic() savepoints.
All tests are SQLite-safe.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db import models
from django.test import TestCase
from django.utils import timezone

from apps.inventory.models import (
    InventoryItem,
    PackagingProfile,
    PackagingProfileItem,
    ProductPackagingRule,
)
from apps.menu.models import Category, Product

User = get_user_model()

_PASSWORD = 'test-4b2-pass'
_counter = 0


def _uid():
    global _counter
    _counter += 1
    return str(_counter)


def _user(username=None, role='admin'):
    uid = username or f'u_{_uid()}'
    return User.objects.create_user(username=uid, password=_PASSWORD, role=role)


def _category(name=None):
    n = name or f'Cat_{_uid()}'
    return Category.objects.create(name=n, slug=n.lower().replace(' ', '-'))


def _product(name=None, category=None, price='65.00'):
    cat = category or _category()
    n = name or f'Prod_{_uid()}'
    return Product.objects.create(
        category=cat, name=n, price=Decimal(price), stock_quantity=100,
    )


def _inventory_item(name=None, item_type='packaging', base_unit='pcs',
                    on_hand=Decimal('500.000')):
    n = name or f'Item_{_uid()}'
    return InventoryItem.objects.create(
        name=n, item_type=item_type, base_unit=base_unit,
        on_hand_quantity=on_hand,
    )


def _profile(name=None, status='draft', revision=1,
             approved_by=None, approved_at=None, approval_note=''):
    n = name or f'Profile_{_uid()}'
    return PackagingProfile.objects.create(
        name=n, status=status, revision=revision,
        approved_by=approved_by,
        approved_at=approved_at,
        approval_note=approval_note,
    )


def _approved_profile(name=None, revision=1, **kwargs):
    kwargs.setdefault('status', 'approved')
    kwargs.setdefault('approved_at', timezone.now())
    kwargs.setdefault('approval_note', 'Approved for test')
    return _profile(name=name, revision=revision, **kwargs)


# ═════════════════════════════════════════════════════════════════════════════
# 1. PackagingProfile — creation and defaults
# ═════════════════════════════════════════════════════════════════════════════

class PackagingProfileCreationTests(TestCase):

    def test_create_draft_profile(self):
        p = _profile()
        self.assertIsNotNone(p.pk)

    def test_default_status_is_draft(self):
        p = PackagingProfile.objects.create(name=f'P_{_uid()}', revision=1)
        self.assertEqual(p.status, PackagingProfile.STATUS_DRAFT)

    def test_approval_fields_default_null_blank(self):
        p = _profile()
        self.assertIsNone(p.approved_by)
        self.assertIsNone(p.approved_at)
        self.assertEqual(p.approval_note, '')

    def test_created_at_and_updated_at_set(self):
        p = _profile()
        self.assertIsNotNone(p.created_at)
        self.assertIsNotNone(p.updated_at)

    def test_str_smoke(self):
        p = _profile(name='Cup Set', revision=1)
        s = str(p)
        self.assertIn('Cup Set', s)
        self.assertIn('1', s)
        self.assertIn('draft', s)


# ═════════════════════════════════════════════════════════════════════════════
# 2. PackagingProfile — all valid STATUS_CHOICES accepted
# ═════════════════════════════════════════════════════════════════════════════

class PackagingProfileStatusChoicesTests(TestCase):

    def test_status_draft_accepted(self):
        p = _profile(status='draft')
        p.refresh_from_db()
        self.assertEqual(p.status, 'draft')

    def test_status_approved_accepted(self):
        p = _approved_profile()
        p.refresh_from_db()
        self.assertEqual(p.status, 'approved')

    def test_status_retired_accepted(self):
        p = _profile(status='retired')
        p.refresh_from_db()
        self.assertEqual(p.status, 'retired')


# ═════════════════════════════════════════════════════════════════════════════
# 3. DB constraint: pp_revision_unique
# ═════════════════════════════════════════════════════════════════════════════

class PackagingProfileRevisionUniqueTests(TestCase):

    def setUp(self):
        self.name = f'Profile_{_uid()}'
        _profile(name=self.name, revision=1)

    def test_duplicate_name_revision_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _profile(name=self.name, revision=1, status='retired')

    def test_different_revision_same_name_accepted(self):
        p = _profile(name=self.name, revision=2)
        self.assertIsNotNone(p.pk)

    def test_same_revision_different_name_accepted(self):
        p = _profile(name=f'Other_{_uid()}', revision=1)
        self.assertIsNotNone(p.pk)

    def test_constraint_name_present(self):
        names = {c.name for c in PackagingProfile._meta.constraints}
        self.assertIn('pp_revision_unique', names)


# ═════════════════════════════════════════════════════════════════════════════
# 4. DB constraint: pp_one_approved_per_name (partial unique, case-insensitive)
# ═════════════════════════════════════════════════════════════════════════════

class PackagingProfileOneApprovedPerNameTests(TestCase):

    def setUp(self):
        self.name = f'CupProfile_{_uid()}'
        self.p1 = _approved_profile(name=self.name, revision=1)

    def test_second_approved_same_name_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _approved_profile(name=self.name, revision=2)

    def test_second_approved_same_name_different_case_rejected(self):
        """Case-insensitive: 'CUPPROFILE_x' and 'cupprofile_x' are the same."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _approved_profile(name=self.name.upper(), revision=3)

    def test_draft_same_name_not_blocked(self):
        """Only approved status triggers the partial unique."""
        p = _profile(name=self.name, revision=2, status='draft')
        self.assertIsNotNone(p.pk)

    def test_retired_same_name_not_blocked(self):
        p = _profile(name=self.name, revision=2, status='retired')
        self.assertIsNotNone(p.pk)

    def test_approved_different_name_accepted(self):
        p = _approved_profile(name=f'Other_{_uid()}', revision=1)
        self.assertIsNotNone(p.pk)

    def test_constraint_name_present(self):
        names = {c.name for c in PackagingProfile._meta.constraints}
        self.assertIn('pp_one_approved_per_name', names)


# ═════════════════════════════════════════════════════════════════════════════
# 5. DB constraint: pp_approved_has_approval
# ═════════════════════════════════════════════════════════════════════════════

class PackagingProfileApprovedHasApprovalTests(TestCase):

    def test_approved_with_approved_at_accepted(self):
        p = _approved_profile()
        self.assertIsNotNone(p.approved_at)

    def test_approved_without_approved_at_rejected(self):
        with self.assertRaises(Exception):
            with transaction.atomic():
                _profile(status='approved', approved_at=None)

    def test_draft_without_approved_at_accepted(self):
        p = _profile(status='draft', approved_at=None)
        self.assertIsNotNone(p.pk)

    def test_retired_without_approved_at_accepted(self):
        p = _profile(status='retired', approved_at=None)
        self.assertIsNotNone(p.pk)

    def test_constraint_name_present(self):
        names = {c.name for c in PackagingProfile._meta.constraints}
        self.assertIn('pp_approved_has_approval', names)


# ═════════════════════════════════════════════════════════════════════════════
# 6. DB constraint: pp_status_valid
# ═════════════════════════════════════════════════════════════════════════════

class PackagingProfileStatusValidTests(TestCase):

    def test_unknown_status_rejected(self):
        with self.assertRaises(Exception):
            with transaction.atomic():
                PackagingProfile.objects.create(
                    name=f'P_{_uid()}', status='pending', revision=1,
                )

    def test_constraint_name_present(self):
        names = {c.name for c in PackagingProfile._meta.constraints}
        self.assertIn('pp_status_valid', names)


# ═════════════════════════════════════════════════════════════════════════════
# 7. DB constraint: pp_revision_positive
# ═════════════════════════════════════════════════════════════════════════════

class PackagingProfileRevisionPositiveTests(TestCase):

    def test_revision_one_accepted(self):
        p = _profile(revision=1)
        self.assertIsNotNone(p.pk)

    def test_revision_zero_rejected(self):
        with self.assertRaises(Exception):
            with transaction.atomic():
                PackagingProfile.objects.create(
                    name=f'P_{_uid()}', status='draft', revision=0,
                )

    def test_constraint_name_present(self):
        names = {c.name for c in PackagingProfile._meta.constraints}
        self.assertIn('pp_revision_positive', names)


# ═════════════════════════════════════════════════════════════════════════════
# 8. Draft + retired profiles coexist freely
# ═════════════════════════════════════════════════════════════════════════════

class PackagingProfileCoexistenceTests(TestCase):

    def setUp(self):
        self.name = f'SharedName_{_uid()}'

    def test_multiple_drafts_coexist(self):
        p1 = _profile(name=self.name, revision=1, status='draft')
        p2 = _profile(name=self.name, revision=2, status='draft')
        self.assertIsNotNone(p1.pk)
        self.assertIsNotNone(p2.pk)

    def test_draft_and_retired_coexist(self):
        p1 = _profile(name=self.name, revision=1, status='retired')
        p2 = _profile(name=self.name, revision=2, status='draft')
        self.assertIsNotNone(p1.pk)
        self.assertIsNotNone(p2.pk)

    def test_multiple_retired_coexist(self):
        for rev in range(1, 5):
            p = _profile(name=self.name, revision=rev, status='retired')
            self.assertIsNotNone(p.pk)


# ═════════════════════════════════════════════════════════════════════════════
# 9. Two approved profiles with different names coexist
# ═════════════════════════════════════════════════════════════════════════════

class PackagingProfileTwoApprovedDifferentNamesTests(TestCase):

    def test_two_approved_different_names_accepted(self):
        p1 = _approved_profile(name=f'ProfileA_{_uid()}', revision=1)
        p2 = _approved_profile(name=f'ProfileB_{_uid()}', revision=1)
        self.assertIsNotNone(p1.pk)
        self.assertIsNotNone(p2.pk)
        self.assertNotEqual(p1.pk, p2.pk)


# ═════════════════════════════════════════════════════════════════════════════
# 11. All PackagingProfile constraint names present
# ═════════════════════════════════════════════════════════════════════════════

class PackagingProfileAllConstraintNamesTests(TestCase):

    _REQUIRED = {
        'pp_revision_unique',
        'pp_one_approved_per_name',
        'pp_approved_has_approval',
        'pp_status_valid',
        'pp_revision_positive',
    }

    def test_all_constraint_names_present(self):
        actual = {c.name for c in PackagingProfile._meta.constraints}
        for name in self._REQUIRED:
            with self.subTest(constraint=name):
                self.assertIn(name, actual)


# ═════════════════════════════════════════════════════════════════════════════
# 12. PackagingProfileItem — creation and fields
# ═════════════════════════════════════════════════════════════════════════════

class PackagingProfileItemCreationTests(TestCase):

    def setUp(self):
        self.profile = _profile()
        self.item = _inventory_item()

    def test_create_item(self):
        ppi = PackagingProfileItem.objects.create(
            profile=self.profile,
            inventory_item=self.item,
            quantity=Decimal('2.000'),
        )
        self.assertIsNotNone(ppi.pk)

    def test_quantity_stored_correctly(self):
        ppi = PackagingProfileItem.objects.create(
            profile=self.profile,
            inventory_item=self.item,
            quantity=Decimal('12.500'),
        )
        ppi.refresh_from_db()
        self.assertEqual(ppi.quantity, Decimal('12.500'))

    def test_str_smoke(self):
        ppi = PackagingProfileItem.objects.create(
            profile=self.profile,
            inventory_item=self.item,
            quantity=Decimal('1.000'),
        )
        s = str(ppi)
        self.assertIn(str(self.profile.pk), s)
        self.assertIn(str(self.item.pk), s)

    def test_two_constraint_names_present(self):
        names = {c.name for c in PackagingProfileItem._meta.constraints}
        self.assertIn('ppi_quantity_positive', names)
        self.assertIn('ppi_unique_item', names)


# ═════════════════════════════════════════════════════════════════════════════
# 13–15. PackagingProfileItem — ppi_quantity_positive
# ═════════════════════════════════════════════════════════════════════════════

class PackagingProfileItemQuantityPositiveTests(TestCase):

    def setUp(self):
        self.profile = _profile()

    def test_positive_quantity_accepted(self):
        item = _inventory_item()
        ppi = PackagingProfileItem.objects.create(
            profile=self.profile, inventory_item=item,
            quantity=Decimal('0.001'),
        )
        self.assertIsNotNone(ppi.pk)

    def test_large_positive_quantity_accepted(self):
        item = _inventory_item()
        ppi = PackagingProfileItem.objects.create(
            profile=self.profile, inventory_item=item,
            quantity=Decimal('9999.999'),
        )
        self.assertIsNotNone(ppi.pk)

    def test_zero_quantity_rejected(self):
        item = _inventory_item()
        with self.assertRaises(Exception):
            with transaction.atomic():
                PackagingProfileItem.objects.create(
                    profile=self.profile, inventory_item=item,
                    quantity=Decimal('0.000'),
                )

    def test_negative_quantity_rejected(self):
        item = _inventory_item()
        with self.assertRaises(Exception):
            with transaction.atomic():
                PackagingProfileItem.objects.create(
                    profile=self.profile, inventory_item=item,
                    quantity=Decimal('-1.000'),
                )


# ═════════════════════════════════════════════════════════════════════════════
# 16–18. PackagingProfileItem — ppi_unique_item
# ═════════════════════════════════════════════════════════════════════════════

class PackagingProfileItemUniqueItemTests(TestCase):

    def setUp(self):
        self.profile = _profile()
        self.item = _inventory_item(name=f'Cup_{_uid()}')

    def test_same_item_twice_in_profile_rejected(self):
        PackagingProfileItem.objects.create(
            profile=self.profile, inventory_item=self.item,
            quantity=Decimal('1.000'),
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PackagingProfileItem.objects.create(
                    profile=self.profile, inventory_item=self.item,
                    quantity=Decimal('2.000'),
                )

    def test_different_items_same_profile_accepted(self):
        item2 = _inventory_item(name=f'Lid_{_uid()}')
        ppi1 = PackagingProfileItem.objects.create(
            profile=self.profile, inventory_item=self.item,
            quantity=Decimal('1.000'),
        )
        ppi2 = PackagingProfileItem.objects.create(
            profile=self.profile, inventory_item=item2,
            quantity=Decimal('1.000'),
        )
        self.assertIsNotNone(ppi1.pk)
        self.assertIsNotNone(ppi2.pk)

    def test_same_item_different_profiles_accepted(self):
        profile2 = _profile()
        ppi1 = PackagingProfileItem.objects.create(
            profile=self.profile, inventory_item=self.item,
            quantity=Decimal('1.000'),
        )
        ppi2 = PackagingProfileItem.objects.create(
            profile=profile2, inventory_item=self.item,
            quantity=Decimal('1.000'),
        )
        self.assertIsNotNone(ppi1.pk)
        self.assertIsNotNone(ppi2.pk)


# ═════════════════════════════════════════════════════════════════════════════
# 19. PackagingProfileItem CASCADE on profile delete
# ═════════════════════════════════════════════════════════════════════════════

class PackagingProfileItemCascadeTests(TestCase):

    def test_profile_delete_removes_items(self):
        profile = _profile()
        item1 = _inventory_item(name=f'CupC_{_uid()}')
        item2 = _inventory_item(name=f'LidC_{_uid()}')
        ppi1 = PackagingProfileItem.objects.create(
            profile=profile, inventory_item=item1, quantity=Decimal('1.000'),
        )
        ppi2 = PackagingProfileItem.objects.create(
            profile=profile, inventory_item=item2, quantity=Decimal('1.000'),
        )
        profile.delete()
        self.assertFalse(
            PackagingProfileItem.objects.filter(pk__in=[ppi1.pk, ppi2.pk]).exists(),
            'PackagingProfileItem rows must cascade-delete with their profile',
        )


# ═════════════════════════════════════════════════════════════════════════════
# 20. PackagingProfileItem PROTECT on InventoryItem delete
# ═════════════════════════════════════════════════════════════════════════════

class PackagingProfileItemProtectTests(TestCase):

    def test_delete_referenced_item_blocked(self):
        profile = _profile()
        item = _inventory_item(name=f'Protected_{_uid()}')
        PackagingProfileItem.objects.create(
            profile=profile, inventory_item=item, quantity=Decimal('1.000'),
        )
        with self.assertRaises(Exception):
            with transaction.atomic():
                item.delete()

    def test_delete_unreferenced_item_allowed(self):
        item = _inventory_item(name=f'Orphan_{_uid()}')
        pk = item.pk
        item.delete()
        self.assertFalse(InventoryItem.objects.filter(pk=pk).exists())


# ═════════════════════════════════════════════════════════════════════════════
# 22–23. ProductPackagingRule — creation
# ═════════════════════════════════════════════════════════════════════════════

class ProductPackagingRuleCreationTests(TestCase):

    def setUp(self):
        self.product = _product()
        self.profile = _approved_profile()

    def test_create_rule_with_profile(self):
        rule = ProductPackagingRule.objects.create(
            product=self.product,
            size='none',
            order_type='takeout',
            profile=self.profile,
            no_packaging=False,
        )
        self.assertIsNotNone(rule.pk)

    def test_create_rule_no_packaging(self):
        rule = ProductPackagingRule.objects.create(
            product=self.product,
            size='none',
            order_type='dine_in',
            profile=None,
            no_packaging=True,
        )
        self.assertIsNotNone(rule.pk)

    def test_no_packaging_default_is_false(self):
        """no_packaging defaults to False (the profile outcome path)."""
        field = ProductPackagingRule._meta.get_field('no_packaging')
        self.assertFalse(field.default)

    def test_profile_stored_correctly(self):
        rule = ProductPackagingRule.objects.create(
            product=self.product,
            size='small',
            order_type='takeout',
            profile=self.profile,
            no_packaging=False,
        )
        rule.refresh_from_db()
        self.assertEqual(rule.profile_id, self.profile.pk)

    def test_confirmation_fields_optional(self):
        """confirmed_by / confirmed_at / updated_by / updated_at are all optional."""
        rule = ProductPackagingRule.objects.create(
            product=self.product,
            size='medium',
            order_type='dine_in',
            profile=None,
            no_packaging=True,
        )
        self.assertIsNone(rule.confirmed_by)
        self.assertIsNone(rule.confirmed_at)
        self.assertIsNone(rule.updated_by)
        self.assertIsNone(rule.updated_at)

    def test_str_smoke_with_profile(self):
        rule = ProductPackagingRule.objects.create(
            product=self.product,
            size='none',
            order_type='takeout',
            profile=self.profile,
            no_packaging=False,
        )
        s = str(rule)
        self.assertIn(str(self.product.pk), s)
        self.assertIn('takeout', s)

    def test_str_smoke_no_packaging(self):
        rule = ProductPackagingRule.objects.create(
            product=self.product,
            size='none',
            order_type='dine_in',
            profile=None,
            no_packaging=True,
        )
        s = str(rule)
        self.assertIn('no_packaging', s)


# ═════════════════════════════════════════════════════════════════════════════
# 24. ProductPackagingRule — all valid SIZE_CHOICES accepted
# ═════════════════════════════════════════════════════════════════════════════

class ProductPackagingRuleSizeChoicesTests(TestCase):

    def setUp(self):
        self.profile = _approved_profile()

    def _rule(self, size, order_type='dine_in'):
        return ProductPackagingRule.objects.create(
            product=_product(),
            size=size,
            order_type=order_type,
            profile=None,
            no_packaging=True,
        )

    def test_size_none_accepted(self):
        r = self._rule('none')
        self.assertEqual(r.size, 'none')

    def test_size_small_accepted(self):
        r = self._rule('small')
        self.assertEqual(r.size, 'small')

    def test_size_medium_accepted(self):
        r = self._rule('medium')
        self.assertEqual(r.size, 'medium')

    def test_size_large_accepted(self):
        r = self._rule('large')
        self.assertEqual(r.size, 'large')

    def test_size_hot_accepted(self):
        r = self._rule('hot')
        self.assertEqual(r.size, 'hot')

    def test_size_choices_has_five_entries(self):
        self.assertEqual(len(ProductPackagingRule.SIZE_CHOICES), 5)


# ═════════════════════════════════════════════════════════════════════════════
# 25. ProductPackagingRule — both ORDER_TYPE_CHOICES accepted; no "all"
# ═════════════════════════════════════════════════════════════════════════════

class ProductPackagingRuleOrderTypeTests(TestCase):

    def test_order_type_dine_in_accepted(self):
        rule = ProductPackagingRule.objects.create(
            product=_product(), size='none', order_type='dine_in',
            profile=None, no_packaging=True,
        )
        self.assertEqual(rule.order_type, 'dine_in')

    def test_order_type_takeout_accepted(self):
        rule = ProductPackagingRule.objects.create(
            product=_product(), size='none', order_type='takeout',
            profile=None, no_packaging=True,
        )
        self.assertEqual(rule.order_type, 'takeout')

    def test_order_type_choices_has_two_entries(self):
        """Rev3 §D.6: no 'all' scope for rules."""
        self.assertEqual(len(ProductPackagingRule.ORDER_TYPE_CHOICES), 2)
        values = {c[0] for c in ProductPackagingRule.ORDER_TYPE_CHOICES}
        self.assertNotIn('all', values)

    def test_dine_in_and_takeout_same_product_size_coexist(self):
        """One rule per order_type per (product, size): dine_in + takeout can coexist."""
        prod = _product()
        r1 = ProductPackagingRule.objects.create(
            product=prod, size='none', order_type='dine_in',
            profile=None, no_packaging=True,
        )
        r2 = ProductPackagingRule.objects.create(
            product=prod, size='none', order_type='takeout',
            profile=None, no_packaging=True,
        )
        self.assertIsNotNone(r1.pk)
        self.assertIsNotNone(r2.pk)


# ═════════════════════════════════════════════════════════════════════════════
# 26. DB constraint: ppr_unique_combination
# ═════════════════════════════════════════════════════════════════════════════

class ProductPackagingRuleUniqueCombinationTests(TestCase):

    def setUp(self):
        self.product = _product()
        ProductPackagingRule.objects.create(
            product=self.product, size='none', order_type='takeout',
            profile=None, no_packaging=True,
        )

    def test_duplicate_combination_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProductPackagingRule.objects.create(
                    product=self.product, size='none', order_type='takeout',
                    profile=None, no_packaging=True,
                )

    def test_different_size_same_product_accepted(self):
        r = ProductPackagingRule.objects.create(
            product=self.product, size='small', order_type='takeout',
            profile=None, no_packaging=True,
        )
        self.assertIsNotNone(r.pk)

    def test_different_order_type_same_product_accepted(self):
        r = ProductPackagingRule.objects.create(
            product=self.product, size='none', order_type='dine_in',
            profile=None, no_packaging=True,
        )
        self.assertIsNotNone(r.pk)

    def test_different_product_same_size_order_type_accepted(self):
        r = ProductPackagingRule.objects.create(
            product=_product(), size='none', order_type='takeout',
            profile=None, no_packaging=True,
        )
        self.assertIsNotNone(r.pk)

    def test_constraint_name_present(self):
        names = {c.name for c in ProductPackagingRule._meta.constraints}
        self.assertIn('ppr_unique_combination', names)


# ═════════════════════════════════════════════════════════════════════════════
# 27. DB constraint: ppr_profile_xor_none
# ═════════════════════════════════════════════════════════════════════════════

class ProductPackagingRuleProfileXorNoneTests(TestCase):

    def setUp(self):
        self.profile = _approved_profile()

    def test_profile_set_no_packaging_false_accepted(self):
        """The normal packaged path: profile is set and no_packaging is False."""
        rule = ProductPackagingRule.objects.create(
            product=_product(), size='none', order_type='takeout',
            profile=self.profile, no_packaging=False,
        )
        self.assertIsNotNone(rule.pk)

    def test_profile_null_no_packaging_true_accepted(self):
        """The explicit no-packaging path: profile is null and no_packaging is True."""
        rule = ProductPackagingRule.objects.create(
            product=_product(), size='none', order_type='dine_in',
            profile=None, no_packaging=True,
        )
        self.assertIsNotNone(rule.pk)

    def test_profile_null_no_packaging_false_rejected(self):
        """Silence: profile null but no_packaging False — forbidden by Rev3 §D.6."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                ProductPackagingRule.objects.create(
                    product=_product(), size='small', order_type='takeout',
                    profile=None, no_packaging=False,
                )

    def test_profile_set_no_packaging_true_rejected(self):
        """Contradiction: profile set AND no_packaging True — forbidden."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                ProductPackagingRule.objects.create(
                    product=_product(), size='small', order_type='dine_in',
                    profile=self.profile, no_packaging=True,
                )

    def test_constraint_name_present(self):
        names = {c.name for c in ProductPackagingRule._meta.constraints}
        self.assertIn('ppr_profile_xor_none', names)


# ═════════════════════════════════════════════════════════════════════════════
# 28. DB constraint: ppr_size_valid
# ═════════════════════════════════════════════════════════════════════════════

class ProductPackagingRuleSizeValidTests(TestCase):

    def test_unknown_size_rejected(self):
        with self.assertRaises(Exception):
            with transaction.atomic():
                ProductPackagingRule.objects.create(
                    product=_product(), size='jumbo', order_type='takeout',
                    profile=None, no_packaging=True,
                )

    def test_constraint_name_present(self):
        names = {c.name for c in ProductPackagingRule._meta.constraints}
        self.assertIn('ppr_size_valid', names)


# ═════════════════════════════════════════════════════════════════════════════
# 29. DB constraint: ppr_order_type_valid  (also rejects "all")
# ═════════════════════════════════════════════════════════════════════════════

class ProductPackagingRuleOrderTypeValidTests(TestCase):

    def test_unknown_order_type_rejected(self):
        with self.assertRaises(Exception):
            with transaction.atomic():
                ProductPackagingRule.objects.create(
                    product=_product(), size='none', order_type='delivery',
                    profile=None, no_packaging=True,
                )

    def test_all_order_type_rejected(self):
        """'all' is explicitly excluded from ProductPackagingRule (§D.6)."""
        with self.assertRaises(Exception):
            with transaction.atomic():
                ProductPackagingRule.objects.create(
                    product=_product(), size='none', order_type='all',
                    profile=None, no_packaging=True,
                )

    def test_constraint_name_present(self):
        names = {c.name for c in ProductPackagingRule._meta.constraints}
        self.assertIn('ppr_order_type_valid', names)


# ═════════════════════════════════════════════════════════════════════════════
# 30. ProductPackagingRule — confirmation fields round-trip
# ═════════════════════════════════════════════════════════════════════════════

class ProductPackagingRuleConfirmationFieldsTests(TestCase):

    def test_confirmation_fields_stored(self):
        staff = _user()
        now = timezone.now()
        rule = ProductPackagingRule.objects.create(
            product=_product(),
            size='none',
            order_type='takeout',
            profile=None,
            no_packaging=True,
            confirmed_by=staff,
            confirmed_at=now,
            updated_by=staff,
            updated_at=now,
        )
        rule.refresh_from_db()
        self.assertEqual(rule.confirmed_by_id, staff.pk)
        self.assertEqual(rule.updated_by_id, staff.pk)
        self.assertIsNotNone(rule.confirmed_at)
        self.assertIsNotNone(rule.updated_at)


# ═════════════════════════════════════════════════════════════════════════════
# 31. ProductPackagingRule — PROTECT on Product delete
# ═════════════════════════════════════════════════════════════════════════════

class ProductPackagingRuleProductProtectTests(TestCase):
    """
    Product.delete() is soft-deactivation (overridden on the model; returns
    0 rows deleted without removing the DB row). The PROTECT on
    ProductPackagingRule.product is therefore tested at the DB/ORM level
    via a raw QuerySet.delete() call, which the pre_delete signal converts
    to a ProtectedError. This proves the DB-level protection is wired up.
    """

    def test_queryset_delete_product_with_rule_blocked(self):
        """Bulk queryset delete on a product referenced by a rule must be blocked
        by the pre_delete signal (PRODUCT_SOFT_DELETE_ERROR guard) or the
        DB PROTECT — either way, the product must survive."""
        product = _product()
        ProductPackagingRule.objects.create(
            product=product, size='none', order_type='takeout',
            profile=None, no_packaging=True,
        )
        pk = product.pk
        with self.assertRaises(Exception):
            with transaction.atomic():
                Product.objects.filter(pk=pk).delete()
        # Row must still exist.
        self.assertTrue(Product.objects.filter(pk=pk).exists())


# ═════════════════════════════════════════════════════════════════════════════
# 32. ProductPackagingRule — PROTECT on PackagingProfile delete
# ═════════════════════════════════════════════════════════════════════════════

class ProductPackagingRuleProfileProtectTests(TestCase):

    def test_delete_profile_with_rule_blocked(self):
        profile = _approved_profile()
        ProductPackagingRule.objects.create(
            product=_product(), size='none', order_type='takeout',
            profile=profile, no_packaging=False,
        )
        with self.assertRaises(Exception):
            with transaction.atomic():
                profile.delete()

    def test_delete_profile_without_rule_allowed(self):
        profile = _profile()
        pk = profile.pk
        profile.delete()
        self.assertFalse(PackagingProfile.objects.filter(pk=pk).exists())


# ═════════════════════════════════════════════════════════════════════════════
# 33. ProductPackagingRule — SET_NULL declared on User FKs
# ═════════════════════════════════════════════════════════════════════════════

class ProductPackagingRuleUserSetNullTests(TestCase):
    """
    CustomUser hard-deletes are blocked by a pre_delete signal in this
    application (accounts never leave the DB; they are deactivated instead).
    The on_delete=SET_NULL behavior therefore cannot be triggered through the
    normal ORM path. These tests verify the field declaration, which is what
    Django uses to generate the migration DDL and which PostgreSQL enforces
    at the DB level if the signal guard were ever relaxed.
    """

    def test_confirmed_by_on_delete_is_set_null(self):
        field = ProductPackagingRule._meta.get_field('confirmed_by')
        self.assertEqual(
            field.remote_field.on_delete,
            models.SET_NULL,
            'confirmed_by must declare on_delete=SET_NULL',
        )

    def test_updated_by_on_delete_is_set_null(self):
        field = ProductPackagingRule._meta.get_field('updated_by')
        self.assertEqual(
            field.remote_field.on_delete,
            models.SET_NULL,
            'updated_by must declare on_delete=SET_NULL',
        )

    def test_confirmed_by_is_nullable(self):
        field = ProductPackagingRule._meta.get_field('confirmed_by')
        self.assertTrue(field.null, 'confirmed_by must be nullable')

    def test_updated_by_is_nullable(self):
        field = ProductPackagingRule._meta.get_field('updated_by')
        self.assertTrue(field.null, 'updated_by must be nullable')


# ═════════════════════════════════════════════════════════════════════════════
# 34. All ProductPackagingRule constraint names present
# ═════════════════════════════════════════════════════════════════════════════

class ProductPackagingRuleAllConstraintNamesTests(TestCase):

    _REQUIRED = {
        'ppr_unique_combination',
        'ppr_profile_xor_none',
        'ppr_size_valid',
        'ppr_order_type_valid',
    }

    def test_all_constraint_names_present(self):
        actual = {c.name for c in ProductPackagingRule._meta.constraints}
        for name in self._REQUIRED:
            with self.subTest(constraint=name):
                self.assertIn(name, actual)


# ═════════════════════════════════════════════════════════════════════════════
# 35. Cross-model: ORM relationship traversal
# ═════════════════════════════════════════════════════════════════════════════

class PackagingOrmRelationshipTests(TestCase):
    """
    Verify that the ORM relationships between PackagingProfile,
    PackagingProfileItem, and ProductPackagingRule work correctly —
    modelling what resolve_plan() will do in Phase 5B without calling
    any service.
    """

    def test_profile_items_accessible_via_related_manager(self):
        profile = _profile()
        item1 = _inventory_item(name=f'Cup_{_uid()}')
        item2 = _inventory_item(name=f'Lid_{_uid()}')
        PackagingProfileItem.objects.create(
            profile=profile, inventory_item=item1, quantity=Decimal('1.000'),
        )
        PackagingProfileItem.objects.create(
            profile=profile, inventory_item=item2, quantity=Decimal('1.000'),
        )
        self.assertEqual(profile.items.count(), 2)

    def test_rule_profile_relationship(self):
        profile = _approved_profile()
        product = _product()
        rule = ProductPackagingRule.objects.create(
            product=product, size='none', order_type='takeout',
            profile=profile, no_packaging=False,
        )
        # The rule points to the profile.
        self.assertEqual(rule.profile_id, profile.pk)
        # Reverse: the profile can list its rules.
        self.assertEqual(profile.rules.count(), 1)
        self.assertEqual(profile.rules.first().pk, rule.pk)

    def test_product_can_have_two_rules_one_per_order_type(self):
        profile = _approved_profile()
        product = _product()
        r_dine = ProductPackagingRule.objects.create(
            product=product, size='none', order_type='dine_in',
            profile=None, no_packaging=True,
        )
        r_take = ProductPackagingRule.objects.create(
            product=product, size='none', order_type='takeout',
            profile=profile, no_packaging=False,
        )
        rules = ProductPackagingRule.objects.filter(
            product=product, size='none',
        ).order_by('order_type')
        self.assertEqual(rules.count(), 2)
        dine_rule = rules.get(order_type='dine_in')
        take_rule = rules.get(order_type='takeout')
        self.assertTrue(dine_rule.no_packaging)
        self.assertEqual(take_rule.profile_id, profile.pk)
