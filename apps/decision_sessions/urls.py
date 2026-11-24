from django.urls import path
from apps.decision_sessions import views

urlpatterns = [
    # Dashboard Beranda Sesi (L2)
    path('', views.home_view, name='session-list'),

    # Transisi T1: Buka Sesi Penilaian
    path('<int:session_id>/open-assessment/', views.open_assessment_view, name='open-assessment'),

    # L4 — Formulir Penilaian DM & Autosave API & Submit
    path('<int:session_id>/assessment/', views.assessment_view, name='assessment-form'),
    path('api/<int:session_id>/assessments/draft/', views.autosave_assessment_api, name='api-autosave-assessment'),
    path('<int:session_id>/assessments/submit/', views.submit_assessment_view, name='submit-assessment'),

    # L5 — Hasil Awal & Divergensi
    path('<int:session_id>/results/', views.results_view, name='results-view'),

    # Transisi T2-B: Tutup Paksa Penilaian (Kuorum 2/3) & Buka Kunci
    path('<int:session_id>/close-assessment/', views.force_close_assessment_view, name='close-assessment'),
    path('<int:session_id>/assessments/<str:seat_code>/unlock/', views.unlock_assessment_view, name='unlock-assessment'),

    # Transisi T3: Buka Sesi Voting Borda
    path('<int:session_id>/open-voting/', views.open_voting_view, name='open-voting'),

    # L6 — Ruang Konsensus / Voting Borda
    path('<int:session_id>/voting/', views.voting_view, name='voting-view'),
    path('<int:session_id>/votes/submit/', views.submit_vote_view, name='submit-vote'),

    # Transisi T4-B: Tutup Paksa Voting (AC-10)
    path('<int:session_id>/close-voting/', views.force_close_voting_view, name='close-voting'),
]
