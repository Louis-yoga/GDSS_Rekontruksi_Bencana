import pytest
from datetime import timedelta
from django.core.management import call_command
from django.urls import reverse
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User, Seat, SeatHistory
from apps.decision_sessions.models import SesiKeputusan, LocationRef
from apps.audit.models import EventLog
from apps.decision_sessions.services.transitions import transition_t1_open_assessment, extend_deadline
from apps.decision_sessions.services.deadline import (
    evaluate_session_deadline,
    record_sla_escalation_if_needed,
)


@pytest.fixture(autouse=True)
def seed_data(db):
    call_command('seed_gdss')


def _setup_penilaian_session():
    sesi = SesiKeputusan.objects.get(versi=1)
    ketua_user = User.objects.get(email='ketua@bpbd.go.id')
    transition_t1_open_assessment(sesi, ketua_user)
    sesi.refresh_from_db()
    return sesi


@pytest.mark.django_db
class TestDeadlineAndSLAEscalationAC11:
    def test_sla_escalation_6h_and_cron_never_finalizes_AC11(self):
        """
        AC-11 & Zona 1:
        - Jika tenggat terlewati > 6 jam (360 menit) tanpa aksi Ketua, SLA eskalasi terpicu.
        - Cron check_deadlines mencatat EventLog SLA_ESCALATION dan TIDAK mengubah status ke FINAL.
        """
        sesi = _setup_penilaian_session()
        # Simulasikan deadline sudah lewat 6 jam + 5 menit
        sesi.deadline_aktif = timezone.now() - timedelta(minutes=365)
        sesi.save(update_fields=['deadline_aktif'])

        info = evaluate_session_deadline(sesi)
        assert info['is_expired'] is True
        assert info['reminder_level'] == 'EXPIRED'
        assert info['sla_escalated'] is True
        assert 'ESKALASI KRITIS SLA' in info['sla_message']

        # Jalankan management command check_deadlines
        call_command('check_deadlines')

        # Zona 1: Status TIDAK BOLEH menjadi FINAL
        sesi.refresh_from_db()
        assert sesi.status == 'PENILAIAN'

        # EventLog SLA_ESCALATION tercatat
        log = EventLog.objects.filter(sesi=sesi, aksi='SLA_ESCALATION').first()
        assert log is not None
        assert log.status_dari == 'PENILAIAN'

        # Cek banner eskalasi muncul di Beranda L2
        client = Client()
        client.post(reverse('login'), {'email': 'admin@bpbd.go.id', 'kata_sandi': 'Bpbd@2026!'})
        res = client.get(reverse('session-list'))
        assert res.status_code == 200
        assert 'PERINGATAN ESKALASI SLA' in res.content.decode('utf-8')

    def test_reminder_50_and_80_percent(self):
        sesi = _setup_penilaian_session()
        now = timezone.now()
        # durasi_t1_menit default = 1440 menit (24 jam)
        # 55% berlalu -> sisa 45% (648 menit)
        sesi.deadline_aktif = now + timedelta(minutes=648)
        info_50 = evaluate_session_deadline(sesi, now=now)
        assert info_50['reminder_level'] == '50'
        assert info_50['sla_escalated'] is False

        # 85% berlalu -> sisa 15% (216 menit)
        sesi.deadline_aktif = now + timedelta(minutes=216)
        info_80 = evaluate_session_deadline(sesi, now=now)
        assert info_80['reminder_level'] == '80'

    def test_deadline_extension_max_2(self):
        """Perpanjangan tenggat maksimal 2 kali oleh KETUA; ke-3 ditolak."""
        sesi = _setup_penilaian_session()
        ketua = User.objects.get(email='ketua@bpbd.go.id')

        # Perpanjangan 1
        extend_deadline(
            sesi, ketua,
            alasan='Jaringan komunikasi di wilayah terdampak terganggu sementara',
            password_konfirmasi='Bpbd@2026!',
            tambahan_menit=120
        )
        sesi.refresh_from_db()
        assert sesi.jumlah_perpanjangan == 1

        # Perpanjangan 2
        extend_deadline(
            sesi, ketua,
            alasan='Menunggu verifikasi laporan survei lapangan tim PU',
            password_konfirmasi='Bpbd@2026!',
            tambahan_menit=120
        )
        sesi.refresh_from_db()
        assert sesi.jumlah_perpanjangan == 2

        # Perpanjangan ke-3 -> Ditolak
        from django.core.exceptions import ValidationError
        with pytest.raises(ValidationError):
            extend_deadline(
                sesi, ketua,
                alasan='Permintaan perpanjangan ketiga yang melebihi batas maksimal',
                password_konfirmasi='Bpbd@2026!',
                tambahan_menit=120
            )
        sesi.refresh_from_db()
        assert sesi.jumlah_perpanjangan == 2

    def test_draft_data_missing_location_refs_ok(self):
        """location_refs bersifat opsional; sesi tetap bisa berjalan tanpa error."""
        sesi = SesiKeputusan.objects.get(versi=1)
        assert LocationRef.objects.filter(sesi=sesi).count() == 0
        ketua = User.objects.get(email='ketua@bpbd.go.id')
        transition_t1_open_assessment(sesi, ketua)
        sesi.refresh_from_db()
        assert sesi.status == 'PENILAIAN'


