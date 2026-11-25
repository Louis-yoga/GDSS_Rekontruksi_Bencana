"""
State Machine Transitions & Guard Conditions for SesiKeputusan.

Menerapkan transisi status deterministik:
- T1   : DRAFT -> PENILAIAN (hanya KETUA, guard lengkap)
- T2-A : PENILAIAN -> HASIL_AWAL (otomatis saat 3/3 kursi submit)
- T2-B : PENILAIAN -> HASIL_AWAL (tutup paksa KETUA, guard kuorum 2/3 & deadline)
- Unlock Assessment : Mengembalikan assessment kursi ke DRAFT (hanya KETUA + alasan)
"""
import hashlib
import json
from django.utils import timezone
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from apps.decision_sessions.models import SesiKeputusan, Location, Criteria, Assessment, Result
from apps.audit.models import EventLog
from apps.decision_sessions.services.wp_engine import execute_wp_engine


def _get_client_ip(request):
    if not request:
        return '127.0.0.1'
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '127.0.0.1')


def _get_user_agent(request):
    if not request:
        return 'system'
    return request.META.get('HTTP_USER_AGENT', '')[:255]


def compute_config_hash(sesi: SesiKeputusan) -> str:
    """Menghitung SHA-256 hash konfigurasi lokasi, kriteria, dan bobot untuk integritas AC-14."""
    hasher = hashlib.sha256()
    hasher.update(f"SESI:{sesi.id}:v{sesi.versi}:K={sesi.nilai_k}".encode('utf-8'))
    for loc in sesi.locations.order_by('id'):
        hasher.update(f"LOC:{loc.kode}:{loc.nama}".encode('utf-8'))
    for cr in sesi.criteria.order_by('id'):
        hasher.update(f"CRIT:{cr.kode}:{cr.tipe}:{cr.bobot}".encode('utf-8'))
    return hasher.hexdigest()


@transaction.atomic
def transition_t1_open_assessment(sesi: SesiKeputusan, user, request=None) -> None:
    """
    Transisi T1: DRAFT -> PENILAIAN
    Guard:
    - Status sesi saat ini adalah DRAFT
    - Pengguna adalah pemegang kursi KETUA
    - Lokasi >= 2
    - Kriteria >= 3
    - Total bobot > 0
    - Nilai K valid (1 <= K <= jumlah lokasi)
    """
    if sesi.status != 'DRAFT':
        raise ValidationError(f"Sesi tidak dapat dibuka: status saat ini {sesi.status}")

    active_seat = getattr(user, 'active_seat', None)
    if not active_seat or active_seat.kode != 'KETUA':
        raise PermissionDenied("Hanya Ketua BPBD yang berwenang membuka sesi penilaian.")

    locations_count = sesi.locations.count()
    if locations_count < 2:
        raise ValidationError("Sesi minimal harus memiliki 2 lokasi kandidat.")

    criteria_count = sesi.criteria.count()
    if criteria_count < 3:
        raise ValidationError("Sesi minimal harus memiliki 3 kriteria penilaian.")

    total_bobot = sum(c.bobot for c in sesi.criteria.all())
    if total_bobot <= 0:
        raise ValidationError("Total bobot kriteria harus lebih besar dari 0.")

    if not (1 <= sesi.nilai_k <= locations_count):
        raise ValidationError(f"Nilai K ({sesi.nilai_k}) harus berada antara 1 dan {locations_count}.")

    # Efek transisi
    now = timezone.now()
    sesi.config_hash = compute_config_hash(sesi)
    sesi.status = 'PENILAIAN'
    sesi.deadline_aktif = now + timezone.timedelta(minutes=sesi.durasi_t1_menit)
    sesi.save(update_fields=['status', 'config_hash', 'deadline_aktif', 'updated_at'])

    # Catat audit trail
    EventLog.objects.create(
        sesi=sesi,
        user=user,
        seat_kode='KETUA',
        aksi='STATE_T1',
        status_dari='DRAFT',
        status_ke='PENILAIAN',
        ip_address=_get_client_ip(request),
        user_agent=_get_user_agent(request),
        detail={
            'config_hash': sesi.config_hash,
            'deadline_aktif': sesi.deadline_aktif.isoformat(),
            'locations_count': locations_count,
            'criteria_count': criteria_count,
        }
    )


