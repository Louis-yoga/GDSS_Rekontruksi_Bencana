from django.urls import path
from apps.accounts import admin_views

urlpatterns = [
    path('', admin_views.admin_panel_view, name='admin-panel'),
    path('seats/<str:seat_kode>/assign/', admin_views.assign_plt_seat_view, name='assign-plt-seat'),
]
