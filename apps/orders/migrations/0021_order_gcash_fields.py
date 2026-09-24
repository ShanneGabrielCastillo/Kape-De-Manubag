"""
Add manual GCash payment verification fields to Order.

New fields
──────────
gcash_status        — verification state: none / pending / verified / rejected
gcash_reference     — reference number submitted by the customer
gcash_proof         — optional screenshot ImageField (Cloudinary in prod)
gcash_submitted_at  — timestamp of customer submission
gcash_verified_at   — timestamp of staff verification / rejection
gcash_verified_by   — FK to the staff member who acted on the submission
gcash_notes         — free-text staff notes (e.g. rejection reason)

Safety
──────
All new fields default to null / blank / empty so that every existing Order
row remains valid without any data migration.  Cash orders are completely
unaffected — the new fields simply stay at their defaults.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0020_remove_pending_status'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='gcash_status',
            field=models.CharField(
                choices=[
                    ('none',     'Not Submitted'),
                    ('pending',  'Pending Verification'),
                    ('verified', 'Verified'),
                    ('rejected', 'Rejected'),
                ],
                default='none',
                max_length=20,
                help_text='Verification state of a customer-submitted GCash payment.',
            ),
        ),
        migrations.AddField(
            model_name='order',
            name='gcash_reference',
            field=models.CharField(
                blank=True,
                default='',
                max_length=50,
                help_text='GCash reference number submitted by the customer.',
            ),
        ),
        migrations.AddField(
            model_name='order',
            name='gcash_proof',
            field=models.ImageField(
                blank=True,
                null=True,
                upload_to='gcash_proofs/',
                help_text='Optional payment screenshot uploaded by the customer.',
            ),
        ),
        migrations.AddField(
            model_name='order',
            name='gcash_submitted_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text='Timestamp when the customer submitted GCash payment info.',
            ),
        ),
        migrations.AddField(
            model_name='order',
            name='gcash_verified_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text='Timestamp when staff verified or rejected the payment.',
            ),
        ),
        migrations.AddField(
            model_name='order',
            name='gcash_verified_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='gcash_verifications',
                to=settings.AUTH_USER_MODEL,
                help_text='Staff member who verified or rejected this GCash payment.',
            ),
        ),
        migrations.AddField(
            model_name='order',
            name='gcash_notes',
            field=models.TextField(
                blank=True,
                default='',
                help_text='Staff notes, e.g. rejection reason.',
            ),
        ),
        # Also fix the status field default from 'pending' to 'awaiting_payment'
        migrations.AlterField(
            model_name='order',
            name='status',
            field=models.CharField(
                choices=[
                    ('awaiting_payment', 'Awaiting Payment'),
                    ('preparing',        'Preparing'),
                    ('ready',            'Ready'),
                    ('completed',        'Completed'),
                    ('cancelled',        'Cancelled'),
                ],
                default='awaiting_payment',
                max_length=20,
            ),
        ),
    ]
