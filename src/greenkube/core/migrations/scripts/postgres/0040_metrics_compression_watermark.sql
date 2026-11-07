-- 0040: Compression watermark per metrics table (A3, BUG-02).
-- compressed_until is the exclusive end of the last closed hour that was
-- aggregated from its complete raw set. Compression never re-reads earlier
-- hours and raw pruning never deletes rows at or after the watermark.
CREATE TABLE IF NOT EXISTS metrics_compression_watermarks (
    table_name TEXT PRIMARY KEY,
    compressed_until TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