@pytest.mark.django_db
class TestAdminPanelAndPltSwapAC11:
    def test_plt_seat_swap_invalidates_old_session_and_logs_history(self):
        """
        AC-11: Tukar Plt oleh ADMIN:
        - Memutus sesi login pejabat lama
        - Memperbarui pemegang kursi KETUA ke Plt
        - Mencatat SeatHistory dan EventLog PLT_SEAT_ASSIGNED
        """
        old_ketua = User.objects.get(email='ketua@bpbd.go.id')
        plt_user = User.objects.create_user(
            email='plt.ketua@bpbd.go.id',
            nama='Plt. Sekretaris BPBD',
            password='Bpbd@2026!',
            role='DM',
            aktif=True
        )

        # Pejabat lama sedang login di perangkatnya
        client_old = Client()
        client_old.post(reverse('login'), {'email': 'ketua@bpbd.go.id', 'kata_sandi': 'Bpbd@2026!'})
        old_ketua.refresh_from_db()
        assert old_ketua.active_session_key is not None

        # Admin login & melakukan penukaran Plt
        client_admin = Client()
        client_admin.post(reverse('login'), {'email': 'admin@bpbd.go.id', 'kata_sandi': 'Bpbd@2026!'})

        res_panel = client_admin.get(reverse('admin-panel'))
        assert res_panel.status_code == 200

        res_swap = client_admin.post(
            reverse('assign-plt-seat', args=['KETUA']),
            data={
                'user_id': plt_user.id,
                'alasan': 'Surat Tugas Plt No. 800/123/BPBD/2026 karena pejabat definitif tugas luar',
                'password_konfirmasi': 'Bpbd@2026!',
            }
        )
        assert res_swap.status_code == 302

        # Verifikasi pemegang kursi berganti ke Plt
        seat_ketua = Seat.objects.get(kode='KETUA')
        assert seat_ketua.user_id_aktif == plt_user

        # Verifikasi sesi pejabat lama diputus
        old_ketua.refresh_from_db()
        assert old_ketua.active_session_key is None
        res_old_next = client_old.get(reverse('session-list'))
        assert res_old_next.status_code == 302
        assert reverse('login') in res_old_next.url

        # Verifikasi SeatHistory & EventLog tercatat
        assert SeatHistory.objects.filter(seat=seat_ketua, user=plt_user).exists()
        assert EventLog.objects.filter(aksi='PLT_SEAT_ASSIGNED').exists()

    def test_dm_cannot_access_admin_panel_and_admin_cannot_hold_dm_seat(self):
        """Zona 2 & RBAC: DM ditolak (403) di L9; ADMIN ditolak menjadi pemegang kursi DM."""
        client_pu = Client()
        client_pu.post(reverse('login'), {'email': 'kadis.pu@pupr.go.id', 'kata_sandi': 'Bpbd@2026!'})
        res_forbidden = client_pu.get(reverse('admin-panel'))
        assert res_forbidden.status_code == 403

        # Admin mencoba assign dirinya sendiri (role=ADMIN) ke kursi KETUA -> Ditolak
        admin_user = User.objects.get(email='admin@bpbd.go.id')
        client_admin = Client()
        client_admin.post(reverse('login'), {'email': 'admin@bpbd.go.id', 'kata_sandi': 'Bpbd@2026!'})
        client_admin.post(
            reverse('assign-plt-seat', args=['KETUA']),
            data={
                'user_id': admin_user.id,
                'alasan': 'Percobaan ilegal memasukkan admin ke kursi DM',
                'password_konfirmasi': 'Bpbd@2026!',
            }
        )
        seat_ketua = Seat.objects.get(kode='KETUA')
        assert seat_ketua.user_id_aktif != admin_user
