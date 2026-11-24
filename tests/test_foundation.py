import pytest
from decimal import Decimal
from django.core.management import call_command
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from apps.accounts.models import User, Seat, SeatHistory
from apps.decision_sessions.models import SesiKeputusan, Location, Criteria
from apps.audit.models import EventLog

@pytest.fixture(autouse=True)
def seed_data(db):
    call_command('seed_gdss')

@pytest.mark.django_db
class TestFoundation:
    def test_seed_users_and_seats_integrity(self):
        # 4 Akun pengguna
        assert User.objects.count() >= 4
        ketua = User.objects.get(email='ketua@bpbd.go.id')
        pu = User.objects.get(email='kadis.pu@pupr.go.id')
        sosial = User.objects.get(email='kadis.sosial@dinsos.go.id')
        admin = User.objects.get(email='admin@bpbd.go.id')

        assert ketua.role == 'DM'
        assert pu.role == 'DM'
        assert sosial.role == 'DM'
        assert admin.role == 'ADMIN'

        # Password hasher verifikasi (Argon2)
        assert ketua.password.startswith('argon2')

        # 3 Kursi tetap
        assert Seat.objects.count() == 3
        seat_ketua = Seat.objects.get(kode='KETUA')
        seat_pu = Seat.objects.get(kode='PU')
        seat_sosial = Seat.objects.get(kode='SOSIAL')

        assert seat_ketua.user_id_aktif == ketua
        assert seat_pu.user_id_aktif == pu
        assert seat_sosial.user_id_aktif == sosial

    def test_single_user_cannot_hold_two_seats(self):
        ketua = User.objects.get(email='ketua@bpbd.go.id')
        seat_pu = Seat.objects.get(kode='PU')
        seat_pu.user_id_aktif = ketua
        with pytest.raises(IntegrityError):
            seat_pu.save()

    def test_seed_session_locations_and_criteria(self):
        sesi = SesiKeputusan.objects.get(versi=1)
        assert sesi.status == 'DRAFT'
        assert sesi.nilai_k == 3
        assert sesi.durasi_t1_menit == 1440
        assert sesi.durasi_t2_menit == 720
        assert sesi.sla_ketua_menit == 360

        # 6 Lokasi
        assert Location.objects.filter(sesi=sesi).count() == 6

        # 8 Kriteria
        kriteria_list = Criteria.objects.filter(sesi=sesi)
        assert kriteria_list.count() == 8

        total_bobot = sum(k.bobot for k in kriteria_list)
        assert total_bobot == Decimal('100.0000')

        # Verifikasi tipe C1 s.d C8
        c3 = Criteria.objects.get(sesi=sesi, kode='C3')
        assert c3.tipe == 'COST'
        c1 = Criteria.objects.get(sesi=sesi, kode='C1')
        assert c1.tipe == 'BENEFIT'

    def test_event_log_append_only_enforcement(self):
        # 1. Bisa create log baru
        log = EventLog.objects.create(
            aksi='TEST_ACTION',
            ip_address='127.0.0.1',
            user_agent='pytest-test',
            detail={'info': 'test'}
        )
        assert log.pk is not None

        # 2. Update via instance save() ditolak keras
        with pytest.raises(PermissionDenied):
            log.aksi = 'MODIFIED_ACTION'
            log.save()

        # 3. Delete via instance delete() ditolak keras
        with pytest.raises(PermissionDenied):
            log.delete()

        # 4. Bulk update via QuerySet ditolak
        with pytest.raises(PermissionDenied):
            EventLog.objects.filter(pk=log.pk).update(aksi='HACKED')

        # 5. Bulk delete via QuerySet ditolak
        with pytest.raises(PermissionDenied):
            EventLog.objects.filter(pk=log.pk).delete()
