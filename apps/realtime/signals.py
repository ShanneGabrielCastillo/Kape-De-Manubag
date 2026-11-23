"""
Signal receivers that fire real-time events when Order or Product
data changes. These hook into EXISTING model saves — no changes
to existing views required.

Phase 3 — mode-aware realtime inventory contract
------------------------------------------------
``inventory_changed`` now describes AVAILABILITY, not legacy stock:

    product_id, product_name, inventory_tracking_mode,
    available_units, configuration_complete, sellable,
    is_available, is_active

* Non-ingredient products (Phase 7D-2B): broadcast the GUARD payload —
  ``available_units=0``, ``configuration_complete=False``,
  ``sellable=False``. The legacy finished-goods payload was removed with
  the ingredient-to-legacy rollback (retired Phase 7D-2A); the frozen
  ``Product.stock_quantity`` is never broadcast as an availability
  authority.
* Ingredient products: ``available_units`` is derived from the approved
  plan + ingredient/packaging stock via the shared
  ``get_available_units()`` service (respects reservations); it is the
  MAX across configured sizes, and ``configuration_complete`` is True
  only when EVERY configured size resolves a complete plan — the same
  aggregation the POS grid renders (apps/orders/views.cashier_pos).
* ``stock_quantity`` is deliberately NOT broadcast as an authority.
* Finished-product low-stock alerting is retired: the legacy
  ``inventory_low`` event is no longer published. Ingredient / packaging
  low-stock is signalled by the ``ingredient_low`` event, raised from the
  ingredient movement ledger when an item's available quantity crosses its
  threshold (never from frozen Product fields).

Ingredient availability changes (reservation, consumption, release,
purchase, adjustment, wastage) are published from an
``IngredientMovementLog`` post_save hook through
``transaction.on_commit`` — events fire only after the DB commit
succeeds, mirroring the ``mark_order_ready`` pattern.
"""
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.orders.models import Order
from apps.menu.models import Product
from apps.realtime.broker import publish


@receiver(post_save, sender=Order)
def order_saved(sender, instance, created, **kwargs):
    order = instance
    if created:
        # new_order is NOT published here because total=0 at creation time.
        # Items and totals are added after Order.objects.create() inside the
        # same transaction, so this signal fires too early.  Each view that
        # creates an order (checkout_view, create_pos_order) publishes
        # new_order explicitly once the transaction commits and totals are set.
        pass
    else:
        # Allow callers to suppress the automatic in-transaction broadcast
        # (e.g. mark_order_ready sets order._skip_realtime = True and then
        # registers its own transaction.on_commit callback so the event fires
        # only after the DB commit succeeds).
        if getattr(order, '_skip_realtime', False):
            return
        publish('status_changed', {
            'order_id':           order.pk,
            'order_number':       order.order_number,
            'queue_number':       order.queue_number,
            'new_status':         order.status,
            'new_status_display': order.get_status_display(),
            'is_paid':            order.is_paid,
        })


def _ingredient_availability_aggregate(product):
    """(max_units, all_sizes_complete) over configured sizes.

    Same aggregation the POS grid renders (cashier_pos) and the chatbot
    uses: units per size come from the shared read-only availability
    service, so reservations and packaging stock are respected; a size
    with an incomplete plan contributes 0 and clears the complete flag.
    """
    from apps.inventory.services import get_available_units
    from apps.menu.views import configured_sizes_for_product

    max_units = 0
    complete = True
    for size in configured_sizes_for_product(product):
        result = get_available_units(product, size)
        if not result.configuration_complete:
            complete = False
        if result.units > max_units:
            max_units = result.units
    return max_units, complete


