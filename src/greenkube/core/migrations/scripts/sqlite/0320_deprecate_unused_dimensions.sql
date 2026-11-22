-- 0320: Deprecate the unused dimension columns (B.4).
--
-- GreenKube no longer collects network receive and transmit, disk read and
-- write, ephemeral storage, or PVC request and usage. These columns are no
-- longer written or rolled up; their old values are kept. A migration after
-- the next release drops them (see docs/upgrading.md).
--
-- SQLite has no column comments: nothing to change.
SELECT 1;
