from django.urls import path
from apps.decision_sessions import views

urlpatterns = [
    path('', views.home_view, name='session-list'),
]