@transaction.atomic
def transition_t2a_auto(sesi: SesiKeputusan, request=None) -> bool:
    """
    Transisi T2-A: PENILAIAN -> HASIL_AWAL (Otomatis saat 3/3 kursi DM submit)
    """
    if sesi.status != 'PENILAIAN':
        return False

    submitted_count = sesi.assessments.filter(status='SUBMITTED').count()
    if submitted_count < 3:
        return False

    # Jalankan engine WP
    execute_wp_engine(sesi)

    sesi.status = 'HASIL_AWAL'
    sesi.kuorum_parsial = False
    sesi.save(update_fields=['status', 'kuorum_parsial', 'updated_at'])

    EventLog.objects.create(
        sesi=sesi,
        user=None,
        seat_kode=None,
        aksi='STATE_T2_AUTO',
        status_dari='PENILAIAN',
        status_ke='HASIL_AWAL',
        ip_address=_get_client_ip(request),
        user_agent=_get_user_agent(request),
        detail={'submitted_count': 3, 'mode': 'kuorum_lengkap'}
    )
    return True


@transaction.atomic
def transition_t2b_force_close(
    sesi: SesiKeputusan,
    user,
    alasan: str,
    password_konfirmasi: str,
    request=None
) -> None:
    """
    Transisi T2-B: Tutup Paksa Penilaian (Kuorum 2/3 - AC-09)
    Guard:
    - Status sesi saat ini adalah PENILAIAN
    - Pengguna adalah pemegang kursi KETUA
    - now() >= deadline_aktif
    - Kursi submit >= 2 (Kuorum minimal)
    - Jika hanya 1 kursi submit -> DITOLAK KERAS (Guard negatif AC-09)
    - Panjang alasan >= 20 karakter
    - Kata sandi konfirmasi valid
    """
    if sesi.status != 'PENILAIAN':
        raise ValidationError(f"Sesi tidak dapat ditutup paksa: status saat ini {sesi.status}")

    active_seat = getattr(user, 'active_seat', None)
    if not active_seat or active_seat.kode != 'KETUA':
        raise PermissionDenied("Hanya Ketua BPBD yang berwenang menutup paksa penilaian.")

    if not user.check_password(password_konfirmasi):
        raise ValidationError("Kata sandi konfirmasi tidak valid.")

    if not alasan or len(alasan.strip()) < 20:
        raise ValidationError("Alasan penutupan paksa wajib diisi minimal 20 karakter.")

    now = timezone.now()
    if sesi.deadline_aktif and now < sesi.deadline_aktif:
        raise ValidationError("Penutupan paksa tidak dapat dilakukan sebelum tenggat waktu terlewati.")

    submitted_count = sesi.assessments.filter(status='SUBMITTED').count()
    if submitted_count < 2:
        raise PermissionDenied(
            f"Kuorum 2/3 tidak terpenuhi: Hanya {submitted_count} kursi yang submit. Minimal 2 kursi diperlukan."
        )

    # Jalankan engine WP dengan kuorum parsial
    execute_wp_engine(sesi)

    sesi.status = 'HASIL_AWAL'
    sesi.kuorum_parsial = True
    sesi.alasan_status = alasan.strip()
    sesi.save(update_fields=['status', 'kuorum_parsial', 'alasan_status', 'updated_at'])

    EventLog.objects.create(
        sesi=sesi,
        user=user,
        seat_kode='KETUA',
        aksi='STATE_T2_FORCE',
        status_dari='PENILAIAN',
        status_ke='HASIL_AWAL',
        ip_address=_get_client_ip(request),
        user_agent=_get_user_agent(request),
        detail={
            'submitted_count': submitted_count,
            'alasan': alasan.strip(),
            'kuorum_parsial': True,
        }
    )


