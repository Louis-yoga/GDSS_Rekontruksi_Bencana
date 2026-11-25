"""
Deadline & SLA Escalation Evaluator — Fase 6 (Sprint 15).

Aturan Kritis (Zona 1 / AC-11):
- Mengevaluasi tenggat waktu T1 (PENILAIAN), T2 (VOTING), dan SLA Ketua 6 jam (360 menit).
- Menyediakan lazy deadline check untuk HTTP request dan cron check_deadlines.
- DILARANG KERAS mengubah status sesi menjadi FINAL secara otomatis (Zona 1).
"""
from datetime import timedelta
from django.utils import timezone
from apps.decision_sessions.models import SesiKeputusan
from apps.audit.models import EventLog


def evaluate_session_deadline(sesi: SesiKeputusan, now=None) -> dict:
    """
    Mengevaluasi status tenggat waktu dan eskalasi SLA Ketua (6 jam / sla_ketua_menit).
    """
    if now is None:
        now = timezone.now()

    result = {
        'is_expired': False,
        'percent_elapsed': 0.0,
        'reminder_level': 'NONE',  # NONE, 50, 80, EXPIRED
        'sla_escalated': False,
        'sla_message': None,
    }

    if sesi.status in ['FINAL', 'DIBATALKAN', 'DIGANTIKAN', 'DRAFT']:
        return result

    sla_delta = timedelta(minutes=sesi.sla_ketua_menit)

    if sesi.status in ['PENILAIAN', 'VOTING'] and sesi.deadline_aktif:
        total_minutes = sesi.durasi_t1_menit if sesi.status == 'PENILAIAN' else sesi.durasi_t2_menit
        total_seconds = max(total_minutes * 60, 1)
        remaining_seconds = (sesi.deadline_aktif - now).total_seconds()
        elapsed_seconds = total_seconds - remaining_seconds

        percent = max(0.0, (elapsed_seconds / total_seconds) * 100.0)
        result['percent_elapsed'] = round(min(percent, 100.0), 1)

        if remaining_seconds <= 0:
            result['is_expired'] = True
            result['reminder_level'] = 'EXPIRED'
            result['percent_elapsed'] = 100.0

            # Cek SLA Ketua (misal 6 jam setelah deadline terlewati)
            if now > sesi.deadline_aktif + sla_delta:
                result['sla_escalated'] = True
                jam_sla = sesi.sla_ketua_menit // 60
                result['sla_message'] = (
                    f"ESKALASI KRITIS SLA ({jam_sla} Jam): Tenggat tahap {sesi.status} telah terlewati "
                    f"lebih dari {jam_sla} jam tanpa tindak lanjut Ketua BPBD. "
                    f"Administrator dapat melakukan penukaran Plt Ketua jika pejabat definitif berhalangan."
                )
        elif percent >= 80.0:
            result['reminder_level'] = '80'
        elif percent >= 50.0:
            result['reminder_level'] = '50'

    elif sesi.status == 'PENETAPAN':
        # Pada tahap PENETAPAN, SLA dihitung sejak masuk tahap PENETAPAN (updated_at)
        if sesi.updated_at and now > sesi.updated_at + sla_delta:
            result['sla_escalated'] = True
            jam_sla = sesi.sla_ketua_menit // 60
            result['sla_message'] = (
                f"ESKALASI KRITIS SLA ({jam_sla} Jam): Tahap Penetapan Final tertunda lebih dari "
                f"{jam_sla} jam tanpa keputusan Ketua BPBD."
            )

    return result


def record_sla_escalation_if_needed(sesi: SesiKeputusan, now=None) -> bool:
    """
    Lazy & Cron SLA check: mencatat EventLog SLA_ESCALATION (1x per tahap) bila SLA terlewati.
    Tidak pernah mengubah status sesi menjadi FINAL (Zona 1).
    """
    info = evaluate_session_deadline(sesi, now=now)
    if info['sla_escalated']:
        already_logged = EventLog.objects.filter(
            sesi=sesi,
            aksi='SLA_ESCALATION',
            status_dari=sesi.status,
        ).exists()

        if not already_logged:
            EventLog.objects.create(
                sesi=sesi,
                user=None,
                seat_kode=None,
                aksi='SLA_ESCALATION',
                status_dari=sesi.status,
                status_ke=sesi.status,
                ip_address='127.0.0.1',
                user_agent='system/deadline-evaluator',
                detail={
                    'sla_ketua_menit': sesi.sla_ketua_menit,
                    'message': info['sla_message'],
                }
            )
            return True
    return False
