"""
Sample Data Seeder for Kape De Manubag System
Run: python manage.py shell < sample_data/seed_data.py
Or use the management command: python manage.py seed_data
"""

import os
import sys
import django

# Setup Django
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'kape_de_manubag.settings')
django.setup()

from apps.accounts.models import CustomUser
from apps.menu.models import Category, Product

print("🌱 Seeding database...")

# ── Create Admin User ──
if not CustomUser.objects.filter(username='admin').exists():
    admin = CustomUser.objects.create_superuser(
        username='admin',
        email='admin@kapedemanubag.com',
        password='admin123',
        first_name='Admin',
        last_name='KDM',
        role='admin'
    )
    print("✅ Admin user created: admin / admin123")
else:
    print("ℹ️  Admin user already exists")

# ── Create Cashier ──
if not CustomUser.objects.filter(username='cashier1').exists():
    cashier = CustomUser.objects.create_user(
        username='cashier1',
        email='cashier@kapedemanubag.com',
        password='cashier123',
        first_name='Maria',
        last_name='Santos',
        role='cashier'
    )
    print("✅ Cashier created: cashier1 / cashier123")

# ── Categories ──
categories_data = [
    {'name': 'Coffee', 'icon': '☕', 'order': 1},
    {'name': 'Milk Tea', 'icon': '🧋', 'order': 2},
    {'name': 'Non-Coffee Drinks', 'icon': '🥤', 'order': 3},
    {'name': 'Combo Meals', 'icon': '🍽️', 'order': 4},
    {'name': 'Pastil Meals', 'icon': '🍱', 'order': 5},
    {'name': 'Burgers', 'icon': '🍔', 'order': 6},
    {'name': 'Snacks', 'icon': '🍟', 'order': 7},
    {'name': 'Appetizers', 'icon': '🥗', 'order': 8},
    {'name': 'Ala Carte', 'icon': '🍝', 'order': 9},
]

cats = {}
for cd in categories_data:
    cat, created = Category.objects.get_or_create(name=cd['name'], defaults=cd)
    cats[cd['name']] = cat
    if created:
        print(f"✅ Category: {cd['icon']} {cd['name']}")

# Packaging category flags
PACKAGING_REQUIRED = ['Combo Meals', 'Pastil Meals', 'Burgers', 'Snacks', 'Appetizers', 'Ala Carte']
for name in PACKAGING_REQUIRED:
    Category.objects.filter(name=name).update(is_packaging_required=True)
    print(f"  📦 Packaging required: {name}")

NO_PACKAGING = ['Coffee', 'Milk Tea', 'Non-Coffee Drinks']
for name in NO_PACKAGING:
    Category.objects.filter(name=name).update(is_packaging_required=False)
    print(f"  🥤 No packaging: {name}")

# System settings seed
from apps.dashboard.models import SystemSetting
SystemSetting.objects.get_or_create(
    key='PACKAGING_FEE_PER_ITEM',
    defaults={
        'value': '6.00',
        'description': (
            'Packaging fee in PHP per eligible meal item '
            'for Take-Out orders.'
        )
    }
)
print("✅ SystemSetting: PACKAGING_FEE_PER_ITEM = 6.00")