@transaction.atomic
def unlock_assessment(
    sesi: SesiKeputusan,
    user,
    target_seat_code: str,
    alasan: str,
    password_konfirmasi: str,
    request=None
) -> None:
    """
    Membuka kembali kunci penilaian DM yang telah disubmit (AC-07)
    Guard:
    - Status sesi saat ini adalah PENILAIAN
    - Pengguna adalah pemegang kursi KETUA
    - Alasan minimal 20 karakter
    - Password konfirmasi valid
    """
    if sesi.status != 'PENILAIAN':
        raise ValidationError("Buka kunci penilaian hanya dapat dilakukan pada tahap PENILAIAN.")

    active_seat = getattr(user, 'active_seat', None)
    if not active_seat or active_seat.kode != 'KETUA':
        raise PermissionDenied("Hanya Ketua BPBD yang berwenang membuka kunci penilaian.")

    if not user.check_password(password_konfirmasi):
        raise ValidationError("Kata sandi konfirmasi tidak valid.")

    if not alasan or len(alasan.strip()) < 20:
        raise ValidationError("Alasan buka kunci wajib diisi minimal 20 karakter.")

    try:
        assessment = sesi.assessments.get(seat__kode=target_seat_code)
    except Assessment.DoesNotExist:
        raise ValidationError(f"Penilaian untuk kursi {target_seat_code} tidak ditemukan.")

    assessment.status = 'DRAFT'
    assessment.versi_draf += 1
    assessment.save(update_fields=['status', 'versi_draf'])

    EventLog.objects.create(
        sesi=sesi,
        user=user,
        seat_kode='KETUA',
        aksi='ASSESSMENT_UNLOCKED',
        ip_address=_get_client_ip(request),
        user_agent=_get_user_agent(request),
        detail={
            'target_seat': target_seat_code,
            'alasan': alasan.strip(),
            'new_versi_draf': assessment.versi_draf,
        }
    )


@transaction.atomic
def transition_t3_open_voting(sesi: SesiKeputusan, user, request=None) -> None:
    """
    Transisi T3: HASIL_AWAL -> VOTING
    Guard:
    - Status sesi saat ini adalah HASIL_AWAL
    - Pengguna adalah pemegang kursi KETUA
    """
    if sesi.status != 'HASIL_AWAL':
        raise ValidationError(f"Sesi tidak dapat dibuka untuk voting: status saat ini {sesi.status}")

    active_seat = getattr(user, 'active_seat', None)
    if not active_seat or active_seat.kode != 'KETUA':
        raise PermissionDenied("Hanya Ketua BPBD yang berwenang membuka sesi voting.")

    now = timezone.now()
    sesi.status = 'VOTING'
    sesi.deadline_aktif = now + timezone.timedelta(minutes=sesi.durasi_t2_menit)
    sesi.save(update_fields=['status', 'deadline_aktif', 'updated_at'])

    EventLog.objects.create(
        sesi=sesi,
        user=user,
        seat_kode='KETUA',
        aksi='STATE_T3',
        status_dari='HASIL_AWAL',
        status_ke='VOTING',
        ip_address=_get_client_ip(request),
        user_agent=_get_user_agent(request),
        detail={
            'deadline_aktif': sesi.deadline_aktif.isoformat(),
            'durasi_menit': sesi.durasi_t2_menit,
        }
    )


@transaction.atomic
def transition_t4a_auto(sesi: SesiKeputusan, request=None) -> bool:
    """
    Transisi T4-A: VOTING -> PENETAPAN (Otomatis saat 3/3 kursi DM telah memberikan suara)
    """
    if sesi.status != 'VOTING':
        return False

    votes_count = sesi.votes.count()
    if votes_count < 3:
        return False

    from apps.decision_sessions.services.borda_engine import execute_borda_and_ranking
    execute_borda_and_ranking(sesi)

    sesi.status = 'PENETAPAN'
    sesi.kuorum_parsial = False
    sesi.save(update_fields=['status', 'kuorum_parsial', 'updated_at'])

    EventLog.objects.create(
        sesi=sesi,
        user=None,
        seat_kode=None,
        aksi='STATE_T4_AUTO',
        status_dari='VOTING',
        status_ke='PENETAPAN',
        ip_address=_get_client_ip(request),
        user_agent=_get_user_agent(request),
        detail={'votes_count': 3, 'mode': 'kuorum_lengkap'}
    )
    return True


