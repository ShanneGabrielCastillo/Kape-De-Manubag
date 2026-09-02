"""
Menu Models - Categories and Products for Kape De Manubag
"""
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Prefetch, Q
from django.db.models.functions import Trim
from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver
from django.utils import timezone
from django.utils.text import slugify


# Shared by every low-stock surface (dashboard, inventory, product
# management, POS): stock at or below this many units is "critically low".
CRITICAL_STOCK_THRESHOLD = 5


def _name_is_unchanged(instance):
    """True when this save keeps the instance's current name (compared
    case-insensitively after trimming).

    Shared by Category and Product (both normalise names the same way). It
    preserves the editing workflow for legacy duplicates: an instance that
    already shares a name with another can still be edited as long as its
    name is not changed -- only new duplicates and renames are rejected.
    """
    if not instance.pk:
        return False
    old = type(instance).objects.filter(pk=instance.pk).values_list('name', flat=True).first()
    return old is not None and old.strip().lower() == instance.name.lower()


class CategoryQuerySet(models.QuerySet):
    """Common category querysets shared by every selling surface."""

    def active(self):
        """Categories currently shown on selling surfaces (not deactivated)."""
        return self.filter(is_active=True)

    def with_sellable_products(self):
        """Active categories with their sellable products prefetched.

        One query for the categories and one for the products (via
        ``Prefetch``), so rendering every category's product grid (the
        customer menu and the POS) never triggers a query per category.
        The prefetch reuses ``Product.objects.sellable()`` so "sellable"
        means exactly the same thing on every surface.
        """
        return self.active().prefetch_related(
            Prefetch('products', queryset=Product.objects.sellable()),
        )


CategoryManager = models.Manager.from_queryset(CategoryQuerySet)


class Category(models.Model):
    """Food/drink categories"""
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True, blank=True)
    icon = models.CharField(max_length=10, default='🍽️')
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to='categories/', blank=True, null=True)
    is_active = models.BooleanField(default=True)
    is_packaging_required = models.BooleanField(
        default=False,
        help_text=(
            "Enable for meal/food categories. Adds packaging fee "
            "per item for Take-Out orders. Disable for beverages."
        )
    )
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = CategoryManager()

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    # ── Name validation ─────────────────────────────────────────────────────
    # Runs for every form-driven save (create/edit views and the admin site,
    # which call full_clean()/clean()); direct ORM calls (seed data, tests)
    # use save() and are intentionally unaffected.

    def clean(self):
        """Trim the name and reject duplicates case-insensitively.

        Category names are normalized (leading/trailing whitespace removed)
        and compared case-insensitively against trimmed stored names, so
        "  drinks  " collides with "Drinks". Editing a legacy category that
        already shares a name with another category stays possible as long as
        the name itself is not changed -- only new duplicates and renames are
        rejected.
        """
        super().clean()
        if not self.name:
            return
        name = self.name.strip()
        if not name:
            raise ValidationError({'name': 'Category name cannot be empty.'})
        self.name = name
        if _name_is_unchanged(self):
            return
        if self._duplicate_name_exists():
            raise ValidationError({
                'name': (
                    f'A category named "{self.name}" already exists. '
                    'Please choose a different name.'
                ),
            })

    def _duplicate_name_exists(self):
        """True when another category uses the same name, ignoring case and
        surrounding whitespace in both the new name and the stored names
        (self is always excluded). Trim() lets whitespace-padded legacy rows
        be detected too, not just freshly typed input.
        """
        return (
            Category.objects.annotate(trimmed_name=Trim('name'))
            .filter(trimmed_name__iexact=self.name)
            .exclude(pk=self.pk)
            .exists()
        )

    def deactivate(self):
        """Soft-deactivate this category (hidden from menu/POS/dropdowns,
        products and history kept, reactivatable at any time)."""
        self.is_active = False
        self.save(update_fields=['is_active'])

    def activate(self):
        """Re-activate a deactivated category."""
        self.is_active = True
        self.save(update_fields=['is_active'])

    def delete(self, *args, **kwargs):
        # Data-integrity guard: a category that still contains products can
        # never be removed, because its products are themselves never
        # hard-deleted (Product.delete soft-deactivates and the pre_delete
        # guard blocks hard deletes). Deleting the category would cascade
        # into those products, so non-empty categories raise instead -- the
        # UI explains why and how to proceed (deactivate the category, or
        # move the products to another category first). Empty categories
        # still delete normally.
        if self.products.exists():
            raise ValidationError(CATEGORY_DELETE_ERROR)
        return super().delete(*args, **kwargs)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name_plural = 'Categories'
        ordering = ['order', 'name']


