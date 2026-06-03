"""
Menu Models - Categories and Products for Kape De Manubag
"""
from django.db import models
from django.db.models import F
from django.utils.text import slugify


class Category(models.Model):
    """Food/drink categories"""
    ICON_CHOICES = [
        ('☕', 'Coffee'),
        ('🧋', 'Milk Tea'),
        ('🥤', 'Drinks'),
        ('🍽️', 'Meals'),
        ('🍣', 'Pastil'),
        ('🍔', 'Burger'),
        ('🍟', 'Snacks'),
        ('🥗', 'Appetizers'),
        ('🍝', 'Ala Carte'),
    ]

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

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name_plural = 'Categories'
        ordering = ['order', 'name']


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
    is_featured = models.BooleanField(default=False)
    stock_quantity = models.PositiveIntegerField(default=100)
    low_stock_threshold = models.PositiveIntegerField(default=10)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            slug = base_slug
            counter = 1
            while Product.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} - ₱{self.price}"

    @property
    def is_low_stock(self):
        return self.stock_quantity <= self.low_stock_threshold

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
        """Atomically reduce stock. Raises ValueError if insufficient."""
        if self.stock_quantity < quantity:
            raise ValueError(
                f"Insufficient stock for '{self.name}'. "
                f"Available: {self.stock_quantity}, Requested: {quantity}"
            )
        self.stock_quantity = F('stock_quantity') - quantity
        self.save(update_fields=['stock_quantity'])
        self.refresh_from_db(fields=['stock_quantity'])

    def restore_stock(self, quantity):
        """Atomically restore stock."""
        self.stock_quantity = F('stock_quantity') + quantity
        self.save(update_fields=['stock_quantity'])
        self.refresh_from_db(fields=['stock_quantity'])

    class Meta:
        ordering = ['category', 'name']
