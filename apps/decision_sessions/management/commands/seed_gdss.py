import os
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from apps.accounts.models import User, Seat, SeatHistory
from apps.decision_sessions.models import SesiKeputusan, Location, Criteria, LocationRef
from apps.audit.models import EventLog

class Command(BaseCommand):
    help = 'Seed data baku GDSS: 4 akun pengguna, 3 kursi tetap, 1 sesi DRAFT, 6 lokasi, dan 8 kriteria standar'

    @transaction.atomic
    def handle(self, *args, **options):
        self.stdout.write("Memulai seeding data GDSS...")

        default_password = os.environ.get('GDSS_DEFAULT_PASSWORD', 'Bpbd@2026!')

        # 1. 4 Akun Pengguna Wajib
        users_data = [
            {
                'email': 'ketua@bpbd.go.id',
                'nama': 'Ir. H. Budi Santoso, M.T.',
                'role': 'DM',
            },
            {
                'email': 'kadis.pu@pupr.go.id',
                'nama': 'Dr. Eng. Ahmad Fauzi, S.T., M.Eng.',
                'role': 'DM',
            },
            {
                'email': 'kadis.sosial@dinsos.go.id',
                'nama': 'Dra. Hj. Siti Nurhaliza, M.Si.',
                'role': 'DM',
            },
            {
                'email': 'admin@bpbd.go.id',
                'nama': 'Administrator Sistem GDSS',
                'role': 'ADMIN',
            },
        ]

        users_map = {}
        for u in users_data:
            user, created = User.objects.get_or_create(
                email=u['email'],
                defaults={'nama': u['nama'], 'role': u['role']}
            )
            user.set_password(default_password)
            user.nama = u['nama']
            user.role = u['role']
            user.aktif = True
            user.failed_login_attempts = 0
            user.locked_until = None
            user.save()
            users_map[u['email']] = user
            status_text = "Dibuat" if created else "Diperbarui"
            self.stdout.write(f"  - Akun [{status_text}]: {user.email} ({user.role})")

        # 2. 3 Baris Kursi Tetap
        seats_data = [
            ('KETUA', 'Ketua Pelaksana BPBD', users_map['ketua@bpbd.go.id']),
            ('PU', 'Kepala Dinas Pekerjaan Umum', users_map['kadis.pu@pupr.go.id']),
            ('SOSIAL', 'Kepala Dinas Sosial', users_map['kadis.sosial@dinsos.go.id']),
        ]

        seats_map = {}
        for kode, nama_jabatan, active_user in seats_data:
            seat, created = Seat.objects.get_or_create(
                kode=kode,
                defaults={'nama_jabatan': nama_jabatan, 'user_id_aktif': active_user}
            )
            seat.nama_jabatan = nama_jabatan
            seat.user_id_aktif = active_user
            seat.save()
            seats_map[kode] = seat

            SeatHistory.objects.get_or_create(
                seat=seat,
                user=active_user,
                mulai_pada=timezone.now(),
                defaults={'alasan': 'Penetapan awal kursi keputusan saat inisialisasi sistem'}
            )
            self.stdout.write(f"  - Kursi: {seat.kode} -> {active_user.nama}")

        # 3. 1 Sesi Awal berstatus DRAFT
        sesi, created = SesiKeputusan.objects.get_or_create(
            judul='Prioritas Rekonstruksi Pascabencana Gempa & Longsor 2026',
            versi=1,
            defaults={
                'status': 'DRAFT',
                'nilai_k': 3,
                'durasi_t1_menit': 1440,
                'durasi_t2_menit': 720,
                'sla_ketua_menit': 360,
                'jumlah_perpanjangan': 0,
                'kuorum_parsial': False,
                'status_deviasi': False,
            }
        )
        self.stdout.write(f"  - Sesi Keputusan: {sesi.judul} (Status: {sesi.status})")

        # 4. 6 Lokasi Bencana Contoh
        locations_data = [
            ('LOK-A', 'Desa Sukamaju', 'Kecamatan Cugenang'),
            ('LOK-B', 'Desa Nagrak', 'Kecamatan Cianjur'),
            ('LOK-C', 'Desa Sarampad', 'Kecamatan Cugenang'),
            ('LOK-D', 'Desa Benjot', 'Kecamatan Cugenang'),
            ('LOK-E', 'Desa Gasol', 'Kecamatan Cugenang'),
            ('LOK-F', 'Desa Mangunkerta', 'Kecamatan Cugenang'),
        ]

        loc_objs = {}
        for kode, nama, wilayah in locations_data:
            loc, _ = Location.objects.get_or_create(
                sesi=sesi,
                kode=kode,
                defaults={'nama': nama, 'wilayah': wilayah}
            )
            loc.nama = nama
            loc.wilayah = wilayah
            loc.save()
            loc_objs[kode] = loc
        self.stdout.write(f"  - 6 Lokasi terdaftar: {list(loc_objs.keys())}")

        # 5. 8 Kriteria Baku C1-C8 dengan Rubrik Anchor 5 Tingkat
        criteria_data = [
            {
                'kode': 'C1',
                'nama': 'Tingkat kerusakan bangunan & infrastruktur',
                'tipe': 'BENEFIT',
                'bobot': Decimal('15.0000'),
                'anchor_1': 'Kerusakan minor <10% bangunan; fungsi struktur & utilitas tetap berjalan normal.',
                'anchor_2': 'Kerusakan ringan 10%–25%; retak non-struktural, masih layak huni dengan perbaikan kecil.',
                'anchor_3': 'Kerusakan sedang 26%–50%; sebagian elemen struktur/atap rusak, butuh rehabilitasi.',
                'anchor_4': 'Kerusakan berat 51%–75%; struktur utama goyah/miring, tidak layak huni.',
                'anchor_5': 'Kerusakan total >75%; bangunan & infrastruktur vital roboh/lumpuh total.',
            },
            {
                'kode': 'C2',
                'nama': 'Tingkat keterisolasian akses jalan',
                'tipe': 'BENEFIT',
                'bobot': Decimal('10.0000'),
                'anchor_1': 'Akses jalan utama lancar 100%, dapat dilalui seluruh kendaraan logistik berat.',
                'anchor_2': 'Hambatan ringan (puing/genangan); kendaraan logistik masih bisa melintas dengan kecepatan terbatas.',
                'anchor_3': 'Akses utama terputus sebagian; hanya bisa dilalui kendaraan kecil/roda dua atau memutar jauh.',
                'anchor_4': 'Wilayah terisolasi dari jalur darat roda empat; hanya terjangkau jalan setapak/perahu kecil.',
                'anchor_5': 'Wilayah terisolasi total (100% akses darat putus); hanya bisa dijangkau jalur udara/evakuasi khusus.',
            },
            {
                'kode': 'C3',
                'nama': 'Estimasi biaya perbaikan',
                'tipe': 'COST',
                'bobot': Decimal('10.0000'),
                'anchor_1': 'Biaya sangat kecil (< Rp 500 juta); cukup ditangani dana operasional rutin.',
                'anchor_2': 'Biaya kecil (Rp 500 juta – Rp 2 miliar); rehabilitasi ringan skala lokal.',
                'anchor_3': 'Biaya sedang (Rp 2 miliar – Rp 10 miliar); membutuhkan anggaran rekonstruksi tingkat kabupaten/kota.',
                'anchor_4': 'Biaya besar (Rp 10 miliar – Rp 50 miliar); membutuhkan realokasi anggaran khusus/bantuan provinsi.',
                'anchor_5': 'Biaya sangat besar (> Rp 50 miliar); butuh pendanaan darurat lintas tingkat/nasional.',
            },
            {
                'kode': 'C4',
                'nama': 'Jumlah korban jiwa/luka',
                'tipe': 'BENEFIT',
                'bobot': Decimal('15.0000'),
                'anchor_1': 'Tidak ada korban jiwa/luka berat; maksimal hanya luka ringan <= 5 orang.',
                'anchor_2': 'Tidak ada korban meninggal; 1–10 orang luka berat/dirawat.',
                'anchor_3': '1–5 korban meninggal atau 11–30 orang luka berat.',
                'anchor_4': '6–20 korban meninggal atau 31–100 orang luka berat.',
                'anchor_5': '> 20 korban meninggal atau > 100 orang luka berat/hilang.',
            },
            {
                'kode': 'C5',
                'nama': 'Jumlah pengungsi',
                'tipe': 'BENEFIT',
                'bobot': Decimal('15.0000'),
                'anchor_1': '< 100 jiwa.',
                'anchor_2': '100–500 jiwa.',
                'anchor_3': '501–2.000 jiwa.',
                'anchor_4': '2.001–5.000 jiwa.',
                'anchor_5': '> 5.000 jiwa.',
            },
            {
                'kode': 'C6',
                'nama': 'Proporsi kelompok rentan (lansia, anak)',
                'tipe': 'BENEFIT',
                'bobot': Decimal('10.0000'),
                'anchor_1': '< 10% dari total warga terdampak di lokasi.',
                'anchor_2': '10%–19% dari total warga terdampak.',
                'anchor_3': '20%–34% dari total warga terdampak.',
                'anchor_4': '35%–49% dari total warga terdampak.',
                'anchor_5': '>= 50% dari total warga terdampak (mayoritas lansia, balita, ibu hamil, atau difabel).',
            },
            {
                'kode': 'C7',
                'nama': 'Urgensi air bersih & pangan',
                'tipe': 'BENEFIT',
                'bobot': Decimal('15.0000'),
                'anchor_1': 'Pasokan air bersih & pangan aman (> 7 hari) tanpa ketergantungan bantuan luar.',
                'anchor_2': 'Pasokan lokal mulai menipis (cukup 4–7 hari); butuh dukungan distribusi reguler.',
                'anchor_3': 'Pasokan kritis (cukup 2–3 hari); sumber air lokal tercemar/terbatas.',
                'anchor_4': 'Sangat mendesak (stok 12–24 jam); mulai terjadi kelangkaan air minum & pangan siap saji.',
                'anchor_5': 'Darurat akut (stok < 12 jam); tidak ada akses air bersih dan pangan sama sekali.',
            },
            {
                'kode': 'C8',
                'nama': 'Risiko bencana lanjutan & stabilitas wilayah',
                'tipe': 'BENEFIT',
                'bobot': Decimal('10.0000'),
                'anchor_1': 'Kondisi geologis/cuaca stabil; tidak ada potensi bencana susulan, situasi aman.',
                'anchor_2': 'Potensi susulan rendah; retakan tanah minor atau gangguan keamanan ringan terkendali.',
                'anchor_3': 'Potensi susulan sedang (gempa susulan/longsor lokal); perlu pemantauan berkala.',
                'anchor_4': 'Potensi susulan tinggi (lereng labil/banjir bandang mengancam posko); perlu relokasi sementara.',
                'anchor_5': 'Ancaman susulan kritis & langsung mengancam keselamatan jiwa/stabilitas wilayah; wajib evakuasi segera.',
            },
        ]

        crit_objs = {}
        for c in criteria_data:
            crit, _ = Criteria.objects.get_or_create(
                sesi=sesi,
                kode=c['kode'],
                defaults={
                    'nama': c['nama'],
                    'tipe': c['tipe'],
                    'bobot': c['bobot'],
                    'anchor_1': c['anchor_1'],
                    'anchor_2': c['anchor_2'],
                    'anchor_3': c['anchor_3'],
                    'anchor_4': c['anchor_4'],
                    'anchor_5': c['anchor_5'],
                }
            )
            crit.nama = c['nama']
            crit.tipe = c['tipe']
            crit.bobot = c['bobot']
            crit.anchor_1 = c['anchor_1']
            crit.anchor_2 = c['anchor_2']
            crit.anchor_3 = c['anchor_3']
            crit.anchor_4 = c['anchor_4']
            crit.anchor_5 = c['anchor_5']
            crit.save()
            crit_objs[c['kode']] = crit
        self.stdout.write(f"  - 8 Kriteria standar (C1-C8, total bobot: 100) terdaftar.")

        # 6. Catat Event Log Inisialisasi Sistem
        EventLog.objects.create(
            sesi=sesi,
            user=users_map['admin@bpbd.go.id'],
            seat_kode='ADMIN',
            aksi='SYSTEM_INITIALIZED',
            status_dari=None,
            status_ke='DRAFT',
            ip_address='127.0.0.1',
            user_agent='Django Management Command: seed_gdss',
            detail={'catatan': 'Seeding data awal berhasil dilakukan'}
        )

        self.stdout.write(self.style.SUCCESS('Seeding data GDSS berhasil diselesaikan dengan sukses!'))
