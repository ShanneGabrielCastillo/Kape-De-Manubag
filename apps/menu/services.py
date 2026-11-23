"""
Menu services — product lifecycle safeguards.

These helpers answer "is this product tied to a live workflow right now?"
without changing how orders behave. An order counts as *active* while it is
still being fulfilled (awaiting payment, preparing, or ready); completed and
cancelled orders are historical records and never block or delay any product
action.

``OrderItem`` is imported lazily because ``apps.orders`` already imports
``apps.menu`` (a module-level import here would be circular).
"""

import dataclasses
import typing

from django.db import transaction
from django.db.models import Count, Q

# Order statuses in which the product is still part of a live workflow.
# Completed/cancelled orders are history and are always excluded.
ACTIVE_ORDER_STATUSES = ('awaiting_payment', 'preparing', 'ready')


def annotate_order_reference_counts(queryset):
    """Annotate a ``Product`` queryset with two reference counters:

    * ``active_order_count`` — distinct orders that are still being
      fulfilled (awaiting_payment/preparing/ready) and reference the product;
    * ``total_order_count``  — distinct orders, active or historical,
      that reference the product.

    Both count distinct *orders* (not line items), so the numbers read as
    "currently in N active orders" / "appears in M orders overall".
    """
    return queryset.annotate(
        active_order_count=Count(
            'orderitem__order',
            filter=Q(orderitem__order__status__in=ACTIVE_ORDER_STATUSES),
            distinct=True,
        ),
        total_order_count=Count('orderitem__order', distinct=True),
    )


def get_order_reference_counts(product):
    """Return ``{'active': n, 'total': m}`` for a single product instance.

    One aggregate query; never touches the order rows themselves.
    """
    from apps.menu.models import Product

    row = (
        annotate_order_reference_counts(
            Product.objects.filter(pk=product.pk),
        )
        .values('active_order_count', 'total_order_count')
        .first()
    ) or {}
    return {
        'active': row.get('active_order_count', 0),
        'total': row.get('total_order_count', 0),
    }


def category_active_order_count(category):
    """Distinct active orders referencing any product in ``category``."""
    from apps.orders.models import Order

    return Order.objects.filter(
        status__in=ACTIVE_ORDER_STATUSES,
        items__product__category=category,
    ).distinct().count()


# ═════════════════════════════════════════════════════════════════════════════
# Phase 1 — readiness-gated inventory cutover
# ═════════════════════════════════════════════════════════════════════════════
#
# ``inventory_tracking_mode`` decides which ledger a product sells from. The
# cutover service below is the only code path allowed to change it (the product
# forms and admin exclude the field).
#
# Design rules:
#
# * **Readiness first.** A product is eligible only when every configured size
#   has exactly one applicable approved recipe and both packaging rules, all
#   referenced items are active and correctly typed, ``resolve_plan()`` succeeds
#   for every size/order type, and at least one unit can be produced from
#   currently available stock.
# * **No open work.** A product with an open order (awaiting_payment,
#   preparing, or ready) or an outstanding reservation line is never eligible.
# * **Locking.** Cutover locks the Product row(s) first (ascending pk), then
#   every InventoryItem referenced by the resolved plans in ascending pk order,
#   and re-runs the full readiness assessment under those locks.
# * **One audit row per product.** ``log_action(..., strict=True)`` is part of
#   the same transaction. If it fails, the mode change rolls back.
# * **Explicit scope.** Only the product ids passed by the caller are changed;
#   nothing is inferred or converted in bulk.
#
# The read-only assessment functions never write, lock, or audit anything and
# are safe to call from a dry-run command or a shell.

CUTOVER_AUDIT_ACTION = 'product.inventory_mode_change'
CUTOVER_TARGET_INGREDIENT = 'ingredient'
CUTOVER_ORDER_TYPES = ('dine_in', 'takeout')


class TrackingModeChangeError(ValueError):
    """A requested tracking-mode change was rejected before any write."""


