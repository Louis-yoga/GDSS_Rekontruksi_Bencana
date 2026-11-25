import pytest
from decimal import Decimal
from django.core.management import call_command
from django.urls import reverse
from django.test import Client
from django.core.exceptions import PermissionDenied

from apps.accounts.models import User, Seat
from apps.decision_sessions.models import SesiKeputusan, Location, Criteria, Result
from apps.audit.models import EventLog
from apps.decision_sessions.services.transitions import (
    transition_t1_open_assessment,
    transition_t3_open_voting,
    transition_t5_finalize,
    transition_t6_cancel,
    transition_t7_revise,
    compute_final_hash,
)


@pytest.fixture(autouse=True)
def seed_data(db):
    call_command('seed_gdss')


@pytest.mark.django_db
class TestUATAndHardening15AC:
    def test_config_frozen_after_t1_AC14(self):
        """
        AC-14: Konfigurasi (bobot, nilai_k, judul) dapat diubah saat DRAFT,
        tetapi dibekukan keras (HTTP 403) setelah T1 (PENILAIAN).
        """
        sesi = SesiKeputusan.objects.get(versi=1)
        client_ketua = Client()
        client_ketua.post(reverse('login'), {'email': 'ketua@bpbd.go.id', 'kata_sandi': 'Bpbd@2026!'})

        # 1. Saat DRAFT: perubahan diperbolehkan
        res_ok = client_ketua.post(
            reverse('config-session', args=[sesi.id]),
            data={'judul': 'Prioritas Rekonstruksi 2026 (Updated)', 'nilai_k': '3'}
        )
        assert res_ok.status_code == 302
        sesi.refresh_from_db()
        assert sesi.judul == 'Prioritas Rekonstruksi 2026 (Updated)'

        # 2. Buka Penilaian (T1) -> Status menjadi PENILAIAN
        ketua_user = User.objects.get(email='ketua@bpbd.go.id')
        transition_t1_open_assessment(sesi, ketua_user)

        # 3. Coba ubah konfigurasi pasca-T1 -> HTTP 403 Forbidden
        res_frozen = client_ketua.post(
            reverse('config-session', args=[sesi.id]),
            data={'judul': 'Manipulasi Judul', 'nilai_k': '2'}
        )
        assert res_frozen.status_code == 403
        assert EventLog.objects.filter(sesi=sesi, aksi='CONFIG_MODIFICATION_BLOCKED').exists()

    def test_full_e2e_uat_draft_to_final_and_immutability_AC12_AC15(self):
        """
        Full End-to-End UAT:
        DRAFT -> PENILAIAN -> HASIL_AWAL -> VOTING -> PENETAPAN -> FINAL -> L8 Berita Acara
        Sekaligus memverifikasi:
        - AC-12 (Deviasi Top-K Borda vs WP wajib alasan_deviasi >= 20 char)
        - AC-15 (Immutability FINAL, SHA-256 final_hash konsisten, L8 terbuka pasca-FINAL)
        """
        sesi = SesiKeputusan.objects.get(versi=1)
        locations = list(sesi.locations.order_by('id'))
        criteria = list(sesi.criteria.order_by('id'))

        # Login 3 klien DM
        c_ketua = Client()
        c_pu = Client()
        c_sosial = Client()
        c_ketua.post(reverse('login'), {'email': 'ketua@bpbd.go.id', 'kata_sandi': 'Bpbd@2026!'})
        c_pu.post(reverse('login'), {'email': 'kadis.pu@pupr.go.id', 'kata_sandi': 'Bpbd@2026!'})
        c_sosial.post(reverse('login'), {'email': 'kadis.sosial@dinsos.go.id', 'kata_sandi': 'Bpbd@2026!'})

        # L8 Berita Acara sebelum FINAL harus ditolak (403)
        res_report_early = c_pu.get(reverse('report-view', args=[sesi.id]))
        assert res_report_early.status_code == 403

        # 1. T1: Ketua buka penilaian
        c_ketua.post(reverse('open-assessment', args=[sesi.id]))
        sesi.refresh_from_db()
        assert sesi.status == 'PENILAIAN'

        # 2. Semua 3 DM isi dan submit penilaian (LOK-A, LOK-B, LOK-C skor tinggi -> Top-3 WP)
        for client in [c_ketua, c_pu, c_sosial]:
            client.get(reverse('assessment-form', args=[sesi.id]))
            post_data = {'password_konfirmasi': 'Bpbd@2026!'}
            for idx, loc in enumerate(locations):
                score = '5' if idx < 3 else '1'
                for cr in criteria:
                    post_data[f'score_{loc.id}_{cr.id}'] = score
            client.post(reverse('submit-assessment', args=[sesi.id]), data=post_data)

        # Verifikasi T2-A otomatis ke HASIL_AWAL
        sesi.refresh_from_db()
        assert sesi.status == 'HASIL_AWAL'

        # 3. T3: Ketua buka voting
        c_ketua.post(reverse('open-voting', args=[sesi.id]))
        sesi.refresh_from_db()
        assert sesi.status == 'VOTING'

        # 4. Semua 3 DM vote memilih LOK-D, LOK-E, LOK-F (berbeda dari Top-3 WP -> memicu DEVIASI AC-12!)
        for client in [c_ketua, c_pu, c_sosial]:
            res_get = client.get(reverse('voting-view', args=[sesi.id]))
            token = res_get.context['idempotency_token']
            client.post(reverse('submit-vote', args=[sesi.id]), data={
                'idempotency_token': token,
                'password_konfirmasi': 'Bpbd@2026!',
                'rank_1': str(locations[3].id),
                'rank_2': str(locations[4].id),
                'rank_3': str(locations[5].id),
            })

        # Verifikasi T4-A otomatis ke PENETAPAN
        sesi.refresh_from_db()
        assert sesi.status == 'PENETAPAN'

        # 5. AC-12: Coba finalize tanpa alasan deviasi -> DITOLAK (status tetap PENETAPAN)
        c_ketua.post(reverse('finalize-session', args=[sesi.id]), data={
            'alasan_deviasi': 'terlalu pendek',
            'password_konfirmasi': 'Bpbd@2026!',
        })
        sesi.refresh_from_db()
        assert sesi.status == 'PENETAPAN'

        # 6. T5: Finalize dengan alasan deviasi >= 20 char -> BERHASIL ke FINAL
        res_fin = c_ketua.post(reverse('finalize-session', args=[sesi.id]), data={
            'alasan_deviasi': 'Pertimbangan akses logistik darurat dan kesepakatan rapat koordinasi lintas dinas.',
            'password_konfirmasi': 'Bpbd@2026!',
        })
        assert res_fin.status_code == 302
        sesi.refresh_from_db()
        assert sesi.status == 'FINAL'
        assert sesi.status_deviasi is True
        assert sesi.final_hash is not None
        assert len(sesi.final_hash) == 64
        assert sesi.final_hash == compute_final_hash(sesi)

        # 7. AC-15: Immutability pasca-FINAL
        res_refinal = c_ketua.post(reverse('finalize-session', args=[sesi.id]), data={
            'alasan_deviasi': 'Coba ubah lagi setelah final',
            'password_konfirmasi': 'Bpbd@2026!',
        })
        assert res_refinal.status_code == 403

        res_cancel_final = c_ketua.post(reverse('cancel-session', args=[sesi.id]), data={
            'alasan': 'Coba batalkan sesi yang sudah final',
            'password_konfirmasi': 'Bpbd@2026!',
        })
        assert res_cancel_final.status_code == 403

        # 8. L8 Berita Acara terbuka untuk semua peran setelah FINAL
        res_report = c_pu.get(reverse('report-view', args=[sesi.id]))
        assert res_report.status_code == 200
        html = res_report.content.decode('utf-8')
        assert sesi.final_hash in html
        assert 'Cetak Berita Acara' in html

    def test_revision_t7_and_cancellation_t6(self):
        """Verifikasi Transisi T7 (Revisi v2 dari FINAL) dan Transisi T6 (Pembatalan sesi non-FINAL)."""
        sesi = SesiKeputusan.objects.get(versi=1)
        ketua = User.objects.get(email='ketua@bpbd.go.id')

        # Siapkan sesi v1 langsung ke FINAL
        transition_t1_open_assessment(sesi, ketua)
        for idx, loc in enumerate(sesi.locations.order_by('id'), start=1):
            Result.objects.create(
                sesi=sesi, lokasi=loc,
                skor_wp=Decimal(str(0.30 - idx * 0.02)),
                rank_wp=idx, poin_borda=max(4 - idx, 0), rank_final=idx
            )
        sesi.status = 'PENETAPAN'
        sesi.save()
        transition_t5_finalize(sesi, ketua, password_konfirmasi='Bpbd@2026!')
        sesi.refresh_from_db()
        assert sesi.status == 'FINAL'

        # T7: Buat revisi v2
        v2 = transition_t7_revise(
            sesi, ketua,
            alasan='Data korban jiwa terbaru dari posko induk memerlukan evaluasi ulang',
            password_konfirmasi='Bpbd@2026!'
        )
        assert v2.versi == 2
        assert v2.parent == sesi
        assert v2.status == 'DRAFT'
        assert v2.locations.count() == sesi.locations.count()
        assert v2.criteria.count() == sesi.criteria.count()

        # T6: Batalkan v2
        transition_t6_cancel(
            v2, ketua,
            alasan='Sesi revisi dibatalkan karena kondisi lapangan sudah kembali normal',
            password_konfirmasi='Bpbd@2026!'
        )
        v2.refresh_from_db()
        assert v2.status == 'DIBATALKAN'
