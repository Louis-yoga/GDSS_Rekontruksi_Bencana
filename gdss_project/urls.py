"""
Root URL configuration — GDSS Penentuan Lokasi Rekonstruksi Pascabencana.

Auth namespace : /auth/
Decision sessions (Fase 2+): /sessions/
Admin panel     : /admin/
"""
from django.contrib import admin
from django.urls import path, include
from django.contrib.auth.decorators import login_required
from django.views.generic import RedirectView

urlpatterns = [
    path('admin/', admin.site.urls),

    # Autentikasi (Fase 1)
    path('auth/', include('apps.accounts.urls')),

    # Sesi Keputusan (Fase 2+) — stub home redirect for now
    path(
        'sessions/',
        include('apps.decision_sessions.urls'),
    ),

    # Root → redirect ke daftar sesi (login required)
    path(
        '',
        login_required(
            RedirectView.as_view(url='/sessions/', permanent=False),
            login_url='/auth/login/',
        ),
        name='home',
    ),
]
