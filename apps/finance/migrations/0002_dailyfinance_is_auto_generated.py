from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='dailyfinance',
            name='is_auto_generated',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'True if this record was auto-generated because the cashier '
                    'did not save a finance record for this day. '
                    'Expenses, coins, cash advance, and floating cash default to '
                    'zero; gcash_payments is auto-filled from Order data. '
                    'The cashier should review and correct this record.'
                ),
            ),
        ),
    ]
