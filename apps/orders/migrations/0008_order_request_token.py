from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0006_convert_card_payments_to_cash'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='request_token',
            field=models.CharField(
                blank=True,
                help_text=(
                    'Client-generated idempotency key: replaying a request '
                    'with the same key returns the original order instead of '
                    'creating a duplicate.'
                ),
                max_length=64,
                null=True,
                unique=True,
            ),
        ),
    ]
