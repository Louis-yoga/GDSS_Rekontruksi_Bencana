"""
Weighted Product (WP) Engine & Multi-Expert Geometric Mean Aggregation.

Zona 3 / PRD §1.5:
- Agregasi kelompok menggunakan rata-rata geometrik.
- Perankingan kelompok menggunakan Weighted Product (WP) multi-attribute utility theory.
- Dilarang mengganti dengan metode SAW atau TOPSIS.
"""
from decimal import Decimal
import math
from typing import Dict, List, Tuple
from django.db import transaction
from apps.decision_sessions.models import SesiKeputusan, Location, Criteria, Assessment, AssessmentScore, Result


def normalize_weights(criteria_list: List[Criteria]) -> Dict[int, float]:
    """
    Menghitung normalisasi bobot kriteria:
    ŵ_j = W_j / Σ W_k
    sehingga Σ ŵ_j = 1.0
    """
    total_bobot = sum(float(c.bobot) for c in criteria_list)
    if total_bobot <= 0:
        raise ValueError("Total bobot kriteria harus lebih besar dari nol.")

    return {c.id: float(c.bobot) / total_bobot for c in criteria_list}


def compute_geometric_mean(
    locations: List[Location],
    criteria: List[Criteria],
    assessments: List[Assessment]
) -> Dict[Tuple[int, int], float]:
    """
    Menghitung rata-rata geometrik skor kelompok untuk tiap sel (lokasi, kriteria):
    x̄_{i,j} = ( Π x_{i,j}^{(d)} )^(1 / |S|)
    hanya dari kursi yang telah berstatus SUBMITTED (|S| >= 2).
    """
    m = len(assessments)
    if m == 0:
        raise ValueError("Tidak ada assessment yang valid untuk dihitung.")

    # Ambil semua skor dari assessment yang valid
    scores_qs = AssessmentScore.objects.filter(assessment__in=assessments)
    # Map: (lokasi_id, kriteria_id) -> list of scores
    score_map: Dict[Tuple[int, int], List[int]] = {}
    for sc in scores_qs:
        key = (sc.lokasi_id, sc.kriteria_id)
        score_map.setdefault(key, []).append(sc.skor)

    geo_means: Dict[Tuple[int, int], float] = {}
    for loc in locations:
        for crit in criteria:
            key = (loc.id, crit.id)
            scores = score_map.get(key, [])
            if not scores:
                raise ValueError(f"Skor tidak lengkap untuk lokasi {loc.kode} kriteria {crit.kode}")
            
            # Perkalian semua skor DM
            prod = 1.0
            for s in scores:
                prod *= float(s)
            geo_mean = prod ** (1.0 / len(scores))
            geo_means[key] = geo_mean

    return geo_means


def compute_wp_vector(
    locations: List[Location],
    criteria: List[Criteria],
    matrix: Dict[Tuple[int, int], float],
    norm_weights: Dict[int, float]
) -> Dict[int, Dict[str, float]]:
    """
    Menghitung vektor preferensi S_i dan nilai relatif V_i:
    S_i = Π ( x̄_{i,j} )^(p_j)
    di mana p_j = +ŵ_j jika BENEFIT, dan -ŵ_j jika COST.
    V_i = S_i / Σ S_k
    """
    s_vectors: Dict[int, float] = {}

    for loc in locations:
        s_i = 1.0
        for crit in criteria:
            w_hat = norm_weights[crit.id]
            exponent = w_hat if crit.tipe == 'BENEFIT' else -w_hat
            val = matrix[(loc.id, crit.id)]
            s_i *= (val ** exponent)
        s_vectors[loc.id] = s_i

    sum_s = sum(s_vectors.values())
    if sum_s <= 0:
        raise ZeroDivisionError("Total vektor S tidak boleh nol.")

    results: Dict[int, Dict[str, float]] = {}
    for loc in locations:
        s_val = s_vectors[loc.id]
        v_val = s_val / sum_s
        results[loc.id] = {
            'S': s_val,
            'V': v_val,
        }

    return results


