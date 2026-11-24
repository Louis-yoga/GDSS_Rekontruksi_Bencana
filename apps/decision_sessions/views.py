"""
Views for Decision Sessions — Fase 2 & Fase 3:
- Home Dashboard (L2)
- Form Penilaian DM Interaktif (L4)
- Autosave API (Debounce 2s, Optimistic Locking, AC-13)
- Submit Penilaian (Validasi Matriks 1-5 AC-08, Idempotency AC-05)
- Hasil Awal & Divergensi (L5, Blind Scoring AC-04)
- Transisi T1 & T2-B, Buka Kunci Penilaian (AC-07, AC-09)
"""
import json
import secrets
from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse, HttpResponseBadRequest
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_protect
from django.utils import timezone
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError

from apps.accounts.rbac import requires_seat, requires_any_dm
from apps.decision_sessions.models import (
    SesiKeputusan, Location, Criteria, Assessment, AssessmentScore, Result, Vote, VoteItem
)
from apps.decision_sessions.services.transitions import (
    transition_t1_open_assessment,
    transition_t2a_auto,
    transition_t2b_force_close,
    unlock_assessment,
    transition_t3_open_voting,
    transition_t4a_auto,
    transition_t4b_force_close_voting
)
from apps.decision_sessions.services.wp_engine import (
    normalize_weights,
    compute_individual_wp,
    calculate_divergence
)
from apps.audit.models import EventLog


def _get_client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '127.0.0.1')


@login_required(login_url='/auth/login/')
def home_view(request):
    """L2 — Beranda Sesi Keputusan"""
    sesi_list = SesiKeputusan.objects.all().order_by('-id')
    user = request.user
    active_seat = getattr(user, 'active_seat', None)

    sessions_data = []
    for s in sesi_list:
        assessments_count = s.assessments.filter(status='SUBMITTED').count()
        # Cek status penilaian kursi user ini
        my_assessment = None
        if active_seat:
            my_assessment = s.assessments.filter(seat=active_seat).first()

        sessions_data.append({
            'sesi': s,
            'submitted_dm_count': assessments_count,
            'my_assessment': my_assessment,
        })

    return render(request, 'sessions/home_stub.html', {
        'sessions_data': sessions_data,
        'active_seat': active_seat,
    })


@require_http_methods(['POST'])
@requires_seat('KETUA')
def open_assessment_view(request, session_id):
    """Transisi T1: Buka Sesi Penilaian (DRAFT -> PENILAIAN)"""
    sesi = get_object_or_404(SesiKeputusan, pk=session_id)
    try:
        transition_t1_open_assessment(sesi, request.user, request)
        messages.success(request, f"Sesi '{sesi.judul}' berhasil dibuka untuk tahap PENILAIAN.")
    except (ValidationError, PermissionDenied) as e:
        messages.error(request, str(e))
    return redirect('session-list')


@require_http_methods(['GET'])
@requires_seat('KETUA', 'PU', 'SOSIAL')
def assessment_view(request, session_id):
    """
    L4 — Formulir Penilaian DM
    - Menampilkan grid matriks lokasi × kriteria
    - Blind scoring: Hanya menampilkan skor milik kursi sendiri
    """
    sesi = get_object_or_404(SesiKeputusan, pk=session_id)
    if sesi.status != 'PENILAIAN':
        messages.warning(request, f"Sesi saat ini berstatus {sesi.status}, bukan tahap PENILAIAN.")
        return redirect('session-list')

    seat = request.user.active_seat
    assessment, created = Assessment.objects.get_or_create(
        sesi=sesi,
        seat=seat,
        defaults={'user': request.user, 'status': 'DRAFT', 'versi_draf': 1}
    )

    locations = sesi.locations.all().order_by('id')
    criteria = sesi.criteria.all().order_by('id')

    # Ambil skor tersimpan milik kursi ini saja (Blind Scoring AC-04)
    scores = AssessmentScore.objects.filter(assessment=assessment)
    score_dict = {(sc.lokasi_id, sc.kriteria_id): sc.skor for sc in scores}

    # Buat atau ambil token idempotency untuk submit
    token_key = f'idempotency_token_assessment_{sesi.id}'
    idempotency_token = secrets.token_hex(16)
    request.session[token_key] = idempotency_token

    return render(request, 'sessions/assessment_form.html', {
        'sesi': sesi,
        'seat': seat,
        'assessment': assessment,
        'locations': locations,
        'criteria': criteria,
        'score_dict': score_dict,
        'idempotency_token': idempotency_token,
    })


