import pytest
import json
from django.core.management import call_command
from django.urls import reverse
from django.test import Client
from django.utils import timezone
from apps.decision_sessions.models import SesiKeputusan, Location, Criteria, Assessment, AssessmentScore, Result
from apps.accounts.models import User, Seat
from apps.audit.models import EventLog
from apps.decision_sessions.services.transitions import transition_t1_open_assessment


@pytest.fixture(autouse=True)
def seed_data(db):
    call_command('seed_gdss')


def _setup_penilaian_session():
    """Helper untuk membuka sesi ke status PENILAIAN"""
    sesi = SesiKeputusan.objects.get(versi=1)
    ketua_user = User.objects.get(email='ketua@bpbd.go.id')
    transition_t1_open_assessment(sesi, ketua_user)
    sesi.refresh_from_db()
    return sesi


@pytest.mark.django_db
class TestAssessmentWorkflowFase3:
    def test_autosave_optimistic_locking_AC13(self):
        sesi = _setup_penilaian_session()
        client = Client()
        client.post(reverse('login'), {'email': 'kadis.pu@pupr.go.id', 'kata_sandi': 'Bpbd@2026!'})

        # Buka form dulu untuk menginisiasi assessment
        client.get(reverse('assessment-form', args=[sesi.id]))
        seat_pu = Seat.objects.get(kode='PU')
        ass = Assessment.objects.get(sesi=sesi, seat=seat_pu)
        assert ass.versi_draf == 1

        loc = sesi.locations.first()
        crit = sesi.criteria.first()

        # 1. Autosave dengan versi_draf yang cocok -> Berhasil
        payload = {
            'versi_draf': 1,
            'scores': [{'lokasi_id': loc.id, 'kriteria_id': crit.id, 'skor': 4}]
        }
        res = client.post(
            reverse('api-autosave-assessment', args=[sesi.id]),
            data=json.dumps(payload),
            content_type='application/json'
        )
        assert res.status_code == 200
        data = res.json()
        assert data['status'] == 'ok'
        assert data['versi_draf'] == 2

        # 2. Autosave kedua dengan versi_draf usang (versi 1 padahal sudah jadi 2) -> 409 Conflict
        res_conflict = client.post(
            reverse('api-autosave-assessment', args=[sesi.id]),
            data=json.dumps(payload),
            content_type='application/json'
        )
        assert res_conflict.status_code == 409
        assert 'Konflik versi draf' in res_conflict.json()['error']

    def test_matrix_validation_incomplete_cells_returns_422_AC08(self):
        sesi = _setup_penilaian_session()
        client = Client()
        client.post(reverse('login'), {'email': 'kadis.pu@pupr.go.id', 'kata_sandi': 'Bpbd@2026!'})

        # Isi hanya 1 sel dari 48 sel yang seharusnya (6 lokasi x 8 kriteria)
        loc = sesi.locations.first()
        crit = sesi.criteria.first()
        post_data = {
            f'score_{loc.id}_{crit.id}': '4',
            'password_konfirmasi': 'Bpbd@2026!',
            'idempotency_token': 'test_token',
        }
        res = client.post(reverse('submit-assessment', args=[sesi.id]), data=post_data)
        # Menolak dengan HTTP 422
        assert res.status_code == 422
        content = res.content.decode('utf-8')
        assert 'Matriks belum lengkap' in content

    def test_blind_scoring_no_leak_between_dms_AC04(self):
        """
        AC-04 Blind Scoring:
        Skor mentah milik DM lain dilarang terlihat atau terekspos sebelum sesi FINAL.
        """
        sesi = _setup_penilaian_session()
        # PU mengisi skor
        client_pu = Client()
        client_pu.post(reverse('login'), {'email': 'kadis.pu@pupr.go.id', 'kata_sandi': 'Bpbd@2026!'})
        client_pu.get(reverse('assessment-form', args=[sesi.id]))

        seat_pu = Seat.objects.get(kode='PU')
        ass_pu = Assessment.objects.get(sesi=sesi, seat=seat_pu)
        loc1 = sesi.locations.first()
        crit1 = sesi.criteria.first()
        AssessmentScore.objects.create(assessment=ass_pu, lokasi=loc1, kriteria=crit1, skor=5)

        # Sekarang Dinsos (SOSIAL) login dan buka form penilaian
        client_sosial = Client()
        client_sosial.post(reverse('login'), {'email': 'kadis.sosial@dinsos.go.id', 'kata_sandi': 'Bpbd@2026!'})
        res_sosial = client_sosial.get(reverse('assessment-form', args=[sesi.id]))

        # Pastikan skor PU (skor_dict) tidak ada di konteks Dinsos
        sosial_scores = res_sosial.context['score_dict']
        assert (loc1.id, crit1.id) not in sosial_scores or sosial_scores.get((loc1.id, crit1.id)) != 5

    def test_assessment_immutable_post_submit_and_unlock_by_ketua_AC07(self):
        sesi = _setup_penilaian_session()
        client = Client()
        client.post(reverse('login'), {'email': 'kadis.pu@pupr.go.id', 'kata_sandi': 'Bpbd@2026!'})

        # Isi semua sel
        client.get(reverse('assessment-form', args=[sesi.id]))
        seat_pu = Seat.objects.get(kode='PU')
        ass_pu = Assessment.objects.get(sesi=sesi, seat=seat_pu)

        post_data = {'password_konfirmasi': 'Bpbd@2026!'}
        for loc in sesi.locations.all():
            for cr in sesi.criteria.all():
                post_data[f'score_{loc.id}_{cr.id}'] = '3'

        # Submit
        res_submit = client.post(reverse('submit-assessment', args=[sesi.id]), data=post_data)
        assert res_submit.status_code == 302
        ass_pu.refresh_from_db()
        assert ass_pu.status == 'SUBMITTED'

        # Coba autosave setelah disubmit -> Ditolak HTTP 403
        res_autosave_after = client.post(
            reverse('api-autosave-assessment', args=[sesi.id]),
            data=json.dumps({'versi_draf': ass_pu.versi_draf, 'scores': []}),
            content_type='application/json'
        )
        assert res_autosave_after.status_code == 403

        # Hanya KETUA yang bisa membuka kunci (AC-07)
        client_ketua = Client()
        client_ketua.post(reverse('login'), {'email': 'ketua@bpbd.go.id', 'kata_sandi': 'Bpbd@2026!'})

        # Unlock dengan alasan valid
        res_unlock = client_ketua.post(
            reverse('unlock-assessment', args=[sesi.id, 'PU']),
            data={'alasan': 'Permintaan koreksi data kerusakan infrastruktur jalan dari PU', 'password_konfirmasi': 'Bpbd@2026!'}
        )
        assert res_unlock.status_code == 302
        ass_pu.refresh_from_db()
        assert ass_pu.status == 'DRAFT'
        assert ass_pu.versi_draf == 2

    def test_force_close_quorum_2_3_AC09(self):
        """
        AC-09: Tutup paksa penilaian dengan kuorum 2/3.
        - Jika hanya 1 kursi submit -> Gagal ditutup (guard negatif)
        - Jika 2 kursi submit & lewat tenggat -> Berhasil dengan kuorum_parsial=True
        """
        sesi = _setup_penilaian_session()
        # Set deadline di masa lalu agar memenuhi syarat deadline terlewati
        sesi.deadline_aktif = timezone.now() - timezone.timedelta(minutes=10)
        sesi.save()

        seat_ketua = Seat.objects.get(kode='KETUA')
        seat_pu = Seat.objects.get(kode='PU')
        locations = list(sesi.locations.all())
        criteria = list(sesi.criteria.all())

        # 1. Hanya 1 kursi (KETUA) submit
        ass_ketua = Assessment.objects.create(sesi=sesi, seat=seat_ketua, user=seat_ketua.user_id_aktif, status='SUBMITTED')
        for loc in locations:
            for cr in criteria:
                AssessmentScore.objects.create(assessment=ass_ketua, lokasi=loc, kriteria=cr, skor=3)

        client_ketua = Client()
        client_ketua.post(reverse('login'), {'email': 'ketua@bpbd.go.id', 'kata_sandi': 'Bpbd@2026!'})

        # Coba tutup paksa dengan 1 kursi -> Gagal (redirect dengan flash message error)
        res_fail = client_ketua.post(
            reverse('close-assessment', args=[sesi.id]),
            data={'alasan': 'Penutupan paksa darurat untuk mempercepat alokasi logistik', 'password_konfirmasi': 'Bpbd@2026!'}
        )
        sesi.refresh_from_db()
        assert sesi.status == 'PENILAIAN'

        # 2. Kursi kedua (PU) submit -> Sekarang ada 2 kursi submit (Kuorum 2/3 terpenuhi)
        ass_pu = Assessment.objects.create(sesi=sesi, seat=seat_pu, user=seat_pu.user_id_aktif, status='SUBMITTED')
        for loc in locations:
            for cr in criteria:
                AssessmentScore.objects.create(assessment=ass_pu, lokasi=loc, kriteria=cr, skor=4)

        # Tutup paksa kembali
        res_ok = client_ketua.post(
            reverse('close-assessment', args=[sesi.id]),
            data={'alasan': 'Kadis Sosial berhalangan hadir di daerah terisolir, kuorum 2/3 terpenuhi', 'password_konfirmasi': 'Bpbd@2026!'}
        )
        assert res_ok.status_code == 302
        sesi.refresh_from_db()
        assert sesi.status == 'HASIL_AWAL'
        assert sesi.kuorum_parsial is True

        # Hasil WP terhitung
        assert Result.objects.filter(sesi=sesi).count() == len(locations)

    def test_auto_transition_t2a_when_all_three_dms_submit(self):
        """
        Transisi T2-A: Saat kursi ke-3 submit penilaian, sesi otomatis berpindah ke HASIL_AWAL.
        """
        sesi = _setup_penilaian_session()
        locations = list(sesi.locations.all())
        criteria = list(sesi.criteria.all())

        users = [
            ('ketua@bpbd.go.id', 'KETUA'),
            ('kadis.pu@pupr.go.id', 'PU'),
            ('kadis.sosial@dinsos.go.id', 'SOSIAL'),
        ]

        # 2 kursi pertama submit
        for email, seat_code in users[:2]:
            seat = Seat.objects.get(kode=seat_code)
            ass = Assessment.objects.create(sesi=sesi, seat=seat, user=seat.user_id_aktif, status='SUBMITTED')
            for loc in locations:
                for cr in criteria:
                    AssessmentScore.objects.create(assessment=ass, lokasi=loc, kriteria=cr, skor=3)

        sesi.refresh_from_db()
        assert sesi.status == 'PENILAIAN'

        # Kursi ke-3 (SOSIAL) login dan submit via HTTP POST
        client = Client()
        client.post(reverse('login'), {'email': 'kadis.sosial@dinsos.go.id', 'kata_sandi': 'Bpbd@2026!'})
        client.get(reverse('assessment-form', args=[sesi.id]))

        post_data = {'password_konfirmasi': 'Bpbd@2026!'}
        for loc in locations:
            for cr in criteria:
                post_data[f'score_{loc.id}_{cr.id}'] = '4'

        res = client.post(reverse('submit-assessment', args=[sesi.id]), data=post_data)
        assert res.status_code == 302
        # Redirect ke results-view karena sesi telah auto-transisi
        assert reverse('results-view', args=[sesi.id]) in res.url

        sesi.refresh_from_db()
        assert sesi.status == 'HASIL_AWAL'
        assert sesi.kuorum_parsial is False
        assert Result.objects.filter(sesi=sesi).count() == len(locations)