def _unique_slug(name, exclude_pk=None):
    """Slugify ``name`` and append ``-N`` until it is unique across products.

    The ``-N`` suffix keeps slugs stable when two products would otherwise
    slugify identically (e.g. "Iced Coffee" and "Iced Coffee 2").
    """
    base = slugify(name)
    slug = base
    counter = 1
    while Product.objects.filter(slug=slug).exclude(pk=exclude_pk).exists():
        counter += 1
        slug = f"{base}-{counter}"
    return slug


class ProductQuerySet(models.QuerySet):
    """Common product querysets shared by every selling/reporting surface.

    Keeping the domain filters here (instead of inlining ``is_active`` /
    ``is_available`` / stock thresholds per view) means every consumer -- the
    customer menu, the POS, inventory and dashboard widgets -- uses exactly
    the same definition of "sellable" and "low stock".
    """

    def sellable(self):
        """Products that appear on the customer menu and the POS: active
        (not deactivated) AND marked available."""
        return self.filter(is_active=True, is_available=True)

    def low_stock(self):
        """Products at or below their own restock threshold."""
        return self.filter(stock_quantity__lte=F('low_stock_threshold'))


ProductManager = models.Manager.from_queryset(ProductQuerySet)


class Product(models.Model):
    """Menu items/products"""
    SIZE_CHOICES = [
        ('none', 'No Size'),
        ('small', 'Small'),
        ('medium', 'Medium'),
        ('large', 'Large'),
    ]

    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name='products')
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True, blank=True)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to='products/', blank=True, null=True)

    # Pricing - supports size variants
    price = models.DecimalField(max_digits=10, decimal_places=2)  # Base/small price
    price_medium = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    price_large = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    price_hot = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    has_sizes = models.BooleanField(default=False)

    is_available = models.BooleanField(default=True)
    is_active = models.BooleanField(
        default=True,
        help_text=(
            'Active products are sellable. Inactive (deactivated) products '
            'are hidden from the customer menu and the POS but are kept '
            'for historical records and can be reactivated at any time.'
        ),
    )
    deactivated_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When this product was soft-deactivated (is_active=False). '
                  'Null while the product is active.',
    )
    is_featured = models.BooleanField(default=False)
    stock_quantity = models.PositiveIntegerField(default=100)
    low_stock_threshold = models.PositiveIntegerField(default=10)

    objects = ProductManager()

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slug(self.name, exclude_pk=self.pk)
        super().save(*args, **kwargs)

    # ── Name validation ─────────────────────────────────────────────────────
    # Runs for every form-driven save (create/edit views and the admin site,
    # which call full_clean()/clean()); direct ORM calls (seed data, stock
    # operations) use save() and are intentionally unaffected.

    def clean(self):
        """Trim the name and reject duplicates within the same category.

        Product names are normalized (leading/trailing whitespace removed)
        and compared case-insensitively, so " iced coffee  " collides with
        "Iced Coffee". The same name may still be used in different
        categories -- the menu and POS present products grouped by category.
        """
        super().clean()
        if not self.name:
            return
        self.name = self.name.strip()
        if not self.name:
            return
        if _name_is_unchanged(self):
            return
        if self._duplicate_name_in_category():
            raise ValidationError({
                'name': (
                    f'A product named "{self.name}" already exists in the '
                    f'"{self.category}" category. Please choose a different name.'
                ),
            })

    def _duplicate_name_in_category(self):
        """True when another product in this category uses the same name,
        ignoring case (self is always excluded)."""
        if not self.category_id:
            return False
        return Product.objects.filter(
            category_id=self.category_id,
            name__iexact=self.name,
        ).exclude(pk=self.pk).exists()

    def __str__(self):
        return f"{self.name} - ₱{self.price}"

    @property
    def is_low_stock(self):
        """True when stock is at or below this product's own threshold.

        The per-instance counterpart of ``Product.objects.low_stock()``
        (used e.g. by the realtime stock alerts).
        """
        return self.stock_quantity <= self.low_stock_threshold

    @property
    def is_critical_stock(self):
        """True when stock is at or below the shared critical threshold."""
        return self.stock_quantity <= CRITICAL_STOCK_THRESHOLD

    @property
    def stock_status(self):
        """'critical', 'low' or 'ok' for stock-level displays.

        One definition shared by the dashboard, inventory, product
        management and POS templates so every surface agrees on what counts
        as critically low (<= CRITICAL_STOCK_THRESHOLD) vs low (<= this
        product's own low_stock_threshold) vs OK.
        """
        if self.is_critical_stock:
            return 'critical'
        if self.is_low_stock:
            return 'low'
        return 'ok'

    def get_price_for_size(self, size='none'):
        prices = {
            'none': self.price,
            'small': self.price,
            'medium': self.price_medium or self.price,
            'large': self.price_large or self.price,
            'hot': self.price_hot or self.price,
        }
        return prices.get(size, self.price)

    def reduce_stock(self, quantity):
        """Atomically reduce stock. Raises ValueError if insufficient.

        The decrement is a single conditional UPDATE (``stock_quantity >=
        quantity``), so the availability check and the subtraction happen in
        one atomic statement: two simultaneous orders can never both pass a
        stale pre-check and drive stock below zero -- the second one's UPDATE
        simply affects no row and raises instead. The ``updated_at`` pin
        keeps the row's timestamp unchanged, exactly like the previous
        ``save(update_fields=['stock_quantity'])``. ``QuerySet.update()``
        bypasses ``save()``, so the post_save signal (realtime low-stock
        alert) is replayed manually to preserve the old behaviour.
        """
        updated = Product.objects.filter(
            pk=self.pk,
            stock_quantity__gte=quantity,
        ).update(
            stock_quantity=F('stock_quantity') - quantity,
            # QuerySet.update() bumps auto_now fields; pin it to itself so
            # deductions behave exactly like the previous save() call.
            updated_at=F('updated_at'),
        )
        if not updated:
            # Re-read the real value: under a concurrent order the instance
            # may hold a stale snapshot, but the message should always show
            # what is actually available.
            available = Product.objects.filter(pk=self.pk).values_list(
                'stock_quantity', flat=True,
            ).first()
            raise ValueError(
                f"Insufficient stock for '{self.name}'. "
                f"Available: {available}, Requested: {quantity}"
            )
        self.refresh_from_db(fields=['stock_quantity'])
        post_save.send(sender=Product, instance=self, created=False)

    def restore_stock(self, quantity):
        """Atomically restore stock."""
        self.stock_quantity = F('stock_quantity') + quantity
        self.save(update_fields=['stock_quantity'])
        self.refresh_from_db(fields=['stock_quantity'])

    # ── Soft-deactivation workflow ──────────────────────────────────────────
    # Products are deactivated, never deleted, so every historical reference
    # (order items, reports, finance records, analytics) keeps its link to
    # this product and its ID survives untouched.

    def deactivate(self):
        """Soft-deactivate this product (hidden from menu/POS, history kept)."""
        self.is_active = False
        self.deactivated_at = timezone.now()
        self.save(update_fields=['is_active', 'deactivated_at', 'updated_at'])

    def activate(self):
        """Re-activate a deactivated product."""
        self.is_active = True
        self.deactivated_at = None
        self.save(update_fields=['is_active', 'deactivated_at', 'updated_at'])

    def delete(self, *args, **kwargs):
        # Permanent deletion is disabled: products are soft-deactivated instead
        # (is_active=False, deactivated_at set), so order items, inventory
        # logs and cart rows never dangle and the product ID is preserved.
        # Bulk ``QuerySet.delete()`` is blocked by the ``pre_delete`` signal.
        self.deactivate()
        return 0, {}  # nothing was removed from the database

    class Meta:
        ordering = ['category', 'name']
        constraints = [
            # Hard database-level guarantee: stock can never be negative,
            # even if a future code path forgets to check before writing.
            models.CheckConstraint(
                check=Q(stock_quantity__gte=0),
                name='product_stock_non_negative',
            ),
        ]



