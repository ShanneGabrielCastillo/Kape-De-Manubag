"""
Data migration: populate Category.category_type from actual category names.

Rules applied (based on inspected live-database categories):

  DRINK  — Coffee, Milk Tea, Non-Coffee Drinks, AuditDrinks, Drinks,
            and any active category whose name contains 'coffee', 'tea',
            'drink', or 'beverage' (case-insensitive).

  FOOD   — Combo Meals, Pastil Meals, Burgers, Snacks, Appetizers,
            Ala Carte, Rice Meals, and any active category whose name
            contains 'meal', 'burger', 'snack', 'pastil', 'rice',
            'appetizer', 'ala carte', 'food'.

  OTHER  — everything else (default); staff can correct via admin.

is_packaging_required is NOT used here — this migration fixes the bug
where Appetizers had is_packaging_required=False yet is clearly food.
"""
from django.db import migrations


# Explicit overrides take priority over the keyword scan.
# Keyed by case-insensitive exact name match.
_EXPLICIT = {
    # ── Drinks ───────────────────────────────────────────────────────────
    'coffee':            'drink',
    'milk tea':          'drink',
    'non-coffee drinks': 'drink',
    'auditdrinks':       'drink',
    'drinks':            'drink',
    'draftcat':          'drink',   # test/draft drink category

    # ── Food / meals ─────────────────────────────────────────────────────
    'combo meals':   'food',
    'pastil meals':  'food',
    'burgers':       'food',
    'snacks':        'food',
    'appetizers':    'food',   # ← THE KEY FIX: Appetizers = food, not drink
    'ala carte':     'food',
    'rice meals':    'food',
    'm3':            'food',   # internal test meal category
}

_DRINK_KEYWORDS = ('coffee', 'tea', 'drink', 'beverage', 'juice',
                   'shake', 'smoothie', 'frappe', 'latte', 'soda')
_FOOD_KEYWORDS  = ('meal', 'burger', 'snack', 'pastil', 'rice',
                   'appetizer', 'ala carte', 'food', 'combo', 'lutong')


def _classify(name: str) -> str:
    lower = name.strip().lower()

    # Explicit mapping wins
    if lower in _EXPLICIT:
        return _EXPLICIT[lower]

    # Keyword scan as fallback
    if any(k in lower for k in _DRINK_KEYWORDS):
        return 'drink'
    if any(k in lower for k in _FOOD_KEYWORDS):
        return 'food'

    return 'other'


def populate_category_type(apps, schema_editor):
    Category = apps.get_model('menu', 'Category')
    for cat in Category.objects.all():
        cat.category_type = _classify(cat.name)
        cat.save(update_fields=['category_type'])


def reverse_category_type(apps, schema_editor):
    """Revert: set all back to the default 'other'."""
    Category = apps.get_model('menu', 'Category')
    Category.objects.all().update(category_type='other')


class Migration(migrations.Migration):

    dependencies = [
        ('menu', '0006_category_type'),
    ]

    operations = [
        migrations.RunPython(
            populate_category_type,
            reverse_code=reverse_category_type,
        ),
    ]
