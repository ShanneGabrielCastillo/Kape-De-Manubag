# Hand-written Phase 5C migration: creates ONLY the PasswordResetAttempt
# throttle table. (Deliberately not generated via ``makemigrations`` — that
# would bundle the pre-existing accounts model drift that is out of scope
# and has never been reconciled on this branch.)
from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0005_customuser_kitchen_staff_role'),
    ]

    operations = [
        migrations.CreateModel(
            name='PasswordResetAttempt',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True, primary_key=True, serialize=False,
                    verbose_name='ID')),
                ('scope', models.CharField(
                    choices=[('email', 'Email (keyed hash)'),
                             ('ip', 'IP address')],
                    max_length=10)),
                ('value', models.CharField(max_length=64)),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('last_attempt_at', models.DateTimeField()),
            ],
            options={
                'verbose_name': 'Password reset attempt',
                'verbose_name_plural': 'Password reset attempts',
                'constraints': [
                    models.UniqueConstraint(
                        fields=['scope', 'value'],
                        name='unique_password_reset_scope_value'),
                ],
            },
        ),
    ]