@transaction.atomic
def transition_t4b_force_close_voting(
    sesi: SesiKeputusan,
    user,
    alasan: str,
    password_konfirmasi: str,
    request=None
) -> None:
    """
    Transisi T4-B: Tutup Paksa Voting (AC-10)
    Guard:
    - Status sesi saat ini adalah VOTING
    - Pengguna adalah pemegang kursi KETUA
    - now() > deadline_aktif (AC-10: DILARANG tutup voting sebelum tenggat waktu terlewati)
    - Suara masuk >= 2 (Kuorum minimal 2/3)
    - KETUA wajib sudah memberikan suara
    - Alasan minimal 20 karakter
    - Kata sandi konfirmasi valid
    """
    if sesi.status != 'VOTING':
        raise ValidationError(f"Sesi tidak dapat ditutup: status saat ini {sesi.status}")

    active_seat = getattr(user, 'active_seat', None)
    if not active_seat or active_seat.kode != 'KETUA':
        raise PermissionDenied("Hanya Ketua BPBD yang berwenang menutup paksa voting.")

    if not user.check_password(password_konfirmasi):
        raise ValidationError("Kata sandi konfirmasi tidak valid.")

    if not alasan or len(alasan.strip()) < 20:
        raise ValidationError("Alasan penutupan paksa voting wajib diisi minimal 20 karakter.")

    now = timezone.now()
    if not sesi.deadline_aktif or now <= sesi.deadline_aktif:
        raise PermissionDenied(
            "Larangan AC-10: Voting tidak dapat ditutup sebelum batas waktu/deadline berakhir."
        )

    votes = sesi.votes.all()
    if votes.count() < 2:
        raise PermissionDenied(
            f"Kuorum 2/3 tidak terpenuhi: Hanya {votes.count()} suara yang masuk. Minimal 2 suara diperlukan."
        )

    ketua_voted = votes.filter(seat__kode='KETUA').exists()
    if not ketua_voted:
        raise PermissionDenied(
            "Ketua BPBD wajib sudah memberikan suara sebelum melakukan penutupan paksa voting."
        )

    from apps.decision_sessions.services.borda_engine import execute_borda_and_ranking
    execute_borda_and_ranking(sesi)

    sesi.status = 'PENETAPAN'
    sesi.kuorum_parsial = True
    sesi.alasan_status = alasan.strip()
    sesi.save(update_fields=['status', 'kuorum_parsial', 'alasan_status', 'updated_at'])

    EventLog.objects.create(
        sesi=sesi,
        user=user,
        seat_kode='KETUA',
        aksi='STATE_T4_FORCE',
        status_dari='VOTING',
        status_ke='PENETAPAN',
        ip_address=_get_client_ip(request),
        user_agent=_get_user_agent(request),
        detail={
            'votes_count': votes.count(),
            'alasan': alasan.strip(),
            'kuorum_parsial': True,
        }
    )


