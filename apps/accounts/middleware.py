"""
SingleSessionMiddleware — Zona 7 & AC-06 enforcement.

Every request from an authenticated user is checked against the
active_session_key stored on the User record.  If the keys differ,
the previous device's session is invalidated and the user is
redirected to the login page with a clear, plain-language message.

Exempt paths (login, logout, static) are skipped to avoid redirect loops.
"""
from django.contrib.auth import logout as auth_logout
from django.shortcuts import redirect
from django.urls import reverse


EXEMPT_PATHS = ('/auth/login/', '/auth/logout/', '/static/', '/favicon.ico')


class SingleSessionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            path = request.path_info

            # Skip enforcement for auth and static endpoints
            if not any(path.startswith(p) for p in EXEMPT_PATHS):
                stored_key = request.session.get('gdss_session_key')
                db_key = request.user.active_session_key

                # Keys missing or mismatched → another device logged in
                if not stored_key or not db_key or stored_key != db_key:
                    auth_logout(request)
                    request.session['session_ended_by_new_login'] = True
                    return redirect(reverse('login'))

        return self.get_response(request)
