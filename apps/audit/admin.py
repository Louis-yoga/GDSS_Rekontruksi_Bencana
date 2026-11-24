from django.contrib import admin
from apps.audit.models import EventLog

@admin.register(EventLog)
class EventLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'waktu', 'user', 'seat_kode', 'aksi', 'status_dari', 'status_ke', 'ip_address')
    list_filter = ('aksi', 'seat_kode')
    search_fields = ('aksi', 'ip_address', 'seat_kode')
    readonly_fields = [f.name for f in EventLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