@transaction.atomic
def extend_deadline(
    sesi: SesiKeputusan,
    user,
    alasan: str,
    password_konfirmasi: str,
    tambahan_menit: int = 360,
    request=None
) -> None:
    """
    Perpanjang Tenggat Waktu (Fase 6 — Sprint 15)
    Guard:
    - Status sesi harus PENILAIAN atau VOTING
    - Hanya pemegang kursi KETUA
    - jumlah_perpanjangan < 2 (maksimal 2 kali perpanjangan)
    - Alasan >= 20 karakter
    - Kata sandi konfirmasi valid
    """
    if sesi.status not in ['PENILAIAN', 'VOTING']:
        raise ValidationError(f"Perpanjangan tenggat hanya diizinkan pada tahap PENILAIAN atau VOTING (status saat ini: {sesi.status}).")

    active_seat = getattr(user, 'active_seat', None)
    if not active_seat or active_seat.kode != 'KETUA':
        raise PermissionDenied("Hanya Ketua BPBD yang berwenang memperpanjang tenggat waktu.")

    if not user.check_password(password_konfirmasi):
        raise ValidationError("Kata sandi konfirmasi tidak valid.")

    if sesi.jumlah_perpanjangan >= 2:
        raise ValidationError("Batas maksimal perpanjangan tenggat (2 kali) telah tercapai.")

    if not alasan or len(alasan.strip()) < 20:
        raise ValidationError("Alasan perpanjangan tenggat wajib diisi minimal 20 karakter.")

    if tambahan_menit <= 0:
        raise ValidationError("Durasi tambahan harus lebih besar dari 0 menit.")

    now = timezone.now()
    base_time = max(now, sesi.deadline_aktif) if sesi.deadline_aktif else now
    old_deadline = sesi.deadline_aktif.isoformat() if sesi.deadline_aktif else None

    sesi.deadline_aktif = base_time + timezone.timedelta(minutes=tambahan_menit)
    sesi.jumlah_perpanjangan += 1
    sesi.save(update_fields=['deadline_aktif', 'jumlah_perpanjangan', 'updated_at'])

    EventLog.objects.create(
        sesi=sesi,
        user=user,
        seat_kode='KETUA',
        aksi='DEADLINE_EXTENDED',
        ip_address=_get_client_ip(request),
        user_agent=_get_user_agent(request),
        detail={
            'tahap': sesi.status,
            'old_deadline': old_deadline,
            'new_deadline': sesi.deadline_aktif.isoformat(),
            'tambahan_menit': tambahan_menit,
            'jumlah_perpanjangan': sesi.jumlah_perpanjangan,
            'alasan': alasan.strip(),
        }
    )


def detect_deviation(sesi: SesiKeputusan) -> bool:
    """
    Mendeteksi deviasi antara Top-K Weighted Product (rank_wp)
    dan Top-K Borda Final (rank_final).
    """
    k = sesi.nilai_k
    top_wp = set(
        Result.objects.filter(sesi=sesi, rank_wp__lte=k).values_list('lokasi_id', flat=True)
    )
    top_final = set(
        Result.objects.filter(sesi=sesi, rank_final__lte=k).values_list('lokasi_id', flat=True)
    )
    if not top_wp or not top_final:
        return False
    return top_wp != top_final


