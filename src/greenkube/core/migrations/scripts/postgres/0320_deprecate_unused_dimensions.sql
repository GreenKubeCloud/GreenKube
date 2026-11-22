-- 0320: Deprecate the unused dimension columns (B.4).
--
-- GreenKube no longer collects network receive and transmit, disk read and
-- write, ephemeral storage, or PVC request and usage. These columns are no
-- longer written or rolled up; their old values are kept. A migration after
-- the next release drops them (see docs/upgrading.md).
COMMENT ON COLUMN combined_metrics.network_receive_bytes IS 'Deprecated (B.4): no longer written; dropped after the next release.';
COMMENT ON COLUMN combined_metrics.network_transmit_bytes IS 'Deprecated (B.4): no longer written; dropped after the next release.';
COMMENT ON COLUMN combined_metrics.disk_read_bytes IS 'Deprecated (B.4): no longer written; dropped after the next release.';
COMMENT ON COLUMN combined_metrics.disk_write_bytes IS 'Deprecated (B.4): no longer written; dropped after the next release.';
COMMENT ON COLUMN combined_metrics.storage_request_bytes IS 'Deprecated (B.4): no longer written; dropped after the next release.';
COMMENT ON COLUMN combined_metrics.storage_usage_bytes IS 'Deprecated (B.4): no longer written; dropped after the next release.';
COMMENT ON COLUMN combined_metrics.ephemeral_storage_request_bytes IS 'Deprecated (B.4): no longer written; dropped after the next release.';
COMMENT ON COLUMN combined_metrics.ephemeral_storage_usage_bytes IS 'Deprecated (B.4): no longer written; dropped after the next release.';
COMMENT ON COLUMN combined_metrics_hourly.network_receive_bytes IS 'Deprecated (B.4): no longer written; dropped after the next release.';
COMMENT ON COLUMN combined_metrics_hourly.network_transmit_bytes IS 'Deprecated (B.4): no longer written; dropped after the next release.';
COMMENT ON COLUMN combined_metrics_hourly.disk_read_bytes IS 'Deprecated (B.4): no longer written; dropped after the next release.';
COMMENT ON COLUMN combined_metrics_hourly.disk_write_bytes IS 'Deprecated (B.4): no longer written; dropped after the next release.';
COMMENT ON COLUMN combined_metrics_hourly.storage_request_bytes IS 'Deprecated (B.4): no longer written; dropped after the next release.';
COMMENT ON COLUMN combined_metrics_hourly.storage_usage_bytes IS 'Deprecated (B.4): no longer written; dropped after the next release.';
