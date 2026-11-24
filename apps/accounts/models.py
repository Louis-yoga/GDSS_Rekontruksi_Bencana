from django.db import models
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin, BaseUserManager
from django.utils import timezone

class UserManager(BaseUserManager):
    def create_user(self, email, nama, password=None, role='DM', **extra_fields):
        if not email:
            raise ValueError('Email wajib disediakan')
        email = self.normalize_email(email)
        user = self.model(email=email, nama=nama, role=role, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, nama='Administrator', password=None, **extra_fields):
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(email, nama, password=password, role='ADMIN', **extra_fields)

class User(AbstractBaseUser, PermissionsMixin):
    ROLE_CHOICES = [
        ('DM', 'Decision Maker'),
        ('ADMIN', 'Administrator Teknis'),
    ]

    id = models.BigAutoField(primary_key=True)
    nama = models.CharField(max_length=120)
    email = models.EmailField(max_length=150, unique=True)
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='DM')
    aktif = models.BooleanField(default=True)
    active_session_key = models.CharField(max_length=64, null=True, blank=True)
    failed_login_attempts = models.IntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = UserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['nama']

    class Meta:
        db_table = 'users'
        verbose_name = 'Pengguna'
        verbose_name_plural = 'Pengguna'

    @property
    def is_active(self):
        return self.aktif

    @property
    def is_staff(self):
        return self.role == 'ADMIN'

    def is_locked(self):
        if self.locked_until and self.locked_until > timezone.now():
            return True
        return False

    def __str__(self):
        return f"{self.nama} ({self.email})"

class Seat(models.Model):
    SEAT_CHOICES = [
        ('KETUA', 'Ketua Pelaksana BPBD'),
        ('PU', 'Kepala Dinas Pekerjaan Umum'),
        ('SOSIAL', 'Kepala Dinas Sosial'),
    ]

    id = models.AutoField(primary_key=True)
    kode = models.CharField(max_length=10, unique=True, choices=SEAT_CHOICES)
    nama_jabatan = models.CharField(max_length=120)
    user_id_aktif = models.OneToOneField(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='active_seat',
        db_column='user_id_aktif'
    )

    class Meta:
        db_table = 'seats'
        verbose_name = 'Kursi Keputusan'
        verbose_name_plural = 'Kursi Keputusan'

    def __str__(self):
        return f"Kursi {self.kode} - {self.nama_jabatan}"

class SeatHistory(models.Model):
    id = models.BigAutoField(primary_key=True)
    seat = models.ForeignKey(Seat, on_delete=models.CASCADE, related_name='history')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='seat_history')
    mulai_pada = models.DateTimeField()
    selesai_pada = models.DateTimeField(null=True, blank=True)
    oleh_admin = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='administered_seat_changes'
    )
    alasan = models.TextField()

    class Meta:
        db_table = 'seat_history'
        verbose_name = 'Riwayat Kursi'
        verbose_name_plural = 'Riwayat Kursi'

    def __str__(self):
        return f"Riwayat {self.seat.kode}: {self.user.nama} ({self.mulai_pada})"
