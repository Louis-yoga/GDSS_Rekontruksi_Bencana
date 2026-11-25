"""
Management command: check_deadlines (Cron 1 menit).

Mengevaluasi tenggat waktu dan SLA Ketua 6 jam pada seluruh sesi aktif
(PENILAIAN, VOTING, PENETAPAN).
Zona 1: Command ini HANYA mencatat eskalasi/log dan tidak pernah mengubah status ke FINAL.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from apps.decision_sessions.models import SesiKeputusan
from apps.decision_sessions.services.deadline import (
    evaluate_session_deadline,
    record_sla_escalation_if_needed,
)


class Command(BaseCommand):
    help = 'Evaluasi tenggat waktu T1/T2 dan eskalasi SLA Ketua 6 jam tanpa auto-finalisasi.'

    def handle(self, *args, **options):
        now = timezone.now()
        active_sessions = SesiKeputusan.objects.filter(
            status__in=['PENILAIAN', 'VOTING', 'PENETAPAN']
        )

        checked_count = 0
        escalated_count = 0

        for sesi in active_sessions:
            checked_count += 1
            info = evaluate_session_deadline(sesi, now=now)
            if record_sla_escalation_if_needed(sesi, now=now):
                escalated_count += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"[SLA ESCALATION] Sesi #{sesi.id} ({sesi.status}): {info['sla_message']}"
                    )
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"check_deadlines selesai: {checked_count} sesi diperiksa, {escalated_count} eskalasi baru dicatat."
            )
        )
