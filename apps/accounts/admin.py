from django.contrib import admin
from apps.accounts.models import User, Seat, SeatHistory

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('id', 'nama', 'email', 'role', 'aktif', 'failed_login_attempts', 'locked_until')
    search_fields = ('nama', 'email')
    list_filter = ('role', 'aktif')

@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    list_display = ('id', 'kode', 'nama_jabatan', 'user_id_aktif')
    search_fields = ('kode', 'nama_jabatan')

@admin.register(SeatHistory)
class SeatHistoryAdmin(admin.ModelAdmin):
    list_display = ('id', 'seat', 'user', 'mulai_pada', 'selesai_pada', 'oleh_admin')
    list_filter = ('seat',)
