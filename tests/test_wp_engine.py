import pytest
import math
from decimal import Decimal
from django.core.management import call_command
from apps.decision_sessions.models import SesiKeputusan, Location, Criteria, Assessment, AssessmentScore, Result
from apps.accounts.models import User, Seat
from apps.decision_sessions.services.wp_engine import (
    normalize_weights,
    compute_geometric_mean,
    compute_wp_vector,
    rank_wp,
    calculate_divergence,
    execute_wp_engine
)


@pytest.fixture(autouse=True)
def seed_data(db):
    call_command('seed_gdss')


@pytest.mark.django_db
class TestWPEngineDeterministic:
    def test_normalize_weights_sum_to_one(self):
        sesi = SesiKeputusan.objects.get(versi=1)
        criteria = list(sesi.criteria.all())
        norm_weights = normalize_weights(criteria)

        total_normalized = sum(norm_weights.values())
        assert math.isclose(total_normalized, 1.0, rel_tol=1e-5)

        # C3 adalah COST dengan bobot 10 dari total 100 -> 0.10
        c3 = sesi.criteria.get(kode='C3')
        assert math.isclose(norm_weights[c3.id], 0.10, rel_tol=1e-5)

    def test_geometric_mean_calculation(self):
        sesi = SesiKeputusan.objects.get(versi=1)
        locations = list(sesi.locations.all()[:2])
        criteria = list(sesi.criteria.all()[:2])
        seat_ketua = Seat.objects.get(kode='KETUA')
        seat_pu = Seat.objects.get(kode='PU')

        # Buat 2 assessment SUBMITTED
        ass1 = Assessment.objects.create(sesi=sesi, seat=seat_ketua, user=seat_ketua.user_id_aktif, status='SUBMITTED')
        ass2 = Assessment.objects.create(sesi=sesi, seat=seat_pu, user=seat_pu.user_id_aktif, status='SUBMITTED')

        # Skor loc[0], crit[0] = 4 dan 4 -> geo_mean = sqrt(16) = 4.0
        # Skor loc[0], crit[1] = 2 dan 8 (skor 1-5, misal 2 dan 5 -> sqrt(10) = 3.162277)
        AssessmentScore.objects.create(assessment=ass1, lokasi=locations[0], kriteria=criteria[0], skor=4)
        AssessmentScore.objects.create(assessment=ass2, lokasi=locations[0], kriteria=criteria[0], skor=4)
        AssessmentScore.objects.create(assessment=ass1, lokasi=locations[0], kriteria=criteria[1], skor=2)
        AssessmentScore.objects.create(assessment=ass2, lokasi=locations[0], kriteria=criteria[1], skor=5)

        AssessmentScore.objects.create(assessment=ass1, lokasi=locations[1], kriteria=criteria[0], skor=3)
        AssessmentScore.objects.create(assessment=ass2, lokasi=locations[1], kriteria=criteria[0], skor=3)
        AssessmentScore.objects.create(assessment=ass1, lokasi=locations[1], kriteria=criteria[1], skor=1)
        AssessmentScore.objects.create(assessment=ass2, lokasi=locations[1], kriteria=criteria[1], skor=4)

        geo = compute_geometric_mean(locations, criteria, [ass1, ass2])
        assert math.isclose(geo[(locations[0].id, criteria[0].id)], 4.0, rel_tol=1e-5)
        assert math.isclose(geo[(locations[0].id, criteria[1].id)], math.sqrt(10), rel_tol=1e-5)

    def test_cost_criteria_receives_negative_power(self):
        sesi = SesiKeputusan.objects.get(versi=1)
        locations = list(sesi.locations.all()[:2])
        # Ambil C1 (BENEFIT) dan C3 (COST)
        c1 = sesi.criteria.get(kode='C1')
        c3 = sesi.criteria.get(kode='C3')
        criteria = [c1, c3]

        norm_weights = normalize_weights(criteria)
        # Misal lokasi 0 memiliki nilai cost tinggi (5) dan lokasi 1 memiliki nilai cost rendah (1)
        # Nilai benefit sama (3)
        matrix = {
            (locations[0].id, c1.id): 3.0,
            (locations[0].id, c3.id): 5.0, # Biaya tinggi -> harusnya menurunkan S_i
            (locations[1].id, c1.id): 3.0,
            (locations[1].id, c3.id): 1.0, # Biaya rendah -> harusnya menaikkan S_i
        }

        wp_vec = compute_wp_vector(locations, criteria, matrix, norm_weights)
        s0 = wp_vec[locations[0].id]['S']
        s1 = wp_vec[locations[1].id]['S']

        # Karena C3 adalah COST, lokasi dengan biaya lebih rendah (lokasi 1) harus memiliki S_1 > S_0
        assert s1 > s0
        assert wp_vec[locations[1].id]['V'] > wp_vec[locations[0].id]['V']

    def test_divergence_classification(self):
        sesi = SesiKeputusan.objects.get(versi=1)
        locations = list(sesi.locations.all()[:3])
        criteria = list(sesi.criteria.all())
        norm_weights = normalize_weights(criteria)

        seat_ketua = Seat.objects.get(kode='KETUA')
        seat_pu = Seat.objects.get(kode='PU')
        seat_sosial = Seat.objects.get(kode='SOSIAL')

        ass_ketua = Assessment.objects.create(sesi=sesi, seat=seat_ketua, user=seat_ketua.user_id_aktif, status='SUBMITTED')
        ass_pu = Assessment.objects.create(sesi=sesi, seat=seat_pu, user=seat_pu.user_id_aktif, status='SUBMITTED')
        ass_sosial = Assessment.objects.create(sesi=sesi, seat=seat_sosial, user=seat_sosial.user_id_aktif, status='SUBMITTED')

        # Beri skor tinggi untuk lokasi 0 bagi semua DM -> Divergensi Rendah
        for ass in [ass_ketua, ass_pu, ass_sosial]:
            for cr in criteria:
                AssessmentScore.objects.create(assessment=ass, lokasi=locations[0], kriteria=cr, skor=5)
                AssessmentScore.objects.create(assessment=ass, lokasi=locations[1], kriteria=cr, skor=2)
                AssessmentScore.objects.create(assessment=ass, lokasi=locations[2], kriteria=cr, skor=1)

        div = calculate_divergence([ass_ketua, ass_pu, ass_sosial], locations, criteria, norm_weights)
        assert div == 'RENDAH'
