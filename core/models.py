from django.db import models
from django.contrib.auth.models import User
from django.utils.timezone import now
import uuid


class ServerProfile(models.Model):
    SERVER_CHOICES = [
        ('server1', 'Сервер 1'),
        ('server2', 'Сервер 2'),
    ]
    PROTOCOL_CHOICES = [
        ('vless', 'VLESS'),
        ('amneziawg', 'AmneziaWG'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='profiles')
    server = models.CharField(max_length=10, choices=SERVER_CHOICES)
    protocol = models.CharField(max_length=15, choices=PROTOCOL_CHOICES)
    vpn_email = models.EmailField(unique=True)
    vpn_uuid = models.UUIDField(default=uuid.uuid4, editable=False)
    vpn_inbound_id = models.IntegerField()
    vpn_sub_id = models.CharField(max_length=255, blank=True, null=True)
    subscription_expiry = models.DateTimeField(null=True, blank=True)
    total_gb = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def is_active(self):
        if self.subscription_expiry is None:
            return True
        return self.subscription_expiry > now()

    def days_left(self):
        if self.subscription_expiry is None:
            return -1
        delta = self.subscription_expiry - now()
        return max(0, delta.days)


class AdminSettings(models.Model):
    default_days = models.IntegerField(default=30)
    default_traffic_gb = models.IntegerField(default=0)
    footer_text = models.CharField(max_length=200, default='VPN Service')

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class ServerMetric(models.Model):
    SERVER_CHOICES = [('server1', 'Сервер 1'), ('server2', 'Сервер 2')]
    server = models.CharField(max_length=10, choices=SERVER_CHOICES)
    timestamp = models.DateTimeField(auto_now_add=True)
    cpu = models.FloatField(default=0)
    mem_percent = models.FloatField(default=0)
    net_up = models.BigIntegerField(default=0)
    net_down = models.BigIntegerField(default=0)
    onlines = models.IntegerField(default=0)

    class Meta:
        ordering = ['timestamp']