@dataclasses.dataclass(frozen=True)
class ReadinessCheck:
    """One named, pass/fail readiness condition with a human explanation."""

    key: str
    label: str
    passed: bool
    detail: str = ''


@dataclasses.dataclass(frozen=True)
class SizeAvailability:
    """Resolved availability for one configured (size, order type) cell."""

    size: str
    order_type: str
    available_units: int
    configuration_complete: bool
    reason: str = ''
    recipe_id: typing.Optional[int] = None
    recipe_revision: typing.Optional[int] = None
    packaging_profile_id: typing.Optional[int] = None
    packaging_profile_name: typing.Optional[str] = None


@dataclasses.dataclass(frozen=True)
class CutoverReadiness:
    """Read-only readiness report for one product."""

    product_id: int
    product_name: str
    current_mode: str
    is_active: bool
    eligible: bool
    checks: typing.Tuple[ReadinessCheck, ...]
    reasons: typing.Tuple[str, ...]
    size_availability: typing.Tuple[SizeAvailability, ...]
    missing_or_inactive_items: typing.Tuple[str, ...]
    open_orders: typing.Tuple[str, ...]
    outstanding_reservations: typing.Tuple[str, ...]

    def as_dict(self):
        """JSON-friendly representation (used by dry-run reports and tests)."""
        return {
            'product_id': self.product_id,
            'product_name': self.product_name,
            'current_mode': self.current_mode,
            'is_active': self.is_active,
            'eligible': self.eligible,
            'checks': [dataclasses.asdict(check) for check in self.checks],
            'reasons': list(self.reasons),
            'size_availability': [
                dataclasses.asdict(slot) for slot in self.size_availability
            ],
            'missing_or_inactive_items': list(self.missing_or_inactive_items),
            'open_orders': list(self.open_orders),
            'outstanding_reservations': list(self.outstanding_reservations),
        }


@dataclasses.dataclass(frozen=True)
class TrackingModeChange:
    """One product's mode switch, with the audit row that recorded it."""

    product_id: int
    product_name: str
    old_mode: str
    new_mode: str
    audit_log_id: int


@dataclasses.dataclass(frozen=True)
class TrackingModeChangeResult:
    """Structured outcome of a committed cutover."""

    target_mode: str
    changes: typing.Tuple[TrackingModeChange, ...]

    @property
    def product_ids(self):
        return tuple(change.product_id for change in self.changes)

    def old_modes(self):
        return {change.product_id: change.old_mode for change in self.changes}


def _normalise_product_ids(product_ids):
    """Validate and sort an explicit iterable of product primary keys.

    There is deliberately no allowlist: safety comes from the readiness gates,
    not from a hardcoded set of ids. Callers must still pass an explicit,
    non-empty list; this function never discovers products by itself.
    """
    if isinstance(product_ids, (str, bytes)) or not isinstance(
            product_ids, typing.Iterable):
        raise TrackingModeChangeError(
            'product_ids must be an iterable of integers.')

    ids = list(product_ids)
    if not ids:
        raise TrackingModeChangeError('No product ids were supplied.')

    for product_id in ids:
        if not isinstance(product_id, int) or isinstance(product_id, bool):
            raise TrackingModeChangeError(
                f'Product ids must be integers, got {product_id!r}.')

    duplicates = sorted({pid for pid in ids if ids.count(pid) > 1})
    if duplicates:
        raise TrackingModeChangeError(
            f'Duplicate product ids are not allowed: {duplicates}.')

    return sorted(ids)


def _configured_sizes(product):
    """Return the product's configured sizes from the menu's source of truth."""
    from apps.menu.views import configured_sizes_for_product

    return configured_sizes_for_product(product)


def _add_check(checks, key, label, passed, detail=''):
    checks.append(ReadinessCheck(
        key=key, label=label, passed=bool(passed), detail=detail,
    ))