def rank_wp(locations: List[Location], wp_results: Dict[int, Dict[str, float]]) -> List[Dict]:
    """
    Mengurutkan alternatif berdasarkan nilai V_i secara descending.
    Tie-break deterministik jika V_i identik: urutkan berdasarkan kode lokasi ascending.
    """
    loc_map = {loc.id: loc for loc in locations}
    
    # Sort: V_i desc, then kode asc
    sorted_items = sorted(
        wp_results.items(),
        key=lambda item: (-item[1]['V'], loc_map[item[0]].kode)
    )

    ranked_list = []
    for rank, (loc_id, data) in enumerate(sorted_items, start=1):
        loc = loc_map[loc_id]
        ranked_list.append({
            'lokasi': loc,
            'lokasi_id': loc.id,
            'lokasi_kode': loc.kode,
            'lokasi_nama': loc.nama,
            'skor_s': data['S'],
            'skor_v': data['V'],
            'rank_wp': rank,
        })

    return ranked_list


def compute_individual_wp(
    assessment: Assessment,
    locations: List[Location],
    criteria: List[Criteria],
    norm_weights: Dict[int, float]
) -> List[int]:
    """
    Menghitung WP individual untuk 1 kursi DM tertentu guna menentukan peringkat Top-1 DM
    tanpa mengekspos skor mentah ke kursi lain (Zona 5 / Blind Scoring).
    Mengembalikan list lokasi_id terurut dari peringkat 1 s.d. terakhir.
    """
    scores = AssessmentScore.objects.filter(assessment=assessment)
    matrix = {(sc.lokasi_id, sc.kriteria_id): float(sc.skor) for sc in scores}

    s_vectors = {}
    for loc in locations:
        s_i = 1.0
        for crit in criteria:
            val = matrix.get((loc.id, crit.id), 1.0)
            w_hat = norm_weights[crit.id]
            exp = w_hat if crit.tipe == 'BENEFIT' else -w_hat
            s_i *= (val ** exp)
        s_vectors[loc.id] = s_i

    loc_map = {loc.id: loc for loc in locations}
    sorted_locs = sorted(
        s_vectors.keys(),
        key=lambda loc_id: (-s_vectors[loc_id], loc_map[loc_id].kode)
    )
    return sorted_locs


def calculate_divergence(
    assessments: List[Assessment],
    locations: List[Location],
    criteria: List[Criteria],
    norm_weights: Dict[int, float]
) -> str:
    """
    Klasifikasi divergensi opini berdasarkan kesamaan Top-1 tiap DM:
    - RENDAH : Semua DM memiliki Top-1 lokasi yang sama
    - SEDANG : Tepat 2 DM memiliki Top-1 lokasi yang sama, 1 berbeda
    - TINGGI : Ketiga DM memiliki Top-1 lokasi yang berbeda semua
    """
    if len(assessments) < 2:
        return 'SEDANG'

    top1_list = []
    for ass in assessments:
        ranked = compute_individual_wp(ass, locations, criteria, norm_weights)
        if ranked:
            top1_list.append(ranked[0])

    unique_top1 = set(top1_list)
    if len(unique_top1) == 1:
        return 'RENDAH'
    elif len(unique_top1) == len(top1_list) and len(top1_list) == 3:
        return 'TINGGI'
    else:
        return 'SEDANG'


@transaction.atomic
def execute_wp_engine(sesi: SesiKeputusan) -> List[Dict]:
    """
    Menjalankan seluruh pipeline WP untuk sesi keputusan:
    1. Ambil kriteria dan lokasi
    2. Ambil assessment berstatus SUBMITTED
    3. Normalisasi bobot
    4. Rata-rata geometrik
    5. Hitung V(i) dan ranking
    6. Simpan / perbarui tabel results
    """
    locations = list(sesi.locations.all().order_by('id'))
    criteria = list(sesi.criteria.all().order_by('id'))
    assessments = list(sesi.assessments.filter(status='SUBMITTED'))

    if len(assessments) < 2:
        raise ValueError("Kuorum tidak terpenuhi: Minimal 2 kursi harus sudah submit penilaian.")

    norm_weights = normalize_weights(criteria)
    geo_matrix = compute_geometric_mean(locations, criteria, assessments)
    wp_results = compute_wp_vector(locations, criteria, geo_matrix, norm_weights)
    ranked = rank_wp(locations, wp_results)

    # Simpan atau update ke tabel results
    for item in ranked:
        Result.objects.update_or_create(
            sesi=sesi,
            lokasi=item['lokasi'],
            defaults={
                'skor_wp': Decimal(str(round(item['skor_v'], 6))),
                'rank_wp': item['rank_wp'],
            }
        )

    return ranked
