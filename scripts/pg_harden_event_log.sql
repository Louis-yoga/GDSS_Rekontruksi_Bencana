-- PostgreSQL Production Hardening Script (AC-15 & Zona 6)
-- Menjamin tabel event_log bersifat Append-Only di level database (tidak dapat di-UPDATE atau di-DELETE).
-- Jalankan sebagai superuser PostgreSQL setelah migrasi Django selesai:
-- psql -U postgres -d gdss_db -f scripts/pg_harden_event_log.sql

REVOKE UPDATE, DELETE, TRUNCATE ON TABLE event_log FROM PUBLIC;
REVOKE UPDATE, DELETE, TRUNCATE ON TABLE event_log FROM app_user;
GRANT SELECT, INSERT ON TABLE event_log TO app_user;
GRANT USAGE, SELECT ON SEQUENCE event_log_id_seq TO app_user;
