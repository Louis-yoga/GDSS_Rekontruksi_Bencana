"""
Admin Panel L9 Views — Manajemen Akun, Penukaran Plt Kursi, dan Audit Log.

Hanya dapat diakses oleh peran ADMIN (@requires_admin).
Menerapkan AC-11 (Penukaran Plt saat eskalasi SLA atau pejabat definitif berhalangan)
dan Zona 2 (ADMIN tidak boleh menduduki kursi DM).
"""
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_http_methods
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError

from apps.accounts.rbac import requires_admin
from apps.accounts.models import User, Seat, SeatHistory
from apps.audit.models import EventLog


def _get_client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '127.0.0.1')


@require_http_methods(['GET'])
@requires_admin
def admin_panel_view(request):
    """L9 — Manajemen Akun, Penukaran Kursi Plt, Riwayat Kursi, dan Audit Log"""
    seats = Seat.objects.select_related('user_id_aktif').order_by('id')
    dm_users = User.objects.filter(role='DM').order_by('nama')
    all_users = User.objects.all().order_by('role', 'nama')
    seat_history = SeatHistory.objects.select_related('seat', 'user', 'oleh_admin').order_by('-mulai_pada')[:30]
    event_logs = EventLog.objects.select_related('user', 'sesi').order_by('-waktu')[:50]

    return render(request, 'accounts/admin_panel.html', {
        'seats': seats,
        'dm_users': dm_users,
        'all_users': all_users,
        'seat_history': seat_history,
        'event_logs': event_logs,
    })


@require_http_methods(['POST'])
@requires_admin
def assign_plt_seat_view(request, seat_kode):
    """
    Endpoint Tukar Pemegang Kursi / Plt (AC-11):
    POST /admin-panel/seats/<seat_kode>/assign/
    Guard:
    - Akun target aktif
    - Akun target memiliki role DM (ADMIN tidak boleh menduduki kursi DM)
    - Akun target tidak sedang menduduki kursi lain
    - Alasan / nomor surat tugas >= 10 karakter
    - Kata sandi konfirmasi Admin valid
    Efek:
    - Putus sesi login pejabat lama (active_session_key = None)
    - Tutup SeatHistory lama & catat SeatHistory baru
    - Catat EventLog PLT_SEAT_ASSIGNED
    """
    seat = get_object_or_404(Seat, kode=seat_kode)
    user_id = request.POST.get('user_id')
    alasan = request.POST.get('alasan', '').strip()
    password = request.POST.get('password_konfirmasi', '')

    if not request.user.check_password(password):
        messages.error(request, "Kata sandi konfirmasi Admin tidak valid.")
        return redirect('admin-panel')

    if len(alasan) < 10:
        messages.error(request, "Alasan / Nomor Surat Tugas Plt wajib diisi minimal 10 karakter.")
        return redirect('admin-panel')

    if not user_id:
        messages.error(request, "Pejabat pengganti (Plt) wajib dipilih.")
        return redirect('admin-panel')

    target_user = get_object_or_404(User, pk=user_id)

    if not target_user.aktif:
        messages.error(request, f"Akun {target_user.email} sedang tidak aktif.")
        return redirect('admin-panel')

    if target_user.role != 'DM':
        messages.error(request, "Administrator teknis dilarang menduduki kursi Decision Maker (Zona 2).")
        return redirect('admin-panel')

    other_seat = Seat.objects.filter(user_id_aktif=target_user).exclude(kode=seat_kode).first()
    if other_seat:
        messages.error(
            request,
            f"Pengguna {target_user.nama} saat ini sudah menduduki kursi {other_seat.kode}. "
            f"Satu pengguna tidak boleh memegang dua kursi sekaligus."
        )
        return redirect('admin-panel')

    with transaction.atomic():
        now = timezone.now()
        old_user = seat.user_id_aktif

        # Putus sesi pejabat lama agar tidak bisa mengakses lagi
        if old_user and old_user.id != target_user.id:
            old_user.active_session_key = None
            old_user.save(update_fields=['active_session_key'])

        # Tutup riwayat kursi sebelumnya
        SeatHistory.objects.filter(seat=seat, selesai_pada__isnull=True).update(selesai_pada=now)

        # Tetapkan pemegang kursi baru
        seat.user_id_aktif = target_user
        seat.save(update_fields=['user_id_aktif'])

        # Catat riwayat baru
        SeatHistory.objects.create(
            seat=seat,
            user=target_user,
            mulai_pada=now,
            oleh_admin=request.user,
            alasan=alasan,
        )

        # Catat EventLog
        EventLog.objects.create(
            sesi=None,
            user=request.user,
            seat_kode=None,
            aksi='PLT_SEAT_ASSIGNED',
            ip_address=_get_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:255],
            detail={
                'seat_kode': seat.kode,
                'old_user_email': old_user.email if old_user else None,
                'new_user_email': target_user.email,
                'alasan': alasan,
            }
        )

    messages.success(request, f"Kursi {seat.kode} berhasil dialihkan kepada {target_user.nama}.")
    return redirect('admin-panel')
