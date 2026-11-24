from django.db import models

class SesiKeputusan(models.Model):
    STATUS_CHOICES = [
        ('DRAFT', 'Draft Sesi'),
        ('PENILAIAN', 'Penilaian Berlangsung'),
        ('HASIL_AWAL', 'Siap Konsensus / Hasil Awal'),
        ('VOTING', 'Voting Borda Berlangsung'),
        ('PENETAPAN', 'Menunggu Penetapan Ketua'),
        ('FINAL', 'Keputusan Final'),
        ('DIBATALKAN', 'Sesi Dibatalkan'),
        ('DIGANTIKAN', 'Digantikan Sesi Revisi Baru'),
    ]

    id = models.BigAutoField(primary_key=True)
    judul = models.CharField(max_length=200)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT')
    versi = models.IntegerField(default=1)
    parent = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='revisions'
    )
    nilai_k = models.IntegerField(default=3)
    durasi_t1_menit = models.IntegerField(default=1440)
    durasi_t2_menit = models.IntegerField(default=720)
    sla_ketua_menit = models.IntegerField(default=360)
    jumlah_perpanjangan = models.IntegerField(default=0)
    deadline_aktif = models.DateTimeField(null=True, blank=True)
    kuorum_parsial = models.BooleanField(default=False)
    status_deviasi = models.BooleanField(default=False)
    alasan_deviasi = models.TextField(null=True, blank=True)
    alasan_status = models.TextField(null=True, blank=True)
    config_hash = models.CharField(max_length=64, null=True, blank=True)
    final_hash = models.CharField(max_length=64, null=True, blank=True)
    finalized_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'sesi_keputusan'
        verbose_name = 'Sesi Keputusan'
        verbose_name_plural = 'Sesi Keputusan'
        constraints = [
            models.CheckConstraint(
                condition=models.Q(nilai_k__gte=1),
                name='check_nilai_k_gte_1'
            ),
            models.CheckConstraint(
                condition=models.Q(jumlah_perpanjangan__lte=2),
                name='check_jumlah_perpanjangan_lte_2'
            )
        ]

    def __str__(self):
        return f"{self.judul} (v{self.versi}) - {self.status}"

class Location(models.Model):
    id = models.BigAutoField(primary_key=True)
    sesi = models.ForeignKey(SesiKeputusan, on_delete=models.CASCADE, related_name='locations')
    kode = models.CharField(max_length=30)
    nama = models.CharField(max_length=150)
    wilayah = models.CharField(max_length=150)

    class Meta:
        db_table = 'locations'
        verbose_name = 'Lokasi Bencana'
        verbose_name_plural = 'Lokasi Bencana'
        unique_together = ('sesi', 'kode')

    def __str__(self):
        return f"{self.kode} - {self.nama} ({self.wilayah})"

class Criteria(models.Model):
    TIPE_CHOICES = [
        ('BENEFIT', 'Benefit'),
        ('COST', 'Cost'),
    ]

    id = models.BigAutoField(primary_key=True)
    sesi = models.ForeignKey(SesiKeputusan, on_delete=models.CASCADE, related_name='criteria')
    kode = models.CharField(max_length=20)
    nama = models.CharField(max_length=150)
    tipe = models.CharField(max_length=10, choices=TIPE_CHOICES)
    bobot = models.DecimalField(max_digits=8, decimal_places=4)
    anchor_1 = models.TextField()
    anchor_2 = models.TextField()
    anchor_3 = models.TextField()
    anchor_4 = models.TextField()
    anchor_5 = models.TextField()

    class Meta:
        db_table = 'criteria'
        verbose_name = 'Kriteria Penilaian'
        verbose_name_plural = 'Kriteria Penilaian'
        unique_together = ('sesi', 'kode')
        constraints = [
            models.CheckConstraint(
                condition=models.Q(bobot__gt=0),
                name='check_bobot_gt_0'
            )
        ]

    def __str__(self):
        return f"{self.kode} - {self.nama} ({self.tipe})"

class LocationRef(models.Model):
    id = models.BigAutoField(primary_key=True)
    sesi = models.ForeignKey(SesiKeputusan, on_delete=models.CASCADE, related_name='location_refs')
    lokasi = models.ForeignKey(Location, on_delete=models.CASCADE)
    kriteria = models.ForeignKey(Criteria, on_delete=models.CASCADE)
    nilai_ref = models.CharField(max_length=100)
    sumber = models.CharField(max_length=150)
    tanggal_survei = models.DateField(null=True, blank=True)

    class Meta:
        db_table = 'location_refs'
        verbose_name = 'Data Referensi Lokasi'
        verbose_name_plural = 'Data Referensi Lokasi'
        unique_together = ('sesi', 'lokasi', 'kriteria')

    def __str__(self):
        return f"Ref {self.lokasi.kode} - {self.kriteria.kode}: {self.nilai_ref}"

