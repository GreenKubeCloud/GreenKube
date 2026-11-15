-- 0147: 64-bit ids and sequences (STO-05).
--
-- PostgreSQL only. A SQLite INTEGER PRIMARY KEY is already a 64-bit rowid.
-- The script keeps the version numbers of both dialects aligned.
SELECT 1;
