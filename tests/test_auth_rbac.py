import pytest
from django.core.management import call_command
from django.urls import reverse
from django.test import Client
from apps.accounts.models import User, Seat
from apps.audit.models import EventLog
from apps.accounts.rbac import requires_seat, requires_admin, requires_any_dm
from django.http import HttpResponse
from django.core.exceptions import PermissionDenied


# Dummy views to test RBAC decorators
@requires_seat('KETUA')
def dummy_ketua_only_view(request):
    return HttpResponse('OK_KETUA')


@requires_seat('KETUA', 'PU', 'SOSIAL')
def dummy_any_dm_seat_view(request):
    return HttpResponse('OK_DM_SEAT')


@requires_admin
def dummy_admin_only_view(request):
    return HttpResponse('OK_ADMIN')


@requires_any_dm
def dummy_requires_any_dm_view(request):
    return HttpResponse('OK_ANY_DM')


@pytest.fixture(autouse=True)
def seed_data(db):
    call_command('seed_gdss')


@pytest.mark.django_db
class TestAuthentication:
    def test_login_page_renders_cleanly(self, client):
        response = client.get(reverse('login'))
        assert response.status_code == 200
        assert 'Sistem Pendukung Keputusan' in response.content.decode('utf-8')
        assert 'nama@instansi.go.id' in response.content.decode('utf-8')

    def test_login_success(self, client):
        response = client.post(reverse('login'), {
            'email': 'ketua@bpbd.go.id',
            'kata_sandi': 'Bpbd@2026!',
        })
        assert response.status_code == 302
        assert response.url == reverse('home')

        user = User.objects.get(email='ketua@bpbd.go.id')
        assert user.active_session_key is not None
        assert client.session.get('gdss_session_key') == user.active_session_key
        assert user.failed_login_attempts == 0

        # Verifikasi event log tercatat
        log = EventLog.objects.filter(user=user, aksi='LOGIN_SUCCESS').latest('waktu')
        assert log.seat_kode == 'KETUA'

    def test_login_invalid_password(self, client):
        response = client.post(reverse('login'), {
            'email': 'ketua@bpbd.go.id',
            'kata_sandi': 'WrongPassword123!',
        })
        assert response.status_code == 200
        assert 'Email atau kata sandi tidak valid.' in response.content.decode('utf-8')

        user = User.objects.get(email='ketua@bpbd.go.id')
        assert user.failed_login_attempts == 1
        assert not user.is_locked()

    def test_login_lockout_after_five_attempts(self, client):
        user = User.objects.get(email='ketua@bpbd.go.id')

        # 4 percobaan gagal
        for i in range(4):
            client.post(reverse('login'), {
                'email': 'ketua@bpbd.go.id',
                'kata_sandi': 'WrongPassword!',
            })
            user.refresh_from_db()
            assert user.failed_login_attempts == i + 1
            assert not user.is_locked()

        # Percobaan ke-5 memicu penguncian akun 15 menit
        response = client.post(reverse('login'), {
            'email': 'ketua@bpbd.go.id',
            'kata_sandi': 'WrongPassword!',
        })
        user.refresh_from_db()
        assert user.is_locked()
        assert user.failed_login_attempts == 0

        # Percobaan saat akun terkunci memunculkan pesan penguncian
        response = client.post(reverse('login'), {
            'email': 'ketua@bpbd.go.id',
            'kata_sandi': 'Bpbd@2026!',
        })
        content = response.content.decode('utf-8')
        assert 'Akun dikunci sementara' in content

    def test_logout_clears_active_session_key(self, client):
        # Login dulu
        client.post(reverse('login'), {
            'email': 'ketua@bpbd.go.id',
            'kata_sandi': 'Bpbd@2026!',
        })
        user = User.objects.get(email='ketua@bpbd.go.id')
        assert user.active_session_key is not None

        # Logout
        response = client.post(reverse('logout'))
        assert response.status_code == 302
        assert response.url == reverse('login')

        user.refresh_from_db()
        assert user.active_session_key is None

        # Event log LOGOUT
        log = EventLog.objects.filter(user=user, aksi='LOGOUT').latest('waktu')
        assert log.aksi == 'LOGOUT'


