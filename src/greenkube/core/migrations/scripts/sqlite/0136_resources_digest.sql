-- 0136: Resources and image digests of workload revisions and changes (MEA-08).
--
-- A measured saving depends on the containers' requests only, so a pod
-- template change that leaves them untouched (an image deployment, an env
-- change, a rollout restart) must not end a measurement. The applied detector
-- records the digest of the requests and limits and the digest of the images
-- next to the pod-template digest. Rows stored before this migration keep
-- NULL and the drift monitor reads the live requests before closing.
ALTER TABLE workload_spec_observations ADD COLUMN resources_digest TEXT;
ALTER TABLE workload_spec_observations ADD COLUMN image_digest TEXT;
ALTER TABLE change_events ADD COLUMN resources_digest_after TEXT;
ALTER TABLE change_events ADD COLUMN image_digest_after TEXT;