# ── Products from Menu Images ──
products_data = [
    # Coffee
    {'category': 'Coffee', 'name': 'Salted Caramel', 'price': 69, 'price_medium': 69, 'price_large': 89, 'price_hot': 69, 'has_sizes': True, 'stock_quantity': 100},
    {'category': 'Coffee', 'name': 'Mocha', 'price': 69, 'price_medium': 69, 'price_large': 89, 'price_hot': 69, 'has_sizes': True, 'stock_quantity': 100},
    {'category': 'Coffee', 'name': 'White Mocha', 'price': 69, 'price_medium': 69, 'price_large': 89, 'price_hot': 69, 'has_sizes': True, 'stock_quantity': 100},
    {'category': 'Coffee', 'name': 'Strawberry Coffee', 'price': 69, 'price_medium': 69, 'price_large': 89, 'price_hot': 69, 'has_sizes': True, 'stock_quantity': 100},
    {'category': 'Coffee', 'name': 'Spanish Latte', 'price': 69, 'price_medium': 69, 'price_large': 89, 'price_hot': 69, 'has_sizes': True, 'stock_quantity': 100},

    # Milk Tea
    {'category': 'Milk Tea', 'name': 'Dark Chocolate', 'price': 59, 'price_medium': 69, 'price_large': 89, 'has_sizes': True, 'stock_quantity': 80},
    {'category': 'Milk Tea', 'name': 'Chocolate Milk Tea', 'price': 59, 'price_medium': 69, 'price_large': 89, 'has_sizes': True, 'stock_quantity': 80},
    {'category': 'Milk Tea', 'name': 'Cookies & Cream', 'price': 59, 'price_medium': 69, 'price_large': 89, 'has_sizes': True, 'stock_quantity': 80},
    {'category': 'Milk Tea', 'name': 'Red Velvet', 'price': 59, 'price_medium': 69, 'price_large': 89, 'has_sizes': True, 'stock_quantity': 80},
    {'category': 'Milk Tea', 'name': 'Wintermelon', 'price': 69, 'price_medium': 79, 'price_large': 99, 'has_sizes': True, 'stock_quantity': 80},
    {'category': 'Milk Tea', 'name': 'Okinawa', 'price': 69, 'price_medium': 79, 'price_large': 99, 'has_sizes': True, 'stock_quantity': 80},

    # Non-Coffee
    {'category': 'Non-Coffee Drinks', 'name': 'Strawberry Matcha', 'price': 79, 'price_large': 99, 'has_sizes': True, 'stock_quantity': 60},
    {'category': 'Non-Coffee Drinks', 'name': 'Matcha Latte', 'price': 69, 'price_large': 89, 'has_sizes': True, 'stock_quantity': 60},
    {'category': 'Non-Coffee Drinks', 'name': 'Strawberry Milk', 'price': 79, 'price_large': 99, 'has_sizes': True, 'stock_quantity': 60},
    {'category': 'Non-Coffee Drinks', 'name': 'Blueberry Soda', 'price': 29, 'price_medium': 39, 'price_large': 49, 'has_sizes': True, 'stock_quantity': 60},
    {'category': 'Non-Coffee Drinks', 'name': 'Green Apple Soda', 'price': 29, 'price_medium': 39, 'price_large': 49, 'has_sizes': True, 'stock_quantity': 60},
    {'category': 'Non-Coffee Drinks', 'name': 'Strawberry Soda', 'price': 29, 'price_medium': 39, 'price_large': 49, 'has_sizes': True, 'stock_quantity': 60},

    # Combo Meals
    {'category': 'Combo Meals', 'name': 'Combo 1 - Burger & Fries', 'price': 60, 'description': 'Pork burger with french fries + free ice tea (dine-in)', 'stock_quantity': 50},
    {'category': 'Combo Meals', 'name': 'Combo 2 - Spaghetti & Chicken', 'price': 95, 'description': 'Spaghetti with chicken + free ice tea (dine-in)', 'stock_quantity': 50},
    {'category': 'Combo Meals', 'name': 'Combo 3 - Spaghetti & Fries', 'price': 95, 'description': 'Spaghetti with french fries + free ice tea (dine-in)', 'stock_quantity': 50},
    {'category': 'Combo Meals', 'name': 'Combo 4 - Spaghetti w/ Bread & Chicken', 'price': 99, 'description': 'Spaghetti with bread and chicken + free ice tea (dine-in)', 'stock_quantity': 50},
    {'category': 'Combo Meals', 'name': 'Combo 5 - Spaghetti w/ Bread & Fries', 'price': 99, 'description': 'Spaghetti with bread and french fries + free ice tea (dine-in)', 'stock_quantity': 50},
    {'category': 'Combo Meals', 'name': 'Combo 6 - Spaghetti & Lumpia', 'price': 95, 'description': 'Spaghetti with lumpia + free ice tea (dine-in)', 'stock_quantity': 50},
    {'category': 'Combo Meals', 'name': 'Combo 7 - Spaghetti w/ Bread & Lumpia', 'price': 90, 'description': 'Spaghetti with bread and lumpia + free ice tea (dine-in)', 'stock_quantity': 50},

    # Pastil Meals
    {'category': 'Pastil Meals', 'name': 'Chicken Pastil', 'price': 35, 'stock_quantity': 40},
    {'category': 'Pastil Meals', 'name': 'Pork Pastil', 'price': 35, 'stock_quantity': 40},
    {'category': 'Pastil Meals', 'name': 'Tuna Pastil', 'price': 35, 'stock_quantity': 40},
    {'category': 'Pastil Meals', 'name': 'Pastil with Chicken', 'price': 85, 'stock_quantity': 40},
    {'category': 'Pastil Meals', 'name': 'Pastil with Porkchop', 'price': 105, 'stock_quantity': 40},
    {'category': 'Pastil Meals', 'name': 'Lumpia Meal', 'price': 60, 'stock_quantity': 40},
    {'category': 'Pastil Meals', 'name': 'Chicken Meal', 'price': 60, 'stock_quantity': 40},

    # Burgers
    {'category': 'Burgers', 'name': 'Pork Burger', 'price': 30, 'stock_quantity': 50},
    {'category': 'Burgers', 'name': 'Chicken Burger', 'price': 40, 'stock_quantity': 50},
    {'category': 'Burgers', 'name': 'Special Pork Burger', 'price': 50, 'stock_quantity': 50},
    {'category': 'Burgers', 'name': 'Special Chicken Burger', 'price': 60, 'stock_quantity': 50},

    # Appetizers
    {'category': 'Appetizers', 'name': 'Steamed Siomai', 'price': 25, 'stock_quantity': 100},
    {'category': 'Appetizers', 'name': 'Fried Siomai', 'price': 30, 'stock_quantity': 100},
    {'category': 'Appetizers', 'name': 'Squid Roll', 'price': 25, 'stock_quantity': 100},
    {'category': 'Appetizers', 'name': 'Tempura', 'price': 25, 'stock_quantity': 100},
    {'category': 'Appetizers', 'name': 'French Fries', 'price': 30, 'stock_quantity': 100},

    # Ala Carte
    {'category': 'Ala Carte', 'name': 'Spaghetti', 'price': 65, 'stock_quantity': 60},
]

for pd in products_data:
    cat_name = pd.pop('category')
    cat = cats.get(cat_name)
    if cat:
        product, created = Product.objects.get_or_create(
            name=pd['name'],
            defaults={**pd, 'category': cat}
        )
        if created:
            print(f"  ✅ Product: {product.name} — ₱{product.price}")

print("\n🎉 Seeding complete!")
print("\n📋 Login Credentials:")
print("   Admin:   admin / admin123")
print("   Cashier: cashier1 / cashier123")
print("\n🌐 Start server: python manage.py runserver")
print("   Menu:      http://127.0.0.1:8000/")
print("   Dashboard: http://127.0.0.1:8000/dashboard/")
