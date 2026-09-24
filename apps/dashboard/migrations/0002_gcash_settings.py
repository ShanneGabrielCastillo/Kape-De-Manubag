"""
Add GCashSettings singleton model.

Stores the business owner's GCash payment information:
  account_name    — owner's GCash name
  account_number  — owner's GCash mobile number
  qr_image        — QR code image (stored via Cloudinary on Render)
  instructions    — optional extra text for the payment page
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='GCashSettings',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True, primary_key=True,
                    serialize=False, verbose_name='ID',
                )),
                ('account_name', models.CharField(
                    blank=True, default='', max_length=200,
                    help_text='GCash account name (as it appears in GCash).',
                )),
                ('account_number', models.CharField(
                    blank=True, default='', max_length=20,
                    help_text='GCash mobile number (e.g. 09XXXXXXXXX).',
                )),
                ('qr_image', models.ImageField(
                    blank=True, null=True,
                    upload_to='gcash_qr/',
                    help_text='GCash QR code image displayed to customers.',
                )),
                ('instructions', models.TextField(
                    blank=True, default='',
                    help_text='Optional extra instructions shown on the payment page.',
                )),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'GCash Settings',
                'verbose_name_plural': 'GCash Settings',
            },
        ),
    ]
