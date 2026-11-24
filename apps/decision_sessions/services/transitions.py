"""
State Machine Transitions & Guard Conditions for SesiKeputusan.

Menerapkan transisi status deterministik:
- T1   : DRAFT -> PENILAIAN (hanya KETUA, guard lengkap)
- T2-A : PENILAIAN -> HASIL_AWAL (otomatis saat 3/3 kursi submit)
- T2-B : PENILAIAN -> HASIL_AWAL (tutup paksa KETUA, guard kuorum 2/3 & deadline)
- Unlock Assessment : Mengembalikan assessment kursi ke DRAFT (hanya KETUA + alasan)
"""
import hashlib
from django.utils import timezone
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from apps.decision_sessions.models import SesiKeputusan, Assessment
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
    if sesi.deadline_aktif and now <= sesi.deadline_aktif:
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