@require_http_methods(['POST'])
@requires_seat('KETUA', 'PU', 'SOSIAL')
def autosave_assessment_api(request, session_id):
    """
    API Autosave Penilaian (Debounce 2s, Optimistic Locking AC-13)
    Payload JSON: {
        'versi_draf': int,
        'scores': [{'lokasi_id': int, 'kriteria_id': int, 'skor': int}]
    }
    """
    sesi = get_object_or_404(SesiKeputusan, pk=session_id)
    if sesi.status != 'PENILAIAN':
        return JsonResponse({'error': 'Sesi tidak dalam status PENILAIAN'}, status=400)

    seat = request.user.active_seat
    try:
        assessment = Assessment.objects.get(sesi=sesi, seat=seat)
    except Assessment.DoesNotExist:
        return JsonResponse({'error': 'Assessment belum diinisiasi'}, status=404)

    if assessment.status != 'DRAFT':
        return JsonResponse({'error': 'Penilaian sudah dikunci/disubmit'}, status=403)

    try:
        body = json.loads(request.body.decode('utf-8'))
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Format JSON tidak valid'}, status=400)

    client_version = body.get('versi_draf')
    if client_version != assessment.versi_draf:
        return JsonResponse({
            'error': 'Konflik versi draf. Halaman sedang diedit di sesi lain.',
            'server_version': assessment.versi_draf
        }, status=409)

    scores_data = body.get('scores', [])
    for item in scores_data:
        loc_id = item.get('lokasi_id')
        crit_id = item.get('kriteria_id')
        skor = item.get('skor')

        if skor is not None and isinstance(skor, int) and 1 <= skor <= 5:
            AssessmentScore.objects.update_or_create(
                assessment=assessment,
                lokasi_id=loc_id,
                kriteria_id=crit_id,
                defaults={'skor': skor}
            )

    assessment.versi_draf += 1
    assessment.save(update_fields=['versi_draf'])

    return JsonResponse({
        'status': 'ok',
        'versi_draf': assessment.versi_draf,
        'saved_at': timezone.localtime().strftime('%H:%M WIB'),
    })


