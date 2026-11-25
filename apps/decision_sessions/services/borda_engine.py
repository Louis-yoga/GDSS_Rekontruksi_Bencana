"""
Borda Count Voting Engine & 3-Tier Tie-Breaker Resolution.

Zona 4 / PRD §1.5 & §1.9:
- Tahap voting Borda Top-K wajib selalu ada.
- Poin Borda: Poin_d(i) = K - r + 1 untuk peringkat r in [1, K].
- Abstain = 0 poin untuk semua lokasi.
- Hierarki Tie-Breaker (3 Tingkat Deterministik):
  - Tingkat 1 (WP_SCORE)     : Nilai preferensi WP V(i) lebih tinggi menang (AC-01)
  - Tingkat 2 (KETUA_BALLOT) : Peringkat di surat suara KETUA lebih tinggi menang (AC-02)
  - Tingkat 3 (KETUA_MANUAL) : Penetapan manual oleh KETUA + alasan >= 20 char (AC-02)
"""
from typing import Dict, List, Tuple, Optional
from decimal import Decimal
from django.db import transaction
from apps.decision_sessions.models import SesiKeputusan, Location, Vote, VoteItem, Result


def compute_borda_points(
    votes: List[Vote],
    locations: List[Location],
    k: int
) -> Dict[int, int]:
    """
    Menghitung total poin Borda untuk setiap lokasi dari suara yang sah.
    Poin_d(i) = K - r + 1 jika r in [1, K], 0 jika tidak masuk Top-K atau DM abstain.
    """
    borda_totals: Dict[int, int] = {loc.id: 0 for loc in locations}

    for vote in votes:
        if vote.abstain:
            continue

        items = vote.items.all()
        for item in items:
            if 1 <= item.peringkat <= k:
                points = k - item.peringkat + 1
                borda_totals[item.lokasi_id] = borda_totals.get(item.lokasi_id, 0) + points

    return borda_totals


def resolve_group_ties(
    locations_in_group: List[Location],
    wp_result_map: Dict[int, Result],
    ketua_vote: Optional[Vote],
    k: int
) -> Tuple[List[Location], str, str]:
    """
    Menyelesaikan seri (tie-break) untuk kelompok lokasi dengan poin Borda yang sama.
    Mengembalikan: (ordered_locations, metode_tiebreak, catatan_tiebreak)
    """
    if len(locations_in_group) <= 1:
        return locations_in_group, 'NONE', ''

    # ─── Tingkat 1: Skor WP Kelompok (V_i) ──────────────────────
    # Urutkan berdasarkan skor_wp desc
    # Compare stored decimal values directly; converting to float can change ties.
    wp_values = {loc.id: wp_result_map[loc.id].skor_wp for loc in locations_in_group
                 if loc.id in wp_result_map}
    sorted_by_wp = sorted(
        locations_in_group,
        key=lambda loc: (-wp_values.get(loc.id, Decimal('0')), loc.kode, loc.id)
    )

    # Missing WP data cannot resolve a tie deterministically.
    if len(wp_values) == len(locations_in_group) and len(set(wp_values.values())) == len(locations_in_group):
        return (
            sorted_by_wp,
            'WP_SCORE',
            'Diselesaikan otomatis melalui Tingkat 1: Nilai preferensi Weighted Product (V_i) lebih tinggi.'
        )

    # ─── Tingkat 2: Surat Suara Ketua BPBD ─────────────────────
    # Jika WP skor sama, cek surat suara KETUA
    if ketua_vote and not ketua_vote.abstain:
        ketua_rank_map = {item.lokasi_id: item.peringkat for item in ketua_vote.items.all()}

        # Nilai peringkat: semakin kecil r semakin baik; jika tidak dicantumkan diberi peringkat besar (k + 99)
        def _ketua_priority(loc):
            r = ketua_rank_map.get(loc.id, k + 99)
            return (r, -wp_values.get(loc.id, Decimal('0')), loc.kode, loc.id)

        sorted_by_ketua = sorted(locations_in_group, key=_ketua_priority)
        ketua_ranks = [ketua_rank_map.get(loc.id, k + 99) for loc in locations_in_group]

        if len(set(ketua_ranks)) == len(locations_in_group):
            return (
                sorted_by_ketua,
                'KETUA_BALLOT',
                'Diselesaikan otomatis melalui Tingkat 2: Peringkat pada surat suara Ketua BPBD lebih tinggi.'
            )

    # ─── Tingkat 3: Memerlukan Penetapan Manual Ketua ────────────
    return (
        sorted_by_wp,
        'KETUA_MANUAL',
        'Memerlukan penetapan manual oleh Ketua BPBD (Tingkat 3).'
    )


@transaction.atomic
def execute_borda_and_ranking(sesi: SesiKeputusan) -> List[Result]:
    """
    Menjalankan proses komputasi Borda dan resolusi tie-break:
    1. Ambil seluruh suara yang terkumpul
    2. Hitung total poin Borda untuk semua lokasi
    3. Kelompokkan lokasi berdasarkan poin Borda yang sama
    4. Terapkan hierarki tie-breaker untuk tiap kelompok seri
    5. Simpan poin_borda, rank_final, metode_tiebreak ke tabel results
    """
    locations = list(sesi.locations.all().order_by('id'))
    if not locations:
        return []
    votes = list(sesi.votes.filter(sesi=sesi).prefetch_related('items'))
    k = sesi.nilai_k

    # Ambil map skor WP yang telah dihitung sebelumnya
    results_qs = Result.objects.filter(sesi=sesi)
    wp_result_map = {r.lokasi_id: r for r in results_qs}
    missing = [loc.kode for loc in locations if loc.id not in wp_result_map]
    if missing:
        raise ValueError('Hasil WP belum lengkap untuk lokasi: ' + ', '.join(missing))

    # Cari vote milik kursi KETUA
    ketua_vote = next((v for v in votes if v.seat.kode == 'KETUA'), None)

    # Hitung poin Borda
    borda_points = compute_borda_points(votes, locations, k)

    # Kelompokkan lokasi berdasarkan perolehan poin Borda (descending)
    unique_points = sorted(set(borda_points.values()), reverse=True)

    ordered_final_results: List[Tuple[Location, int, str, str]] = []

    for pt in unique_points:
        locs_with_pt = [loc for loc in locations if borda_points[loc.id] == pt]
        if len(locs_with_pt) == 1:
            ordered_final_results.append((locs_with_pt[0], pt, 'NONE', ''))
        else:
            # Terjadi seri -> selesaikan dengan tie-breaker
            resolved_locs, method, note = resolve_group_ties(
                locs_with_pt, wp_result_map, ketua_vote, k
            )
            for rloc in resolved_locs:
                ordered_final_results.append((rloc, pt, method, note))

    # Perbarui tabel results
    updated_results = []
    for rank, (loc, pt, method, note) in enumerate(ordered_final_results, start=1):
        res, _ = Result.objects.update_or_create(
            sesi=sesi,
            lokasi=loc,
            defaults={
                'poin_borda': pt,
                'rank_final': rank,
                'metode_tiebreak': method,
                'catatan_tiebreak': note,
            }
        )
        updated_results.append(res)

    return updated_results
