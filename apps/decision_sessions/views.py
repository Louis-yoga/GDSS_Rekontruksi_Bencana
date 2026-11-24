"""
Stub views for apps.decision_sessions — Fase 1 placeholder.

Fase 2 (Sprint 5-6) will replace home_view with the real Beranda
(badge status, kartu progres 3 kursi, hitung mundur, polling JS 20s).
"""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required(login_url='/auth/login/')
def home_view(request):
    """Temporary stub — replaced in Fase 2 with full session dashboard."""
    return render(request, 'sessions/home_stub.html', {
        'user': request.user,
    })
