"""
Custom User Model for Kape De Manubag System
Supports Admin, Cashier, and Customer roles
"""
from django.contrib.auth.models import AbstractUser
from django.db import models


class CustomUser(AbstractUser):
    """Extended user model with role-based access"""

    ROLE_CHOICES = [
        ('admin', 'Admin'),
        ('cashier', 'Cashier'),
        ('customer', 'Customer'),
    ]

    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='customer')
    phone = models.CharField(max_length=15, blank=True, null=True)
    profile_image = models.ImageField(upload_to='profiles/', blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"

    @property
    def is_admin_user(self):
        return self.role == 'admin' or self.is_superuser

    @property
    def is_cashier(self):
        return self.role == 'cashier'

    @property
    def is_customer(self):
        return self.role == 'customer'

    class Meta:
        verbose_name = 'User'
        verbose_name_plural = 'Users'
