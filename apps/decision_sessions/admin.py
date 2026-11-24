from django.contrib import admin
from apps.decision_sessions.models import (
    SesiKeputusan, Location, Criteria, LocationRef,
    Assessment, AssessmentScore, Vote, VoteItem, Result
)

@admin.register(SesiKeputusan)
class SesiKeputusanAdmin(admin.ModelAdmin):
    list_display = ('id', 'judul', 'status', 'versi', 'nilai_k', 'kuorum_parsial', 'deadline_aktif')
    list_filter = ('status', 'kuorum_parsial')
    search_fields = ('judul',)

@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ('id', 'sesi', 'kode', 'nama', 'wilayah')
    list_filter = ('sesi',)
    search_fields = ('kode', 'nama', 'wilayah')

@admin.register(Criteria)
class CriteriaAdmin(admin.ModelAdmin):
    list_display = ('id', 'sesi', 'kode', 'nama', 'tipe', 'bobot')
    list_filter = ('sesi', 'tipe')
    search_fields = ('kode', 'nama')

@admin.register(Assessment)
class AssessmentAdmin(admin.ModelAdmin):
    list_display = ('id', 'sesi', 'seat', 'user', 'status', 'versi_draf', 'submitted_at')
    list_filter = ('sesi', 'status', 'seat')

@admin.register(AssessmentScore)
class AssessmentScoreAdmin(admin.ModelAdmin):
    list_display = ('id', 'assessment', 'lokasi', 'kriteria', 'skor')
    list_filter = ('assessment__sesi', 'kriteria', 'skor')

@admin.register(Vote)
class VoteAdmin(admin.ModelAdmin):
    list_display = ('id', 'sesi', 'seat', 'user', 'abstain', 'submitted_at')
    list_filter = ('sesi', 'abstain', 'seat')

@admin.register(VoteItem)
class VoteItemAdmin(admin.ModelAdmin):
    list_display = ('id', 'vote', 'peringkat', 'lokasi')
    list_filter = ('vote__sesi', 'peringkat')

@admin.register(Result)
class ResultAdmin(admin.ModelAdmin):
    list_display = ('id', 'sesi', 'lokasi', 'skor_wp', 'rank_wp', 'poin_borda', 'rank_final', 'metode_tiebreak')
    list_filter = ('sesi', 'metode_tiebreak')