PRODUCT_SOFT_DELETE_ERROR = (
    'Products cannot be deleted from the database. Deactivate them instead; '
    'their order and report history stays intact.'
)

CATEGORY_DELETE_ERROR = (
    'Categories that still contain products cannot be deleted. Move the '
    'products to another category first -- a category can only be deleted '
    'once it is empty.'
)


@receiver(pre_delete, sender=Category)
def _block_category_delete_with_products(sender, instance, **kwargs):
    """Block *any* delete of a category that still contains products,
    including bulk ``QuerySet.delete()`` (which bypasses
    ``Category.delete()``) and cascades from related models.

    Deleting a non-empty category would cascade into products that are
    themselves never hard-deleted, so every code path -- the view, the admin
    site and direct ORM calls -- must refuse the same way. Empty categories
    pass through untouched.
    """
    if instance.products.exists():
        raise ValidationError(CATEGORY_DELETE_ERROR)


@receiver(pre_delete, sender=Product)
def _block_product_hard_delete(sender, instance, **kwargs):
    """Block *any* hard delete of a product, including bulk
    ``QuerySet.delete()`` (which bypasses ``Product.delete()``) and the
    cascade from deleting a Category. Soft deactivation -- via
    ``deactivate()`` or ``Product.delete()`` -- is the only way to remove a
    product from sale, preserving every historical reference and the product
    ID. Modeled after the CustomUser guard in apps/accounts.

    CAUTION: raising inside ``transaction.atomic()`` marks the connection for
    rollback; callers that catch the exception and keep querying must do so
    inside a savepoint (see the category_delete view, which checks for
    products BEFORE deleting instead of relying on this exception).
    """
    raise ValidationError(PRODUCT_SOFT_DELETE_ERROR)
