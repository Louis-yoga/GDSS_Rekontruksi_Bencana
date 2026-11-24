import pytest
from decimal import Decimal
from django.core.management import call_command
from django.urls import reverse
from django.test import Client
from django.utils import timezone
from apps.decision_sessions.models import SesiKeputusan, Location, Result, Vote, VoteItem
from apps.accounts.models import User, Seat
from apps.decision_sessions.services.transitions import (
    transition_t1_open_assessment,
    transition_t3_open_voting,
    transition_t4b_force_close_voting
)
from apps.decision_sessions.services.borda_engine import (
    compute_borda_points,
    resolve_group_ties,
    execute_borda_and_ranking
)


@pytest.fixture(autouse=True)
def seed_data(db):
    call_command('seed_gdss')


def _setup_voting_session():
    """Helper untuk menyiapkan sesi di tahap VOTING dengan data Result WP yang sudah terhitung"""
    sesi = SesiKeputusan.objects.get(versi=1)
    ketua_user = User.objects.get(email='ketua@bpbd.go.id')

    # Buka penilaian
    transition_t1_open_assessment(sesi, ketua_user)
    sesi.refresh_from_db()

    # Buat skor WP dummy untuk seluruh lokasi
    locations = list(sesi.locations.all().order_by('id'))
    for idx, loc in enumerate(locations, start=1):
        Result.objects.update_or_create(
            sesi=sesi,
            lokasi=loc,
            defaults={
                'skor_wp': Decimal(str(0.30 - (idx * 0.03))),
                'rank_wp': idx,
            }
        )

    # Beralih ke HASIL_AWAL lalu buka VOTING (T3)
    sesi.status = 'HASIL_AWAL'
    sesi.save()
    transition_t3_open_voting(sesi, ketua_user)
    sesi.refresh_from_db()
    return sesi