@require_http_methods(['POST'])
@requires_seat('KETUA', 'PU', 'SOSIAL')
def submit_assessment_view(request, session_id):
    """
    Submit Penilaian Kursi DM (AC-08, AC-05)
    - Validasi kelengkapan seluruh sel 1-5 (AC-08)
    - Verifikasi kata sandi konfirmasi
    - Idempotency token check
    - Auto-transisi T2-A bila 3/3 kursi telah submit
    """
    sesi = get_object_or_404(SesiKeputusan, pk=session_id)
    if sesi.status != 'PENILAIAN':
        return HttpResponseBadRequest("Sesi tidak dalam tahap PENILAIAN.")

    seat = request.user.active_seat
    assessment, created = Assessment.objects.get_or_create(
        sesi=sesi,
        seat=seat,
        defaults={'user': request.user, 'status': 'DRAFT', 'versi_draf': 1}
    )

    # Idempotency check: jika sudah SUBMITTED
    if assessment.status == 'SUBMITTED':
        messages.info(request, "Penilaian Anda telah disubmit sebelumnya.")
        return redirect('assessment-form', session_id=sesi.id)

    # Validasi kata sandi
    password = request.POST.get('password_konfirmasi', '')
    if not request.user.check_password(password):
        messages.error(request, "Kata sandi konfirmasi tidak valid.")
        return redirect('assessment-form', session_id=sesi.id)

    # Simpan skor dari POST form jika ada input sebelum submit
    locations = list(sesi.locations.all())
    criteria = list(sesi.criteria.all())

    for loc in locations:
        for crit in criteria:
            param_name = f'score_{loc.id}_{crit.id}'
            val = request.POST.get(param_name)
            if val and val.isdigit():
                skor_int = int(val)
                if 1 <= skor_int <= 5:
                    AssessmentScore.objects.update_or_create(
                        assessment=assessment,
                        lokasi_id=loc.id,
                        kriteria_id=crit.id,
                        defaults={'skor': skor_int}
                    )

    # Validasi kelengkapan matriks 1-5 (AC-08)
    scores = AssessmentScore.objects.filter(assessment=assessment)
    score_map = {(sc.lokasi_id, sc.kriteria_id): sc.skor for sc in scores}

    missing_cells = []
    for loc in locations:
        for crit in criteria:
            skor = score_map.get((loc.id, crit.id))
            if skor is None or skor < 1 or skor > 5:
                missing_cells.append(f"{loc.kode} - {crit.kode}")

    if missing_cells:
        # HTTP 422 Unprocessable Entity
        msg = f"Matriks belum lengkap. Terdapat {len(missing_cells)} sel yang belum diisi (1-5): {', '.join(missing_cells[:5])}..."
        messages.error(request, msg)
        response = render(request, 'sessions/assessment_form.html', {
            'sesi': sesi,
            'seat': seat,
            'assessment': assessment,
            'locations': locations,
            'criteria': criteria,
            'score_dict': score_map,
            'missing_cells': missing_cells,
        }, status=422)
        return response

    # Seluruh sel lengkap -> Tandai SUBMITTED
    assessment.status = 'SUBMITTED'
    assessment.submitted_at = timezone.now()
    assessment.save(update_fields=['status', 'submitted_at'])

    EventLog.objects.create(
        sesi=sesi,
        user=request.user,
        seat_kode=seat.kode,
        aksi='ASSESSMENT_SUBMITTED',
        ip_address=_get_client_ip(request),
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:255],
        detail={'total_cells': len(locations) * len(criteria)}
    )

    # Cek auto-transisi T2-A bila semua kursi telah submit
    transitioned = transition_t2a_auto(sesi, request)
    if transitioned:
        messages.success(request, "Seluruh kursi telah submit penilaian! Sesi beralih otomatis ke HASIL_AWAL.")
        return redirect('results-view', session_id=sesi.id)

    messages.success(request, "Penilaian Anda berhasil disubmit dan dikunci.")
    return redirect('assessment-form', session_id=sesi.id)


@require_http_methods(['GET'])
@requires_seat('KETUA', 'PU', 'SOSIAL')
def results_view(request, session_id):
    """
    L5 — Hasil Awal & Divergensi
    - Peringkat kelompok Weighted Product (V_i)
    - Kartu peringkat individual DM (Hanya urutan lokasi, TANPA SKOR MENTAH — Blind Scoring AC-04)
    - Klasifikasi divergensi: Rendah, Sedang, Tinggi
    """
    sesi = get_object_or_404(SesiKeputusan, pk=session_id)
    if sesi.status in ['DRAFT', 'PENILAIAN']:
        messages.warning(request, "Hasil awal belum tersedia sebelum tahap penilaian selesai.")
        return redirect('session-list')

    results = Result.objects.filter(sesi=sesi).select_related('lokasi').order_by('rank_wp')
    locations = list(sesi.locations.all().order_by('id'))
    criteria = list(sesi.criteria.all().order_by('id'))
    norm_weights = normalize_weights(criteria)

    submitted_assessments = list(sesi.assessments.filter(status='SUBMITTED').select_related('seat'))
    divergence = calculate_divergence(submitted_assessments, locations, criteria, norm_weights)

    # Hitung urutan peringkat individual per kursi (Zona 5: Hanya urutan nama lokasi, tanpa angka skor)
    individual_rankings = {}
    loc_map = {loc.id: loc for loc in locations}
    for ass in submitted_assessments:
        sorted_loc_ids = compute_individual_wp(ass, locations, criteria, norm_weights)
        individual_rankings[ass.seat.kode] = [
            {'rank': r, 'lokasi': loc_map[lid]} for r, lid in enumerate(sorted_loc_ids, start=1)
        ]

    return render(request, 'sessions/initial_results.html', {
        'sesi': sesi,
        'results': results,
        'divergence': divergence,
        'individual_rankings': individual_rankings,
        'user_seat': request.user.active_seat,
    })