def _inventory_changed_payload(product):
    """Build the mode-aware availability payload for one product."""
    if product.inventory_tracking_mode == Product.INVENTORY_TRACKING_INGREDIENT:
        units, complete = _ingredient_availability_aggregate(product)
        return {
            'product_id':               product.pk,
            'product_name':             product.name,
            'inventory_tracking_mode':  product.inventory_tracking_mode,
            'available_units':          units,
            'configuration_complete':   complete,
            'sellable':                 bool(complete and units > 0),
            'is_available':             product.is_available,
            'is_active':                product.is_active,
        }
    # Phase 7D-2B: non-ingredient products broadcast the GUARD payload —
    # zero units, not sellable, never the frozen legacy stock_quantity.
    # The legacy finished-goods payload was removed together with the
    # ingredient-to-legacy rollback (Phase 7D-2A): no supported workflow
    # can make a legacy product sellable, so broadcasting its stock would
    # be wrong. Consumers (POS stock module) treat this as "unavailable";
    # such products never render on the POS grid.
    return {
        'product_id':               product.pk,
        'product_name':             product.name,
        'inventory_tracking_mode':  product.inventory_tracking_mode,
        'available_units':          0,
        'configuration_complete':   False,
        'sellable':                 False,
        'is_available':             product.is_available,
        'is_active':                product.is_active,
    }


@receiver(post_save, sender=Product)
def product_saved(sender, instance, created, **kwargs):
    if created:
        return
    # reduce_stock()/restore_stock() set stock_quantity to an F() expression
    # in memory before saving, so post_save sees a CombinedExpression instead
    # of an int. Re-read the committed value before building the payload.
    instance.refresh_from_db(fields=[
        'stock_quantity', 'low_stock_threshold', 'is_available', 'is_active',
    ])
    # Live availability snapshot for every open POS terminal: when the
    # last unit of a product is sold (or it is restocked / marked
    # unavailable), the client updates the product card and reconciles
    # the current order without a page reload.  The payload is
    # mode-aware (see module docstring); frozen legacy stock is never
    # broadcast as an ingredient product's availability.
    publish('inventory_changed', _inventory_changed_payload(instance))

    # Finished-product low-stock alerting is retired (Phase 7C-2). Ingredient
    # products are monitored from the ingredient movement ledger
    # (ingredient_low below), never from the frozen Product fields.


def _ingredient_products_using(item_id):
    """Ingredient-tracked products whose approved plan uses this item."""
    from apps.inventory.models import RecipeItem, ProductPackagingRule

    product_ids = set(
        RecipeItem.objects
        .filter(inventory_item_id=item_id,
                recipe__status='approved',
                recipe__product__inventory_tracking_mode=(
                    Product.INVENTORY_TRACKING_INGREDIENT))
        .values_list('recipe__product_id', flat=True)
    )
    product_ids.update(
        ProductPackagingRule.objects
        .filter(profile__items__inventory_item_id=item_id,
                product__inventory_tracking_mode=(
                    Product.INVENTORY_TRACKING_INGREDIENT))
        .values_list('product_id', flat=True)
    )
    return sorted(product_ids)


@receiver(post_save, sender='inventory.IngredientMovementLog')
def ingredient_movement_logged(sender, instance, created, **kwargs):
    """Publish availability updates after ingredient ledger writes.

    Every reservation / consumption / release / purchase / adjustment /
    wastage row lands here inside the writer's transaction.  The
    broadcasts are registered on ``transaction.on_commit`` so they fire
    only if that transaction commits — a rollback publishes nothing.

    One movement can affect several products (shared ingredient); each
    affected ingredient-tracked product gets a fresh mode-aware
    ``inventory_changed`` payload.  Publishing never saves models, so
    there is no recursion.  Legacy products are untouched.
    """
    if not created:
        return  # the ledger is append-only; updates are not expected

    item_id = instance.inventory_item_id

    def _broadcast():
        # Threshold-crossing low-stock event for the item itself,
        # computed from the movement's before/after snapshots.
        from apps.inventory.models import InventoryItem
        item = InventoryItem.objects.filter(pk=item_id).first()
        if item is not None:
            before = instance.on_hand_before - instance.reserved_before
            after = instance.on_hand_after - instance.reserved_after
            threshold = item.low_stock_threshold
            if before > threshold and after <= threshold:
                publish('ingredient_low', {
                    'inventory_item_id':   item.pk,
                    'item_name':           item.name,
                    'item_type':           item.item_type,
                    'base_unit':           item.base_unit,
                    'available_quantity':  str(after),
                    'low_stock_threshold': str(threshold),
                })

        for product_id in _ingredient_products_using(item_id):
            product = Product.objects.filter(pk=product_id).first()
            if product is None:
                continue
            publish('inventory_changed', _inventory_changed_payload(product))

    transaction.on_commit(_broadcast)