@pytest.mark.django_db
class TestBordaVotingAndTieBreaker:
    def test_borda_tie_resolution_wp_score_AC01(self):
        """
        AC-01: Tie-break Tingkat 1 (WP_SCORE)
        Jika dua lokasi mendapat total poin Borda yang sama, lokasi dengan skor WP lebih tinggi menang.
        """
        sesi = _setup_voting_session()
        locations = list(sesi.locations.all())
        loc_a = locations[0]
        loc_b = locations[1]

        # Buat Result dengan skor WP: loc_a lebih tinggi dari loc_b
        res_a = Result.objects.get(sesi=sesi, lokasi=loc_a)
        res_b = Result.objects.get(sesi=sesi, lokasi=loc_b)
        res_a.skor_wp = Decimal('0.250000')
        res_a.save()
        res_b.skor_wp = Decimal('0.200000')
        res_b.save()

        wp_map = {res_a.lokasi_id: res_a, res_b.lokasi_id: res_b}

        # Selesaikan seri
        ordered, method, note = resolve_group_ties([loc_a, loc_b], wp_map, ketua_vote=None, k=sesi.nilai_k)
        assert method == 'WP_SCORE'
        assert ordered[0] == loc_a
        assert ordered[1] == loc_b

    def test_borda_tie_resolution_ketua_ballot_AC02(self):
        """
        AC-02: Tie-break Tingkat 2 (KETUA_BALLOT)
        Jika poin Borda sama DAN skor WP sama persis, urutan ditentukan oleh peringkat di surat suara Ketua.
        """
        sesi = _setup_voting_session()
        locations = list(sesi.locations.all())
        loc_a = locations[0]
        loc_b = locations[1]

        # Skor WP sama persis
        res_a = Result.objects.get(sesi=sesi, lokasi=loc_a)
        res_b = Result.objects.get(sesi=sesi, lokasi=loc_b)
        res_a.skor_wp = Decimal('0.200000')
        res_a.save()
        res_b.skor_wp = Decimal('0.200000')
        res_b.save()

        wp_map = {res_a.lokasi_id: res_a, res_b.lokasi_id: res_b}

        # Surat suara Ketua menempatkan loc_b di peringkat 1, loc_a di peringkat 2
        seat_ketua = Seat.objects.get(kode='KETUA')
        ketua_vote = Vote.objects.create(
            sesi=sesi, seat=seat_ketua, user=seat_ketua.user_id_aktif, abstain=False
        )
        VoteItem.objects.create(vote=ketua_vote, peringkat=1, lokasi=loc_b)
        VoteItem.objects.create(vote=ketua_vote, peringkat=2, lokasi=loc_a)

        ordered, method, note = resolve_group_ties([loc_a, loc_b], wp_map, ketua_vote=ketua_vote, k=sesi.nilai_k)
        assert method == 'KETUA_BALLOT'
        # loc_b menang karena dipilih peringkat 1 oleh Ketua
        assert ordered[0] == loc_b
        assert ordered[1] == loc_a

    def test_borda_tie_resolution_ketua_manual_AC02(self):
        """
        AC-02: Tie-break Tingkat 3 (KETUA_MANUAL)
        Jika poin Borda sama, skor WP sama persis, dan Ketua abstain (tidak memilih),
        sistem menandai metode KETUA_MANUAL.
        """
        sesi = _setup_voting_session()
        locations = list(sesi.locations.all())
        loc_a = locations[0]
        loc_b = locations[1]

        res_a = Result.objects.get(sesi=sesi, lokasi=loc_a)
        res_b = Result.objects.get(sesi=sesi, lokasi=loc_b)
        res_a.skor_wp = Decimal('0.200000')
        res_a.save()
        res_b.skor_wp = Decimal('0.200000')
        res_b.save()

        wp_map = {res_a.lokasi_id: res_a, res_b.lokasi_id: res_b}

        # Ketua Abstain
        seat_ketua = Seat.objects.get(kode='KETUA')
        ketua_vote = Vote.objects.create(
            sesi=sesi, seat=seat_ketua, user=seat_ketua.user_id_aktif, abstain=True
        )

        ordered, method, note = resolve_group_ties([loc_a, loc_b], wp_map, ketua_vote=ketua_vote, k=sesi.nilai_k)
        assert method == 'KETUA_MANUAL'

    def test_vote_idempotency_double_click_AC05(self):
        """
        AC-05: Idempotency Submit Suara
        Pengiriman duplikat (misal double-click atau manipulasi token) ditolak.
        """
        sesi = _setup_voting_session()
        client = Client()
        client.post(reverse('login'), {'email': 'kadis.pu@pupr.go.id', 'kata_sandi': 'Bpbd@2026!'})

        # Buka form voting untuk mendapatkan idempotency token
        res_get = client.get(reverse('voting-view', args=[sesi.id]))
        token = res_get.context['idempotency_token']

        locations = list(sesi.locations.all()[:3])
        post_data = {
            'idempotency_token': token,
            'password_konfirmasi': 'Bpbd@2026!',
            'rank_1': str(locations[0].id),
            'rank_2': str(locations[1].id),
            'rank_3': str(locations[2].id),
        }

        # 1. Submit pertama -> Sukses
        res_first = client.post(reverse('submit-vote', args=[sesi.id]), data=post_data)
        assert res_first.status_code == 302
        seat_pu = Seat.objects.get(kode='PU')
        assert Vote.objects.filter(sesi=sesi, seat=seat_pu).count() == 1

        # 2. Submit kedua dengan token yang sama (double click) -> Ditolak / Diabaikan
        res_second = client.post(reverse('submit-vote', args=[sesi.id]), data=post_data)
        assert res_second.status_code == 302
        # Jumlah vote di database tetap tepat 1
        assert Vote.objects.filter(sesi=sesi, seat=seat_pu).count() == 1

    def test_no_close_voting_before_deadline_AC10(self):
        """
        AC-10: Larangan Tutup Voting Sebelum Deadline Terlewati
        Upaya penutupan voting paksa ketika waktu saat ini masih sebelum deadline harus ditolak.
        """
        sesi = _setup_voting_session()
        # Set deadline 1 jam ke depan (belum terlewati)
        sesi.deadline_aktif = timezone.now() + timezone.timedelta(hours=1)
        sesi.save()

        client_ketua = Client()
        client_ketua.post(reverse('login'), {'email': 'ketua@bpbd.go.id', 'kata_sandi': 'Bpbd@2026!'})

        res = client_ketua.post(
            reverse('close-voting', args=[sesi.id]),
            data={'alasan': 'Mencoba menutup voting lebih awal sebelum tenggat', 'password_konfirmasi': 'Bpbd@2026!'}
        )
        sesi.refresh_from_db()
        # Status sesi tetap VOTING (tidak boleh berubah)
        assert sesi.status == 'VOTING'

    def test_auto_transition_t4a_when_all_three_vote(self):
        """
        Transisi T4-A: Saat kursi ke-3 memberikan suara, sesi otomatis beralih ke PENETAPAN.
        """
        sesi = _setup_voting_session()
        locations = list(sesi.locations.all()[:3])

        # 2 kursi pertama submit
        users = [
            ('ketua@bpbd.go.id', 'KETUA'),
            ('kadis.pu@pupr.go.id', 'PU'),
        ]
        for email, seat_code in users:
            seat = Seat.objects.get(kode=seat_code)
            v = Vote.objects.create(sesi=sesi, seat=seat, user=seat.user_id_aktif, abstain=False)
            for r, loc in enumerate(locations, start=1):
                VoteItem.objects.create(vote=v, peringkat=r, lokasi=loc)

        sesi.refresh_from_db()
        assert sesi.status == 'VOTING'

        # Kursi ke-3 (SOSIAL) vote via client POST
        client_sosial = Client()
        client_sosial.post(reverse('login'), {'email': 'kadis.sosial@dinsos.go.id', 'kata_sandi': 'Bpbd@2026!'})
        res_get = client_sosial.get(reverse('voting-view', args=[sesi.id]))
        token = res_get.context['idempotency_token']

        post_data = {
            'idempotency_token': token,
            'password_konfirmasi': 'Bpbd@2026!',
            'rank_1': str(locations[0].id),
            'rank_2': str(locations[1].id),
            'rank_3': str(locations[2].id),
        }
        res_post = client_sosial.post(reverse('submit-vote', args=[sesi.id]), data=post_data)
        assert res_post.status_code == 302

        sesi.refresh_from_db()
        assert sesi.status == 'PENETAPAN'
        assert sesi.kuorum_parsial is False

        # Verifikasi poin Borda terisi di tabel results
        loc_a_res = Result.objects.get(sesi=sesi, lokasi=locations[0])
        assert loc_a_res.poin_borda > 0
        assert loc_a_res.rank_final is not None