@pytest.mark.django_db
class TestSingleSessionEnforcementAC06:
    """
    AC-06:
    Bila pengguna login dari perangkat B, sesi di perangkat A otomatis dibatalkan.
    Ketika perangkat A melakukan request berikutnya, ia diarahkan ke login dengan
    pesan: "Sesi Anda di perangkat sebelumnya telah diakhiri karena login baru terdeteksi."
    """
    def test_single_active_session_terminates_previous_device(self):
        # Perangkat A login
        client_a = Client()
        res_a = client_a.post(reverse('login'), {
            'email': 'ketua@bpbd.go.id',
            'kata_sandi': 'Bpbd@2026!',
        })
        assert res_a.status_code == 302

        user = User.objects.get(email='ketua@bpbd.go.id')
        session_key_device_a = user.active_session_key

        # Perangkat A bisa akses home/sessions
        res_home_a = client_a.get(reverse('session-list'))
        assert res_home_a.status_code == 200

        # Perangkat B login menggunakan akun yang sama
        client_b = Client()
        res_b = client_b.post(reverse('login'), {
            'email': 'ketua@bpbd.go.id',
            'kata_sandi': 'Bpbd@2026!',
        })
        assert res_b.status_code == 302

        user.refresh_from_db()
        session_key_device_b = user.active_session_key
        assert session_key_device_b != session_key_device_a

        # Perangkat B bisa akses
        res_home_b = client_b.get(reverse('session-list'))
        assert res_home_b.status_code == 200

        # Perangkat A sekarang mengirim request berikutnya -> harus ditolak oleh SingleSessionMiddleware
        res_after = client_a.get(reverse('session-list'))
        assert res_after.status_code == 302
        assert res_after.url == reverse('login')

        # Follow redirect ke halaman login di perangkat A -> harus ada pesan terminasi sesi
        res_login_page = client_a.get(reverse('login'))
        assert res_login_page.status_code == 200
        content = res_login_page.content.decode('utf-8')
        assert 'Sesi Anda di perangkat sebelumnya telah diakhiri karena login baru terdeteksi.' in content


@pytest.mark.django_db
class TestRBACAuthorizationAC03:
    """
    AC-03: RBAC & Seat Authorization
    - Admin dilarang mengakses aksi DM (Zona 2)
    - Decision Maker non-Ketua dilarang mengakses aksi khusus Ketua
    - Request tanpa hak akses menghasilkan 403 & ACCESS_DENIED di event_log
    - Pengguna tanpa login diarahkan ke login
    """
    def test_unauthenticated_user_redirected_to_login(self, rf):
        request = rf.get('/sessions/')
        from django.contrib.auth.models import AnonymousUser
        request.user = AnonymousUser()

        response = dummy_ketua_only_view(request)
        assert response.status_code == 302
        assert response.url == reverse('login')

    def test_admin_cannot_access_dm_seat_view(self, rf):
        admin = User.objects.get(email='admin@bpbd.go.id')
        request = rf.get('/sessions/dummy-dm/')
        request.user = admin

        with pytest.raises(PermissionDenied):
            dummy_any_dm_seat_view(request)

        # Verifikasi ACCESS_DENIED tercatat di event_log
        log = EventLog.objects.filter(user=admin, aksi='ACCESS_DENIED').latest('waktu')
        assert log.aksi == 'ACCESS_DENIED'
        assert 'ADMIN role cannot access DM-only views' in log.detail.get('reason', '')

    def test_pu_cannot_access_ketua_only_view(self, rf):
        pu_user = User.objects.get(email='kadis.pu@pupr.go.id')
        request = rf.get('/sessions/dummy-ketua-action/')
        request.user = pu_user

        with pytest.raises(PermissionDenied):
            dummy_ketua_only_view(request)

        # Verifikasi ACCESS_DENIED tercatat di event_log
        log = EventLog.objects.filter(user=pu_user, aksi='ACCESS_DENIED').latest('waktu')
        assert log.aksi == 'ACCESS_DENIED'
        assert log.seat_kode == 'PU'
        assert "Seat 'PU' not in allowed ('KETUA',)" in log.detail.get('reason', '')

    def test_ketua_can_access_ketua_only_view(self, rf):
        ketua = User.objects.get(email='ketua@bpbd.go.id')
        request = rf.get('/sessions/dummy-ketua-action/')
        request.user = ketua

        response = dummy_ketua_only_view(request)
        assert response.status_code == 200
        assert response.content == b'OK_KETUA'

    def test_dm_cannot_access_admin_only_view(self, rf):
        ketua = User.objects.get(email='ketua@bpbd.go.id')
        request = rf.get('/admin/dummy/')
        request.user = ketua

        with pytest.raises(PermissionDenied):
            dummy_admin_only_view(request)

        log = EventLog.objects.filter(user=ketua, aksi='ACCESS_DENIED').latest('waktu')
        assert log.aksi == 'ACCESS_DENIED'
        assert 'Non-ADMIN tried to access admin-only view' in log.detail.get('reason', '')

    def test_admin_can_access_admin_only_view(self, rf):
        admin = User.objects.get(email='admin@bpbd.go.id')
        request = rf.get('/admin/dummy/')
        request.user = admin

        response = dummy_admin_only_view(request)
        assert response.status_code == 200
        assert response.content == b'OK_ADMIN'
