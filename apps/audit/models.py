from django.db import models
from django.core.exceptions import PermissionDenied

class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise PermissionDenied("Operasi UPDATE pada event_log dilarang demi integritas audit trail.")

    def delete(self):
        raise PermissionDenied("Operasi DELETE pada event_log dilarang demi integritas audit trail.")

class AppendOnlyManager(models.Manager):
    def get_queryset(self):
        return AppendOnlyQuerySet(self.model, using=self._db)

class EventLog(models.Model):
    id = models.BigAutoField(primary_key=True)
    sesi = models.ForeignKey(
        'decision_sessions.SesiKeputusan',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='logs'
    )
    waktu = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        'accounts.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL
    )
    seat_kode = models.CharField(max_length=20, null=True, blank=True)
    aksi = models.CharField(max_length=60, db_index=True)
    status_dari = models.CharField(max_length=20, null=True, blank=True)
    status_ke = models.CharField(max_length=20, null=True, blank=True)
    ip_address = models.CharField(max_length=45)
    user_agent = models.CharField(max_length=255)
    detail = models.JSONField(default=dict)

    objects = AppendOnlyManager()

    class Meta:
        db_table = 'event_log'
        verbose_name = 'Jejak Audit'
        verbose_name_plural = 'Jejak Audit'
        ordering = ['waktu']

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise PermissionDenied("Pembaruan rekaman log yang sudah tersimpan dilarang.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Penghapusan rekaman audit log dilarang.")

    def __str__(self):
        return f"[{self.waktu}] {self.aksi} oleh {self.seat_kode or self.user or 'System'}"