@require_http_methods(['POST'])
@requires_seat('KETUA')
def force_close_assessment_view(request, session_id):
    """Transisi T2-B: Tutup Paksa Penilaian (Kuorum 2/3 - AC-09)"""
    sesi = get_object_or_404(SesiKeputusan, pk=session_id)
    alasan = request.POST.get('alasan', '')
    password = request.POST.get('password_konfirmasi', '')

    try:
        transition_t2b_force_close(sesi, request.user, alasan, password, request)
        messages.success(request, "Penilaian berhasil ditutup paksa dengan kuorum parsial.")
        return redirect('results-view', session_id=sesi.id)
    except (ValidationError, PermissionDenied) as e:
        messages.error(request, str(e))
        return redirect('session-list')


@require_http_methods(['POST'])
@requires_seat('KETUA')
def unlock_assessment_view(request, session_id, seat_code):
    """Buka Kunci Penilaian (AC-07)"""
    sesi = get_object_or_404(SesiKeputusan, pk=session_id)
    alasan = request.POST.get('alasan', '')
    password = request.POST.get('password_konfirmasi', '')

    try:
        unlock_assessment(sesi, request.user, seat_code, alasan, password, request)
        messages.success(request, f"Penilaian kursi {seat_code} berhasil dibuka kembali menjadi DRAFT.")
    except (ValidationError, PermissionDenied) as e:
        messages.error(request, str(e))
    return redirect('session-list')


@require_http_methods(['POST'])
@requires_seat('KETUA')
def open_voting_view(request, session_id):
    """Transisi T3: Buka Sesi Konsensus / Voting Borda (HASIL_AWAL -> VOTING)"""
    sesi = get_object_or_404(SesiKeputusan, pk=session_id)
    try:
        transition_t3_open_voting(sesi, request.user, request)
        messages.success(request, f"Sesi voting untuk '{sesi.judul}' berhasil dibuka.")
        return redirect('voting-view', session_id=sesi.id)
    except (ValidationError, PermissionDenied) as e:
        messages.error(request, str(e))
        return redirect('session-list')


@require_http_methods(['GET'])
@requires_seat('KETUA', 'PU', 'SOSIAL')
def voting_view(request, session_id):
    """
    L6 — Ruang Konsensus / Voting Borda
    - Formulir pemilihan urutan peringkat 1 s.d. K
    - Default formulir kosong (anti-bias)
    - Tombol bantu: 1-klik salin rekomendasi WP
    - Opsi Abstain
    - Sealed ballot: DM lain tidak dapat melihat pilihan kursi ini sebelum FINAL (Zona 5)
    """
    sesi = get_object_or_404(SesiKeputusan, pk=session_id)
    if sesi.status not in ['VOTING', 'PENETAPAN', 'FINAL']:
        messages.warning(request, f"Sesi saat ini berstatus {sesi.status}, bukan tahap VOTING.")
        return redirect('session-list')

    seat = request.user.active_seat
    my_vote = Vote.objects.filter(sesi=sesi, seat=seat).first()

    # Daftar kandidat lokasi dan rekomendasi WP
    locations = sesi.locations.all().order_by('id')
    wp_results = Result.objects.filter(sesi=sesi).select_related('lokasi').order_by('rank_wp')

    # Buat token idempotency (AC-05)
    token_key = f'idempotency_token_vote_{sesi.id}'
    idempotency_token = secrets.token_hex(16)
    request.session[token_key] = idempotency_token

    # Ambil pilihan jika sudah pernah memilih
    selected_rank_map = {}
    if my_vote and not my_vote.abstain:
        for it in my_vote.items.all():
            selected_rank_map[it.peringkat] = it.lokasi_id

    # Siapkan range K untuk dropdown peringkat (1 s.d. K)
    k_range = list(range(1, sesi.nilai_k + 1))

    return render(request, 'sessions/voting_form.html', {
        'sesi': sesi,
        'seat': seat,
        'my_vote': my_vote,
        'locations': locations,
        'wp_results': wp_results,
        'idempotency_token': idempotency_token,
        'selected_rank_map': selected_rank_map,
        'k_range': k_range,
    })


