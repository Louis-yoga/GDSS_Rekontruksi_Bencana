import secrets
from django.contrib.auth import login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import render, redirect
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import never_cache
from django.utils import timezone
from apps.accounts.forms import LoginForm
from apps.audit.models import EventLog

def _get_client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '127.0.0.1')

def _get_user_agent(request):
    return request.META.get('HTTP_USER_AGENT', '')[:255]

@never_cache
@require_http_methods(['GET', 'POST'])
def login_view(request):
    # Already authenticated → go home
    if request.user.is_authenticated:
        return redirect('home')

    # Carry an escalation message set by SingleSessionMiddleware
    session_ended = request.session.pop('session_ended_by_new_login', False)

    form = LoginForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        user = form.get_user()

        # --- Single-active-session enforcement (AC-06) ---
        # Generate a fresh session key; storing it on the user record
        # lets the middleware invalidate the previous device on the next request.
        new_key = secrets.token_hex(32)  # 64-char hex

        # Flush any existing Django session so the old session ID is gone
        request.session.flush()

        auth_login(request, user, backend='django.contrib.auth.backends.ModelBackend')

        # Persist the new key to the user record (overwrites old device)
        user.active_session_key = new_key
        user.save(update_fields=['active_session_key'])

        # Store it in the new session so middleware can compare on each request
        request.session['gdss_session_key'] = new_key

        EventLog.objects.create(
            sesi=None,
            user=user,
            seat_kode=getattr(getattr(user, 'active_seat', None), 'kode', None),
            aksi='LOGIN_SUCCESS',
            ip_address=_get_client_ip(request),
            user_agent=_get_user_agent(request),
            detail={'email': user.email}
        )

        return redirect('home')

    return render(request, 'accounts/login.html', {
        'form': form,
        'session_ended': session_ended,
    })


@never_cache
@require_http_methods(['POST'])
def logout_view(request):
    user = request.user
    if user.is_authenticated:
        # Clear server-side session key so the old session becomes invalid everywhere
        user.active_session_key = None
        user.save(update_fields=['active_session_key'])

        EventLog.objects.create(
            sesi=None,
            user=user,
            seat_kode=getattr(getattr(user, 'active_seat', None), 'kode', None),
            aksi='LOGOUT',
            ip_address=_get_client_ip(request),
            user_agent=_get_user_agent(request),
            detail={}
        )

    auth_logout(request)
    return redirect('login')
