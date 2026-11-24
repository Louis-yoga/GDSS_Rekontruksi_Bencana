"""
RBAC enforcement for GDSS.

Design principle (Zona 7 / PRD §2.2):
  - Identity and seat role are read exclusively from request.user and the
    related seats table — never from URL params, query strings, or hidden
    form fields.
  - Every denied access produces HTTP 403 and an event_log entry.
  - Default-Deny: unless a view is explicitly marked as allowed, every
    authenticated request with the wrong role or wrong seat is rejected.
"""
from functools import wraps
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from apps.audit.models import EventLog

def _get_client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '127.0.0.1')

def _log_forbidden(request, reason: str):
    """Append an event_log record for every denied access attempt."""
    user = request.user if request.user.is_authenticated else None
    seat_kode = None
    if user:
        seat = getattr(user, 'active_seat', None)
        seat_kode = seat.kode if seat else None

    EventLog.objects.create(
        sesi=None,
        user=user,
        seat_kode=seat_kode,
        aksi='ACCESS_DENIED',
        ip_address=_get_client_ip(request),
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:255],
        detail={
            'path': request.path,
            'method': request.method,
            'reason': reason,
        }
    )


def requires_seat(*allowed_seat_codes, require_login=True):
    """
    Decorator: permits access only to users whose active seat kode
    is in allowed_seat_codes.

    Usage:
        @requires_seat('KETUA')
        def open_voting(request, session_id): ...

        @requires_seat('KETUA', 'PU', 'SOSIAL')
        def submit_assessment(request, session_id): ...
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect('login')

            # ADMIN is explicitly excluded from DM operations (Zona 2)
            if request.user.role == 'ADMIN' and 'ADMIN' not in allowed_seat_codes:
                _log_forbidden(request, 'ADMIN role cannot access DM-only views')
                raise PermissionDenied

            seat = getattr(request.user, 'active_seat', None)
            seat_kode = seat.kode if seat else None

            if seat_kode not in allowed_seat_codes:
                _log_forbidden(
                    request,
                    f'Seat {seat_kode!r} not in allowed {allowed_seat_codes}'
                )
                raise PermissionDenied

            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator


def requires_admin(view_func):
    """
    Decorator: permits access only to ADMIN role users.
    DM seats cannot access admin-only views.
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')

        if request.user.role != 'ADMIN':
            _log_forbidden(request, 'Non-ADMIN tried to access admin-only view')
            raise PermissionDenied

        return view_func(request, *args, **kwargs)
    return _wrapped


def requires_any_dm(view_func):
    """
    Decorator: permits access to any of the three DM seats (KETUA, PU, SOSIAL).
    ADMIN is excluded.
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')

        if request.user.role == 'ADMIN':
            _log_forbidden(request, 'ADMIN tried to access DM-only view')
            raise PermissionDenied

        seat = getattr(request.user, 'active_seat', None)
        if seat is None:
            _log_forbidden(request, 'Authenticated user has no active seat')
            raise PermissionDenied

        return view_func(request, *args, **kwargs)
    return _wrapped
