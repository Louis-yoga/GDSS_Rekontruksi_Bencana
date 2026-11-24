import secrets
from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from apps.accounts.models import User

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

class LoginForm(forms.Form):
    email = forms.EmailField(
        label='Email',
        widget=forms.EmailInput(attrs={
            'autocomplete': 'email',
            'class': 'w-full px-3 py-2 rounded border border-slate-300 text-sm text-slate-900 '
                     'placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-sky-500 '
                     'focus:border-sky-500 transition-colors',
            'placeholder': 'nama@instansi.go.id',
        })
    )
    kata_sandi = forms.CharField(
        label='Kata Sandi',
        widget=forms.PasswordInput(attrs={
            'autocomplete': 'current-password',
            'class': 'w-full px-3 py-2 rounded border border-slate-300 text-sm text-slate-900 '
                     'placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-sky-500 '
                     'focus:border-sky-500 transition-colors',
            'placeholder': '••••••••',
        })
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._resolved_user = None

    def clean(self):
        cleaned = super().clean()
        email = cleaned.get('email')
        password = cleaned.get('kata_sandi')

        if not (email and password):
            return cleaned

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            # Constant-time failure — same message regardless of whether user exists
            raise ValidationError('Email atau kata sandi tidak valid.')

        if not user.aktif:
            raise ValidationError('Akun ini tidak aktif. Hubungi Administrator.')

        if user.is_locked():
            remaining = int((user.locked_until - timezone.now()).total_seconds() / 60) + 1
            raise ValidationError(
                f'Akun dikunci sementara selama {remaining} menit lagi '
                f'karena terlalu banyak percobaan gagal.'
            )

        if not user.check_password(password):
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
                user.locked_until = timezone.now() + timezone.timedelta(minutes=LOCKOUT_MINUTES)
                user.failed_login_attempts = 0
            user.save(update_fields=['failed_login_attempts', 'locked_until'])
            raise ValidationError('Email atau kata sandi tidak valid.')

        # Credentials valid — reset counter
        user.failed_login_attempts = 0
        user.locked_until = None
        user.save(update_fields=['failed_login_attempts', 'locked_until'])
        self._resolved_user = user
        return cleaned

    def get_user(self):
        return self._resolved_user