@require_http_methods(['POST'])
@requires_seat('KETUA', 'PU', 'SOSIAL')
def submit_vote_view(request, session_id):
    """
    Submit Surat Suara Voting Borda (AC-05, AC-01)
    - Idempotency token check (mencegah double-click / submit berulang)
    - Verifikasi kata sandi konfirmasi
    - Opsi Abstain vs Pilihan Tepat K lokasi unik
    - Auto-transisi T4-A bila seluruh 3 DM telah memberikan suara
    """
    sesi = get_object_or_404(SesiKeputusan, pk=session_id)
    if sesi.status != 'VOTING':
        return HttpResponseBadRequest("Sesi tidak dalam tahap VOTING.")

    seat = request.user.active_seat

    # Cek apakah sudah pernah vote (Idempotency AC-05)
    if Vote.objects.filter(sesi=sesi, seat=seat).exists():
        messages.info(request, "Suara Anda sudah tercatat sebelumnya.")
        return redirect('voting-view', session_id=sesi.id)

    # Validasi idempotency token
    token_key = f'idempotency_token_vote_{sesi.id}'
    submitted_token = request.POST.get('idempotency_token')
    session_token = request.session.pop(token_key, None)

    if not submitted_token or submitted_token != session_token:
        # Jika token hilang atau tidak cocok (misal klik beruntun), abaikan request duplikat
        messages.warning(request, "Permintaan duplikat terdeteksi. Suara Anda sedang diproses.")
        return redirect('voting-view', session_id=sesi.id)

    # Validasi kata sandi
    password = request.POST.get('password_konfirmasi', '')
    if not request.user.check_password(password):
        messages.error(request, "Kata sandi konfirmasi tidak valid.")
        return redirect('voting-view', session_id=sesi.id)

    is_abstain = request.POST.get('abstain') == 'true'

    if is_abstain:
        Vote.objects.create(
            sesi=sesi,
            seat=seat,
            user=request.user,
            abstain=True
        )
    else:
        k = sesi.nilai_k
        chosen_loc_ids = []
        for r in range(1, k + 1):
            val = request.POST.get(f'rank_{r}')
            if not val or not val.isdigit():
                messages.error(request, f"Peringkat ke-{r} wajib dipilih.")
                return redirect('voting-view', session_id=sesi.id)
            loc_id = int(val)
            if loc_id in chosen_loc_ids:
                messages.error(request, "Setiap lokasi hanya boleh dipilih pada satu peringkat (dilarang duplikat).")
                return redirect('voting-view', session_id=sesi.id)
            chosen_loc_ids.append(loc_id)

        # Buat objek Vote dan VoteItem
        vote = Vote.objects.create(
            sesi=sesi,
            seat=seat,
            user=request.user,
            abstain=False
        )
        for r, loc_id in enumerate(chosen_loc_ids, start=1):
            VoteItem.objects.create(
                vote=vote,
                peringkat=r,
                lokasi_id=loc_id
            )

    EventLog.objects.create(
        sesi=sesi,
        user=request.user,
        seat_kode=seat.kode,
        aksi='VOTE_SUBMITTED',
        ip_address=_get_client_ip(request),
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:255],
        detail={'abstain': is_abstain}
    )

    # Cek auto-transisi T4-A bila semua kursi telah memberikan suara
    transitioned = transition_t4a_auto(sesi, request)
    if transitioned:
        messages.success(request, "Seluruh kursi telah memilih! Sesi beralih otomatis ke tahap PENETAPAN.")
        return redirect('session-list')

    messages.success(request, "Surat suara Anda berhasil dikirim dan disegel.")
    return redirect('voting-view', session_id=sesi.id)


@require_http_methods(['POST'])
@requires_seat('KETUA')
def force_close_voting_view(request, session_id):
    """Transisi T4-B: Tutup Paksa Voting (AC-10)"""
    sesi = get_object_or_404(SesiKeputusan, pk=session_id)
    alasan = request.POST.get('alasan', '')
    password = request.POST.get('password_konfirmasi', '')

    try:
        transition_t4b_force_close_voting(sesi, request.user, alasan, password, request)
        messages.success(request, "Voting berhasil ditutup paksa. Sesi beralih ke tahap PENETAPAN.")
        return redirect('session-list')
    except (ValidationError, PermissionDenied) as e:
        messages.error(request, str(e))
        return redirect('session-list')
