# Hand-written Phase 5D migration: creates ONLY the MFA tables
# (StaffMFADevice, MFARecoveryCode, MFAVerificationAttempt).
#
# Deliberately not generated with ``makemigrations``: the pre-existing,
# out-of-scope drift on ``CustomUser.role`` and the orders reservation fields
# would otherwise be bundled into this migration (see 0006 for the same
# decision). ``makemigrations --check`` reports no changes for these three
# models, so this file and the models are in sync.
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0006_passwordresetattempt'),
    ]

    operations = [
        migrations.CreateModel(
            name='StaffMFADevice',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True, primary_key=True, serialize=False,
                    verbose_name='ID')),
                ('secret_encrypted', models.TextField()),
                ('confirmed_at', models.DateTimeField(blank=True, null=True)),
                ('last_used_timestep', models.BigIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='mfa_device',
                    to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Staff MFA device',
                'verbose_name_plural': 'Staff MFA devices',
            },
        ),
        migrations.CreateModel(
            name='MFARecoveryCode',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True, primary_key=True, serialize=False,
                    verbose_name='ID')),
                ('code_hash', models.CharField(max_length=128)),
                ('used_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='mfa_recovery_codes',
                    to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'MFA recovery code',
                'verbose_name_plural': 'MFA recovery codes',
            },
        ),
        migrations.CreateModel(
            name='MFAVerificationAttempt',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True, primary_key=True, serialize=False,
                    verbose_name='ID')),
                ('scope', models.CharField(
                    choices=[('user', 'User'), ('ip', 'IP address')],
                    max_length=10)),
                ('value', models.CharField(max_length=64)),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('last_attempt_at', models.DateTimeField()),
            ],
            options={
                'verbose_name': 'MFA verification attempt',
                'verbose_name_plural': 'MFA verification attempts',
                'constraints': [
                    models.UniqueConstraint(
                        fields=['scope', 'value'],
                        name='unique_mfa_verification_scope_value'),
                ],
            },
        ),
    ]