def assess_cutover_readiness(product):
    """Assess whether one product can safely switch to ingredient tracking.

    Read-only: no locks, writes, audit rows, stock changes, or reservations.
    Accepts a ``Product`` instance or a primary key.

    Returns a :class:`CutoverReadiness`; ``eligible`` is true only when every
    check passes. Blocked reports carry a reason for every failed check and a
    per-size/order-type availability table.
    """
    from apps.inventory.models import (
        PackagingProfile,
        PackagingProfileItem,
        ProductPackagingRule,
        Recipe,
    )
    from apps.inventory.services import RecipeIncompleteError, resolve_plan
    from apps.orders.models import (
        OrderIngredientReservation,
        OrderIngredientReservationItem,
        OrderItem,
    )
    from .models import Product

    if not isinstance(product, Product):
        product = Product.objects.get(pk=product)

    sizes = list(_configured_sizes(product))
    checks = []
    missing_items = set()
    slot_reports = []

    # 1. Exists and is active.
    _add_check(
        checks, 'product_active', 'Product is active', product.is_active,
        '' if product.is_active else 'Product is inactive; reactivate it first.',
    )

    # 2. Not already in the target mode.
    already_ingredient = (
        product.inventory_tracking_mode == Product.INVENTORY_TRACKING_INGREDIENT
    )
    _add_check(
        checks, 'not_already_ingredient',
        'Product is not already ingredient-tracked', not already_ingredient,
        ('Product is already ingredient-tracked.'
         if already_ingredient else ''),
    )

    # 3. Exactly one applicable approved recipe per (size, order type).
    recipe_slot_issues = []
    for size in sizes:
        for order_type in CUTOVER_ORDER_TYPES:
            matching = list(
                Recipe.objects
                .filter(
                    product=product,
                    size=size,
                    status=Recipe.STATUS_APPROVED,
                    order_type_scope__in=[Recipe.SCOPE_ALL, order_type],
                )
                .order_by('pk')
                .values_list('pk', flat=True)
            )
            if len(matching) == 0:
                recipe_slot_issues.append(
                    f'{size}/{order_type}: no approved recipe')
            elif len(matching) > 1:
                recipe_slot_issues.append(
                    f'{size}/{order_type}: {len(matching)} matching approved '
                    f'recipes (ids {matching})')
    _add_check(
        checks, 'approved_recipes',
        'Exactly one applicable approved recipe per size/order type',
        not recipe_slot_issues,
        ('All configured size/order-type slots resolve to one approved recipe.'
         if not recipe_slot_issues else '; '.join(recipe_slot_issues)),
    )

    # 4. Every approved recipe has at least one valid, active ingredient line.
    line_issues = []
    approved_recipes = list(
        Recipe.objects
        .filter(product=product, status=Recipe.STATUS_APPROVED)
        .prefetch_related('items__inventory_item')
        .order_by('pk')
    )
    for recipe in approved_recipes:
        items = list(recipe.items.all())
        if not items:
            line_issues.append(f'recipe {recipe.pk} has no ingredient lines')
            continue
        for item in items:
            inventory_item = item.inventory_item
            if not inventory_item.is_active:
                line_issues.append(
                    f'recipe {recipe.pk}: ingredient '
                    f'{inventory_item.name!r} (id={inventory_item.pk}) is inactive')
                missing_items.add(
                    f'Ingredient {inventory_item.name!r} '
                    f'(id={inventory_item.pk}) is inactive')
            if inventory_item.item_type != 'ingredient':
                line_issues.append(
                    f'recipe {recipe.pk}: item {inventory_item.name!r} '
                    f'(id={inventory_item.pk}) has item_type='
                    f'{inventory_item.item_type!r}, expected ingredient')
                missing_items.add(
                    f'Item {inventory_item.name!r} (id={inventory_item.pk}) '
                    f'is not an ingredient')
            if item.quantity <= 0:
                line_issues.append(
                    f'recipe {recipe.pk}: ingredient '
                    f'{inventory_item.name!r} has quantity {item.quantity}')
    _add_check(
        checks, 'recipe_lines',
        'Every approved recipe has valid, active ingredient lines',
        not line_issues,
        ('All approved recipe lines are active and correctly typed.'
         if not line_issues else '; '.join(line_issues)),
    )

    # 5. Both packaging rules exist for every configured size.
    rules = list(
        ProductPackagingRule.objects
        .filter(product=product)
        .select_related('profile')
        .order_by('pk')
    )
    rule_by_slot = {(rule.size, rule.order_type): rule for rule in rules}
    rule_issues = []
    for size in sizes:
        for order_type in CUTOVER_ORDER_TYPES:
            if (size, order_type) not in rule_by_slot:
                rule_issues.append(
                    f'{size}/{order_type}: no packaging rule')
    _add_check(
        checks, 'packaging_rules',
        'Packaging rules exist for every size (dine-in and takeout)',
        not rule_issues,
        ('Both order types have a packaging rule for every configured size.'
         if not rule_issues else '; '.join(rule_issues)),
    )

    # 6. Every referenced profile is approved with active, typed packaging items.
    profile_issues = []
    for rule in rules:
        if rule.no_packaging:
            continue
        profile = rule.profile
        if profile is None:
            profile_issues.append(
                f'{rule.size}/{rule.order_type}: rule has no profile and is '
                'not marked no_packaging')
            continue
        if profile.status != PackagingProfile.STATUS_APPROVED:
            profile_issues.append(
                f'{rule.size}/{rule.order_type}: profile '
                f'{profile.name!r} (id={profile.pk}) is {profile.status}, '
                'not approved')
            continue
        profile_items = list(
            PackagingProfileItem.objects
            .filter(profile=profile)
            .select_related('inventory_item')
            .order_by('pk')
        )
        if not profile_items:
            profile_issues.append(
                f'{rule.size}/{rule.order_type}: profile {profile.name!r} '
                'has no packaging items')
            continue
        for profile_item in profile_items:
            inventory_item = profile_item.inventory_item
            if not inventory_item.is_active:
                profile_issues.append(
                    f'profile {profile.name!r}: packaging item '
                    f'{inventory_item.name!r} (id={inventory_item.pk}) is inactive')
                missing_items.add(
                    f'Packaging item {inventory_item.name!r} '
                    f'(id={inventory_item.pk}) is inactive')
            if inventory_item.item_type != 'packaging':
                profile_issues.append(
                    f'profile {profile.name!r}: item '
                    f'{inventory_item.name!r} (id={inventory_item.pk}) has '
                    f'item_type={inventory_item.item_type!r}, expected packaging')
                missing_items.add(
                    f'Item {inventory_item.name!r} (id={inventory_item.pk}) '
                    f'is not packaging')
            if profile_item.quantity <= 0:
                profile_issues.append(
                    f'profile {profile.name!r}: item '
                    f'{inventory_item.name!r} has quantity '
                    f'{profile_item.quantity}')
    _add_check(
        checks, 'packaging_profiles',
        'Referenced packaging profiles are approved with valid active items',
        not profile_issues,
        ('All referenced profiles and packaging items are valid.'
         if not profile_issues else '; '.join(profile_issues)),
    )

    # 7. resolve_plan() must succeed for every configured size/order type.
    plans = {}
    plan_issues = []
    for size in sizes:
        for order_type in CUTOVER_ORDER_TYPES:
            try:
                plans[(size, order_type)] = resolve_plan(
                    product.pk, size, order_type)
            except RecipeIncompleteError as exc:
                plans[(size, order_type)] = None
                plan_issues.append(f'{size}/{order_type}: {exc}')
    _add_check(
        checks, 'configuration_complete',
        'Resolve-plan configuration is complete for every size/order type',
        not plan_issues,
        ('Every configured size/order type resolves to a complete plan.'
         if not plan_issues else '; '.join(plan_issues)),
    )

    # 8. At least one sellable unit from current available stock everywhere.
    stock_issues = []
    minimum_units = None
    for size in sizes:
        for order_type in CUTOVER_ORDER_TYPES:
            plan = plans.get((size, order_type))
            if plan is None:
                slot_reports.append(SizeAvailability(
                    size=size,
                    order_type=order_type,
                    available_units=0,
                    configuration_complete=False,
                    reason='plan could not be resolved',
                ))
                stock_issues.append(f'{size}/{order_type}: unresolved plan')
                continue

            if not plan.components:
                units = 0
                reason = 'resolved plan has no components'
            else:
                units = min(
                    int(component.inventory_item.available_quantity
                        // component.quantity_per_unit)
                    for component in plan.components
                )
                reason = ('' if units > 0
                          else f'only {units} sellable unit(s) available')
            complete = bool(plan.components)
            slot_reports.append(SizeAvailability(
                size=size,
                order_type=order_type,
                available_units=units,
                configuration_complete=complete,
                reason=reason,
                recipe_id=plan.recipe.pk,
                recipe_revision=plan.recipe.revision,
                packaging_profile_id=(
                    plan.packaging_rule.profile_id
                    if not plan.packaging_rule.no_packaging else None
                ),
                packaging_profile_name=(
                    plan.packaging_rule.profile.name
                    if not plan.packaging_rule.no_packaging else None
                ),
            ))
            if units < 1:
                stock_issues.append(f'{size}/{order_type}: {units} unit(s)')
            if minimum_units is None or units < minimum_units:
                minimum_units = units

    _add_check(
        checks, 'opening_stock',
        'Opening stock supports at least one unit for every size/order type',
        not stock_issues and bool(slot_reports),
        ('Every slot can produce at least one unit; minimum '
         f'{minimum_units if minimum_units is not None else 0}.'
         if not stock_issues else
         'Insufficient available stock: ' + '; '.join(stock_issues)),
    )

    # 9. No open orders reference this product.
    open_order_rows = list(
        OrderItem.objects
        .filter(
            product=product,
            order__status__in=ACTIVE_ORDER_STATUSES,
        )
        .select_related('order')
        .order_by('order_id', 'pk')
    )
    open_orders = {}
    for item in open_order_rows:
        order = item.order
        open_orders[order.pk] = (
            f'{order.order_number} ({order.status}'
            f'{", stock deducted" if order.stock_deducted else ""})'
        )
    open_order_labels = tuple(open_orders.values())
    _add_check(
        checks, 'no_open_orders',
        'No open orders reference this product', not open_order_labels,
        ('No awaiting-payment, preparing, or ready order references it.'
         if not open_order_labels else
         'Open orders: ' + '; '.join(open_order_labels)),
    )

    # 10. No outstanding ingredient reservation lines for this product.
    reservation_rows = list(
        OrderIngredientReservationItem.objects
        .filter(
            reservation__status=OrderIngredientReservation.STATUS_RESERVED,
        )
        .filter(Q(order_item__product=product) | Q(source_recipe__product=product))
        .select_related('reservation', 'order_item__order')
        .order_by('reservation_id', 'pk')
    )
    reservations = {}
    for item in reservation_rows:
        reservation = item.reservation
        reservations[reservation.pk] = (
            f'reservation {reservation.pk} for order '
            f'{item.order_item.order.order_number}'
        )
    reservation_labels = tuple(reservations.values())
    _add_check(
        checks, 'no_outstanding_reservations',
        'No outstanding reservation lines reference this product',
        not reservation_labels,
        ('No open reservation lines reference it.'
         if not reservation_labels else
         'Outstanding reservations: ' + '; '.join(reservation_labels)),
    )

    failed = [check for check in checks if not check.passed]
    eligible = not failed
    reasons = tuple(
        f'{check.label}: {check.detail}' for check in failed
        if check.detail
    )

    return CutoverReadiness(
        product_id=product.pk,
        product_name=product.name,
        current_mode=product.inventory_tracking_mode,
        is_active=product.is_active,
        eligible=eligible,
        checks=tuple(checks),
        reasons=reasons,
        size_availability=tuple(slot_reports),
        missing_or_inactive_items=tuple(sorted(missing_items)),
        open_orders=open_order_labels,
        outstanding_reservations=reservation_labels,
    )


def assess_products_for_cutover(product_ids=None):
    """Return read-only readiness reports for explicit ids or every product."""
    from .models import Product

    if product_ids is None:
        products = list(Product.objects.order_by('pk'))
    else:
        ids = _normalise_product_ids(product_ids)
        products = list(Product.objects.filter(pk__in=ids).order_by('pk'))
        found = {product.pk for product in products}
        missing = [pid for pid in ids if pid not in found]
        if missing:
            raise TrackingModeChangeError(
                f'Product ids not found: {missing}.')
    return tuple(assess_cutover_readiness(product) for product in products)


def cutover_readiness_report(product_ids=None):
    """JSON-friendly dry-run report; performs no writes or locks."""
    reports = assess_products_for_cutover(product_ids)
    return {
        'eligible_count': sum(1 for report in reports if report.eligible),
        'blocked_count': sum(1 for report in reports if not report.eligible),
        'products': [report.as_dict() for report in reports],
    }


def _collect_plan_item_ids(product):
    """Collect InventoryItem ids referenced by every configured plan."""
    from apps.inventory.services import RecipeIncompleteError, resolve_plan

    item_ids = set()
    for size in _configured_sizes(product):
        for order_type in CUTOVER_ORDER_TYPES:
            try:
                plan = resolve_plan(product.pk, size, order_type)
            except RecipeIncompleteError:
                continue
            item_ids.update(
                component.inventory_item.pk for component in plan.components
            )
    return sorted(item_ids)


# Maximum producible units at or below which an eligible product is flagged
# as a "few units" low-stock demonstration risk.
FEW_UNITS_THRESHOLD = 5


def cutover_stock_report(product_ids=None, *, few_units_threshold=None):
    """Read-only stock-shortage and readiness-classification report.

    Extends :func:`cutover_readiness_report` by distinguishing — and never
    conflating — five states:

    1. ``technically_eligible``        — every Phase 1 gate passed.
    2. ``blocked_insufficient_stock``  — only the ``opening_stock`` gate failed;
                                         the configuration itself is complete.
    3. ``blocked_configuration``       — at least one non-stock gate failed.
    4. ``already_ingredient``          — the product is past the cutover.
    5. ``inactive``                    — the product is soft-deleted.

    Eligible products additionally carry per-inventory-item stock rows
    (on-hand / reserved / available / quantity-per-unit / limiting slots),
    the maximum producible units, the limiting item, low-stock warnings, and
    a ``few_units`` flag for products that can produce only a small number of
    finished units.

    **Client confirmation is always ``False`` in this report.**  The sample
    recipes and packaging are development fixtures; nothing in the database
    records a client approval, and this function never invents one.
    Technical readiness is not client approval.

    Performs no writes, locks, audit rows, stock changes, or reservations.
    """
    if few_units_threshold is None:
        few_units_threshold = FEW_UNITS_THRESHOLD

    from .models import Product
    from apps.inventory.services import RecipeIncompleteError, resolve_plan

    reports = assess_products_for_cutover(product_ids)
    products_out = []
    summary = {
        'technically_eligible': 0,
        'blocked_insufficient_stock': 0,
        'blocked_configuration': 0,
        'already_ingredient': 0,
        'inactive': 0,
        'client_confirmed': 0,
    }

    for report in reports:
        product = Product.objects.get(pk=report.product_id)
        failed_keys = {c.key for c in report.checks if not c.passed}

        if not report.is_active:
            classification = 'inactive'
        elif report.current_mode == CUTOVER_TARGET_INGREDIENT:
            classification = 'already_ingredient'
        elif failed_keys - {'opening_stock'}:
            classification = 'blocked_configuration'
        elif 'opening_stock' in failed_keys:
            classification = 'blocked_insufficient_stock'
        else:
            classification = 'technically_eligible'
        summary[classification] += 1

        slots = []
        item_rows = {}
        limiting_name = None
        max_units = None
        low_stock_warnings = []
        few_units = False

        for slot in report.size_availability:
            slot_entry = dataclasses.asdict(slot)
            slots.append(slot_entry)
            if not slot.configuration_complete:
                continue
            try:
                plan = resolve_plan(report.product_id, slot.size,
                                    slot.order_type)
            except RecipeIncompleteError:
                slot_entry['limiting_item'] = None
                continue
            if not plan.components:
                slot_entry['limiting_item'] = None
                continue
            per_item = []
            for component in plan.components:
                inv = component.inventory_item
                slot_units = int(inv.available_quantity // component.quantity_per_unit)
                per_item.append(slot_units)
                row = item_rows.setdefault(inv.pk, {
                    'inventory_item_id': inv.pk,
                    'name': inv.name,
                    'base_unit': inv.base_unit,
                    'on_hand_quantity': str(inv.on_hand_quantity),
                    'reserved_quantity': str(inv.reserved_quantity),
                    'available_quantity': str(inv.available_quantity),
                    'is_low_stock': inv.is_low_stock,
                    'quantity_per_unit_by_slot': {},
                })
                row['quantity_per_unit_by_slot'][
                    f'{slot.size}/{slot.order_type}'] = str(component.quantity_per_unit)
            min_units = min(per_item)
            lim_index = per_item.index(min_units)
            limiting = plan.components[lim_index].inventory_item
            slot_entry['limiting_item'] = limiting.name
            if max_units is None or min_units < max_units:
                max_units = min_units
                limiting_name = limiting.name

        if classification == 'technically_eligible':
            if max_units is not None and max_units <= few_units_threshold:
                few_units = True
                low_stock_warnings.append(
                    f'Only {max_units} producible unit(s) across all slots '
                    f'(limiting: {limiting_name}).')
            for row in item_rows.values():
                if row['is_low_stock']:
                    low_stock_warnings.append(
                        f"{row['name']} is at or below its low-stock "
                        'threshold.')

        products_out.append({
            'product_id': report.product_id,
            'product_name': report.product_name,
            'inventory_tracking_mode': report.current_mode,
            'classification': classification,
            'eligible': report.eligible,
            'configured_sizes': list(_configured_sizes(product)),
            'stock_sufficient': classification == 'technically_eligible',
            'client_confirmed': False,
            'client_confirmation_note': (
                'No client approval is on record. Sample recipes and '
                'packaging are development fixtures; technical readiness '
                'is not client approval.'),
            'failed_checks': sorted(failed_keys),
            'max_producible_units': max_units,
            'limiting_inventory_item': limiting_name,
            'few_units': few_units,
            'low_stock_warnings': low_stock_warnings,
            'inventory_items': sorted(
                item_rows.values(),
                key=lambda row: row['inventory_item_id']),
            'size_availability': slots,
            'open_orders': list(report.open_orders),
            'outstanding_reservations': list(report.outstanding_reservations),
        })

    return {
        'report_kind': 'cutover_stock_report',
        'read_only': True,
        'summary': summary,
        'products': products_out,
    }


def _validate_cutover_products(products):
    """Raise if any product fails readiness; return the reports."""
    reports = tuple(assess_cutover_readiness(product) for product in products)
    blocked = [report for report in reports if not report.eligible]
    if blocked:
        details = ' | '.join(
            f'{report.product_name} (id={report.product_id}): '
            + '; '.join(report.reasons or ['not eligible'])
            for report in blocked
        )
        raise TrackingModeChangeError(f'Cutover blocked — {details}')
    return reports


# Phase 7D-2A: ``_validate_rollback_products()`` was removed together with
# the ingredient-to-legacy rollback capability it guarded.  Ingredient
# tracking is the permanent direction for active products; the legacy
# POS/menu/cart compatibility branches that remain are historical-only and
# must never become reachable again through this service.  Historical legacy
# orders are still settled independently through the cancellation
# compatibility services (on_status_changed / legacy_restore_by_disposition).


def change_inventory_tracking_mode(product_ids, target_mode, *, actor=None,
                                   reason=''):
    """Switch explicitly requested products to ``target_mode``, audited.

    Phase 7D-2A: only **legacy → ingredient** cutovers are supported.
    Ingredient-to-legacy rollback is RETIRED: ingredient tracking is the
    permanent direction for active products.  A ``target_mode='legacy'``
    request is rejected before any lock, validation, or database mutation.

    For ``target_mode='ingredient'`` the full readiness assessment runs
    before any write and again after the Product and InventoryItem locks
    are held.

    Args:
        product_ids: explicit iterable of product primary keys. Duplicates,
            non-integers, an empty list, and missing ids are rejected.
        target_mode: only ``'ingredient'`` is accepted; ``'legacy'`` is
            rejected (rollback retired).
        actor: the user recorded on the audit row (``None`` is allowed).
        reason: optional free text appended to the audit detail.

    Returns:
        :class:`TrackingModeChangeResult` describing every switched product.

    Raises:
        TrackingModeChangeError: validation failed (including the retired
            rollback request) — nothing was written.
        DatabaseError: a write or the strict audit row failed; the transaction
            rolled back.
    """
    from apps.audit.services import log_action
    from apps.inventory.services import lock_inventory_items

    from .models import Product

    ids = _normalise_product_ids(product_ids)

    valid_modes = {value for value, _label in Product.INVENTORY_TRACKING_CHOICES}
    if target_mode not in valid_modes:
        raise TrackingModeChangeError(
            f'Invalid tracking mode {target_mode!r}; expected one of '
            f'{sorted(valid_modes)}.')

    # Phase 7D-2A: reject ingredient-to-legacy rollback BEFORE any database
    # mutation — before the transaction, the Product locks, and the readiness
    # validation.  The rejection is therefore guaranteed side-effect-free.
    # Historical legacy orders remain settleable through the cancellation
    # compatibility services; this policy only stops NEW legacy conversions.
    if target_mode == 'legacy':
        raise TrackingModeChangeError(
            'Rollback to legacy inventory tracking is retired. Ingredient '
            'tracking is the permanent direction for active products; '
            'historical legacy orders are still settled through the '
            'cancellation compatibility services.')

    with transaction.atomic():
        # Lock Product rows first, in ascending primary-key order.
        products = list(
            Product.objects.select_for_update().filter(pk__in=ids).order_by('pk'))
        by_id = {product.pk: product for product in products}

        missing = [pid for pid in ids if pid not in by_id]
        if missing:
            raise TrackingModeChangeError(
                f'Product ids not found: {missing}.')

        already = [
            pid for pid in ids
            if by_id[pid].inventory_tracking_mode == target_mode
        ]
        if already:
            raise TrackingModeChangeError(
                f'Product ids {already} are already in {target_mode!r} mode.')

        # Phase 7D-2A: only ingredient cutovers reach this point — every
        # legacy request was rejected above, before the transaction opened.
        ordered = [by_id[pid] for pid in ids]
        # First pass rejects obviously incomplete configurations before
        # taking any InventoryItem locks.
        _validate_cutover_products(ordered)

        item_ids = set()
        for product in ordered:
            item_ids.update(_collect_plan_item_ids(product))

        # Lock level 3 in ascending pk order using the shared helper.
        lock_inventory_items(item_ids)

        # Revalidate everything under the Product + InventoryItem locks.
        _validate_cutover_products(ordered)

        detail_suffix = f' ({reason})' if reason else ''
        changes = []
        for product_id in ids:
            product = by_id[product_id]
            old_mode = product.inventory_tracking_mode

            product.inventory_tracking_mode = target_mode
            # save(update_fields=...) keeps the write minimal and preserves the
            # existing post_save realtime signal used by POS terminals.
            product.save(update_fields=['inventory_tracking_mode', 'updated_at'])

            entry = log_action(
                actor,
                CUTOVER_AUDIT_ACTION,
                product,
                detail=(f'inventory_tracking_mode: {old_mode} -> {target_mode}'
                        f'{detail_suffix}'),
                strict=True,
            )
            changes.append(TrackingModeChange(
                product_id=product.pk,
                product_name=product.name,
                old_mode=old_mode,
                new_mode=target_mode,
                audit_log_id=entry.pk,
            ))

    return TrackingModeChangeResult(target_mode=target_mode,
                                    changes=tuple(changes))