def compute_final_hash(sesi: SesiKeputusan) -> str:
    """
    Menghitung SHA-256 final_hash deterministik (sorted-key canonical JSON)
    dari konfigurasi sesi dan seluruh hasil peringkat akhir.
    """
    results_data = []
    for r in Result.objects.filter(sesi=sesi).select_related('lokasi').order_by('rank_final', 'lokasi__kode'):
        results_data.append({
            'lokasi_kode': r.lokasi.kode,
            'skor_wp': str(r.skor_wp),
            'rank_wp': r.rank_wp,
            'poin_borda': r.poin_borda,
            'rank_final': r.rank_final,
            'metode_tiebreak': r.metode_tiebreak or 'NONE',
        })

    payload = {
        'sesi_id': sesi.id,
        'versi': sesi.versi,
        'nilai_k': sesi.nilai_k,
        'config_hash': sesi.config_hash or '',
        'kuorum_parsial': sesi.kuorum_parsial,
        'results': results_data,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


@transaction.atomic
def transition_t5_finalize(
    sesi: SesiKeputusan,
    user,
    alasan_deviasi: str = '',
    password_konfirmasi: str = '',
    manual_winner_id: int = None,
    catatan_tiebreak: str = '',
    request=None
) -> str:
    """
    Transisi T5: PENETAPAN -> FINAL (Hanya KETUA — Zona 1, AC-12, AC-15)
    Guard:
    - Status sesi saat ini adalah PENETAPAN (status FINAL immutable)
    - Pengguna adalah pemegang kursi KETUA
    - Kata sandi konfirmasi valid
    - Jika ada seri KETUA_MANUAL belum diselesaikan: wajib pilih pemenang + catatan >= 20 char
    - Jika Top-K Borda != Top-K WP (deviasi): wajib alasan_deviasi >= 20 char (AC-12)
    """
    if sesi.status == 'FINAL':
        raise PermissionDenied("Sesi sudah FINAL dan bersifat immutable (AC-15).")
    if sesi.status != 'PENETAPAN':
        raise ValidationError(f"Penetapan final hanya dapat dilakukan pada tahap PENETAPAN (status saat ini: {sesi.status}).")

    active_seat = getattr(user, 'active_seat', None)
    if not active_seat or active_seat.kode != 'KETUA':
        raise PermissionDenied("Hanya Ketua BPBD yang berwenang menetapkan keputusan final.")

    if not user.check_password(password_konfirmasi):
        raise ValidationError("Kata sandi konfirmasi tidak valid.")

    # Cek apakah ada tie-break KETUA_MANUAL yang belum diselesaikan
    manual_ties = list(Result.objects.filter(sesi=sesi, metode_tiebreak='KETUA_MANUAL').order_by('rank_final'))
    if manual_ties:
        if not manual_winner_id or not catatan_tiebreak or len(catatan_tiebreak.strip()) < 20:
            raise ValidationError(
                "Terdapat seri Tingkat 3 (KETUA_MANUAL). Ketua wajib memilih pemenang manual "
                "dan mengisi catatan justifikasi minimal 20 karakter."
            )
        # Naikkan rank pemenang manual ke peringkat teratas di kelompok seri tersebut
        min_rank = min(r.rank_final for r in manual_ties)
        winner_res = next((r for r in manual_ties if r.lokasi_id == int(manual_winner_id)), None)
        if not winner_res:
            raise ValidationError("Lokasi pemenang tie-break manual tidak valid.")
        others = [r for r in manual_ties if r.lokasi_id != winner_res.lokasi_id]
        ordered = [winner_res] + others
        for idx, res_obj in enumerate(ordered):
            res_obj.rank_final = min_rank + idx
            res_obj.catatan_tiebreak = f"Penetapan Manual Ketua: {catatan_tiebreak.strip()}"
            res_obj.save(update_fields=['rank_final', 'catatan_tiebreak'])

    # Deteksi deviasi (AC-12)
    is_deviated = detect_deviation(sesi)
    if is_deviated:
        if not alasan_deviasi or len(alasan_deviasi.strip()) < 20:
            raise ValidationError(
                "Terjadi deviasi antara Top-K Borda dan rekomendasi WP. "
                "Alasan deviasi wajib diisi minimal 20 karakter (AC-12)."
            )

    final_hash = compute_final_hash(sesi)
    now = timezone.now()

    sesi.status = 'FINAL'
    sesi.status_deviasi = is_deviated
    sesi.alasan_deviasi = alasan_deviasi.strip() if is_deviated else None
    sesi.final_hash = final_hash
    sesi.finalized_at = now
    sesi.save(update_fields=[
        'status', 'status_deviasi', 'alasan_deviasi', 'final_hash', 'finalized_at', 'updated_at'
    ])

    # Jika sesi ini adalah revisi dari sesi induk, tandai sesi induk menjadi DIGANTIKAN
    if sesi.parent and sesi.parent.status == 'FINAL':
        sesi.parent.status = 'DIGANTIKAN'
        sesi.parent.save(update_fields=['status', 'updated_at'])

    EventLog.objects.create(
        sesi=sesi,
        user=user,
        seat_kode='KETUA',
        aksi='STATE_T5_FINAL',
        status_dari='PENETAPAN',
        status_ke='FINAL',
        ip_address=_get_client_ip(request),
        user_agent=_get_user_agent(request),
        detail={
            'final_hash': final_hash,
            'status_deviasi': is_deviated,
            'alasan_deviasi': sesi.alasan_deviasi,
        }
    )
    return final_hash


@transaction.atomic
def transition_t6_cancel(
    sesi: SesiKeputusan,
    user,
    alasan: str,
    password_konfirmasi: str,
    request=None
) -> None:
    """
    Transisi T6: Membatalkan sesi yang belum FINAL.
    Data historis dan EventLog tetap tersimpan.
    """
    if sesi.status in ['FINAL', 'DIBATALKAN', 'DIGANTIKAN']:
        raise PermissionDenied(f"Sesi dengan status {sesi.status} tidak dapat dibatalkan.")

    active_seat = getattr(user, 'active_seat', None)
    if not active_seat or active_seat.kode != 'KETUA':
        raise PermissionDenied("Hanya Ketua BPBD yang berwenang membatalkan sesi.")

    if not user.check_password(password_konfirmasi):
        raise ValidationError("Kata sandi konfirmasi tidak valid.")

    if not alasan or len(alasan.strip()) < 20:
        raise ValidationError("Alasan pembatalan sesi wajib diisi minimal 20 karakter.")

    old_status = sesi.status
    sesi.status = 'DIBATALKAN'
    sesi.alasan_status = alasan.strip()
    sesi.save(update_fields=['status', 'alasan_status', 'updated_at'])

    EventLog.objects.create(
        sesi=sesi,
        user=user,
        seat_kode='KETUA',
        aksi='STATE_T6_CANCEL',
        status_dari=old_status,
        status_ke='DIBATALKAN',
        ip_address=_get_client_ip(request),
        user_agent=_get_user_agent(request),
        detail={'alasan': alasan.strip()}
    )


@transaction.atomic
def transition_t7_revise(
    sesi: SesiKeputusan,
    user,
    alasan: str,
    password_konfirmasi: str,
    request=None
) -> SesiKeputusan:
    """
    Transisi T7: Membuat sesi revisi v+1 (status DRAFT) dari sesi FINAL,
    menyalin konfigurasi lokasi & kriteria tanpa mengubah data sesi induk yang immutable.
    """
    if sesi.status != 'FINAL':
        raise ValidationError("Revisi hanya dapat dibuat dari sesi yang sudah berstatus FINAL.")

    active_seat = getattr(user, 'active_seat', None)
    if not active_seat or active_seat.kode != 'KETUA':
        raise PermissionDenied("Hanya Ketua BPBD yang berwenang membuat sesi revisi.")

    if not user.check_password(password_konfirmasi):
        raise ValidationError("Kata sandi konfirmasi tidak valid.")

    if not alasan or len(alasan.strip()) < 20:
        raise ValidationError("Alasan revisi sesi wajib diisi minimal 20 karakter.")

    new_sesi = SesiKeputusan.objects.create(
        judul=sesi.judul,
        status='DRAFT',
        versi=sesi.versi + 1,
        parent=sesi,
        nilai_k=sesi.nilai_k,
        durasi_t1_menit=sesi.durasi_t1_menit,
        durasi_t2_menit=sesi.durasi_t2_menit,
        sla_ketua_menit=sesi.sla_ketua_menit,
        alasan_status=alasan.strip(),
    )

    for loc in sesi.locations.order_by('id'):
        Location.objects.create(
            sesi=new_sesi,
            kode=loc.kode,
            nama=loc.nama,
            wilayah=loc.wilayah,
        )

    for cr in sesi.criteria.order_by('id'):
        Criteria.objects.create(
            sesi=new_sesi,
            kode=cr.kode,
            nama=cr.nama,
            tipe=cr.tipe,
            bobot=cr.bobot,
            anchor_1=cr.anchor_1,
            anchor_2=cr.anchor_2,
            anchor_3=cr.anchor_3,
            anchor_4=cr.anchor_4,
            anchor_5=cr.anchor_5,
        )

    EventLog.objects.create(
        sesi=new_sesi,
        user=user,
        seat_kode='KETUA',
        aksi='STATE_T7_REVISE',
        status_dari='FINAL',
        status_ke='DRAFT',
        ip_address=_get_client_ip(request),
        user_agent=_get_user_agent(request),
        detail={
            'parent_sesi_id': sesi.id,
            'parent_versi': sesi.versi,
            'new_versi': new_sesi.versi,
            'alasan': alasan.strip(),
        }
    )
    return new_sesi