class Assessment(models.Model):
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('SUBMITTED', 'Submitted'),
    ]

    id = models.BigAutoField(primary_key=True)
    sesi = models.ForeignKey(SesiKeputusan, on_delete=models.CASCADE, related_name='assessments')
    seat = models.ForeignKey('accounts.Seat', on_delete=models.CASCADE)
    user = models.ForeignKey('accounts.User', on_delete=models.CASCADE)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='DRAFT')
    versi_draf = models.IntegerField(default=1)
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'assessments'
        verbose_name = 'Penilaian Kursi'
        verbose_name_plural = 'Penilaian Kursi'
        unique_together = ('sesi', 'seat')

    def __str__(self):
        return f"Penilaian {self.sesi.judul} - Kursi {self.seat.kode} ({self.status})"

class AssessmentScore(models.Model):
    id = models.BigAutoField(primary_key=True)
    assessment = models.ForeignKey(Assessment, on_delete=models.CASCADE, related_name='scores')
    lokasi = models.ForeignKey(Location, on_delete=models.CASCADE)
    kriteria = models.ForeignKey(Criteria, on_delete=models.CASCADE)
    skor = models.PositiveSmallIntegerField()
    catatan = models.CharField(max_length=300, null=True, blank=True)

    class Meta:
        db_table = 'assessment_scores'
        verbose_name = 'Skor Penilaian'
        verbose_name_plural = 'Skor Penilaian'
        unique_together = ('assessment', 'lokasi', 'kriteria')
        constraints = [
            models.CheckConstraint(
                condition=models.Q(skor__gte=1, skor__lte=5),
                name='check_skor_1_to_5'
            )
        ]

    def __str__(self):
        return f"Skor {self.lokasi.kode}-{self.kriteria.kode}: {self.skor}"

class Vote(models.Model):
    id = models.BigAutoField(primary_key=True)
    sesi = models.ForeignKey(SesiKeputusan, on_delete=models.CASCADE, related_name='votes')
    seat = models.ForeignKey('accounts.Seat', on_delete=models.CASCADE)
    user = models.ForeignKey('accounts.User', on_delete=models.CASCADE)
    abstain = models.BooleanField(default=False)
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'votes'
        verbose_name = 'Suara Voting'
        verbose_name_plural = 'Suara Voting'
        unique_together = ('sesi', 'seat')

    def __str__(self):
        status_text = 'Abstain' if self.abstain else 'Memilih'
        return f"Vote {self.sesi.judul} - {self.seat.kode} ({status_text})"

class VoteItem(models.Model):
    id = models.BigAutoField(primary_key=True)
    vote = models.ForeignKey(Vote, on_delete=models.CASCADE, related_name='items')
    peringkat = models.IntegerField()
    lokasi = models.ForeignKey(Location, on_delete=models.CASCADE)

    class Meta:
        db_table = 'vote_items'
        verbose_name = 'Pilihan Surat Suara'
        verbose_name_plural = 'Pilihan Surat Suara'
        unique_together = [
            ('vote', 'peringkat'),
            ('vote', 'lokasi')
        ]

    def __str__(self):
        return f"Rank {self.peringkat}: {self.lokasi.kode}"

class Result(models.Model):
    TIEBREAK_CHOICES = [
        ('NONE', 'Tidak Ada Seri'),
        ('WP_SCORE', 'Tingkat 1 - Skor WP Kelompok'),
        ('KETUA_BALLOT', 'Tingkat 2 - Peringkat Surat Suara Ketua'),
        ('KETUA_MANUAL', 'Tingkat 3 - Penetapan Manual Ketua'),
    ]

    id = models.BigAutoField(primary_key=True)
    sesi = models.ForeignKey(SesiKeputusan, on_delete=models.CASCADE, related_name='results')
    lokasi = models.ForeignKey(Location, on_delete=models.CASCADE)
    skor_wp = models.DecimalField(max_digits=12, decimal_places=6)
    rank_wp = models.IntegerField()
    poin_borda = models.IntegerField(default=0)
    rank_final = models.IntegerField(null=True, blank=True)
    metode_tiebreak = models.CharField(max_length=20, choices=TIEBREAK_CHOICES, null=True, blank=True)
    catatan_tiebreak = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'results'
        verbose_name = 'Hasil Rekomendasi & Keputusan'
        verbose_name_plural = 'Hasil Rekomendasi & Keputusan'
        unique_together = ('sesi', 'lokasi')

    def __str__(self):
        return f"Hasil {self.lokasi.kode}: WP Rank {self.rank_wp}, Final {self.rank_final}"
