"""The scheduled lifecycle job builds its verifier with a health collector (C0.9, BUG-35)."""

from unittest.mock import AsyncMock, MagicMock, patch

from greenkube.cli import start
from greenkube.collectors.workload_health_collector import WorkloadHealthCollector


async def test_lifecycle_job_wires_health_collector_and_measurement_store():
    captured: dict = {}
    detector_kwargs: dict = {}

    class CapturingVerifier:
        def __init__(self, lifecycle, combined_repo, **kwargs):
            captured.update(kwargs)

        async def verify_due(self):
            return []

    repo = MagicMock()
    # Expiry reads the active recommendations and expires each one with a compare-and-swap.
    repo.get_recommendations_by_statuses = AsyncMock(return_value=[])
    detector = MagicMock()
    detector.detect = AsyncMock(return_value=[])
    close = AsyncMock()
    change_repo = MagicMock()
    change_repo.list_statuses = AsyncMock(return_value=[])

    def build_detector(*args, **kwargs):
        detector_kwargs.update(kwargs)
        return detector

    with (
        patch("greenkube.core.factory.get_recommendation_repository", return_value=repo),
        patch("greenkube.core.factory.get_combined_metrics_repository", return_value=MagicMock()),
        patch("greenkube.core.factory.get_gitops_repository", return_value=MagicMock()),
        patch("greenkube.core.optimization.applied_detector.AppliedDetector", side_effect=build_detector),
        patch("greenkube.storage.change_repository.get_change_repository", return_value=change_repo),
        patch("greenkube.core.factory.get_pull_request_repository", return_value=MagicMock()),
        patch("greenkube.core.optimization.verifier.RecommendationVerifier", CapturingVerifier),
        patch(
            "greenkube.storage.measurement_repository.get_savings_measurement_repository",
            return_value="measurements",
        ),
        patch.object(WorkloadHealthCollector, "close", close),
    ):
        await start.run_recommendation_lifecycle()

    assert isinstance(captured["health_collector"], WorkloadHealthCollector)
    assert captured["measurement_repo"] == "measurements"
    close.assert_awaited_once()
    # Apply detection records changes for the change-measurement pipeline (C1), which then runs.
    from greenkube.verify.service import ChangeMeasurementService

    assert isinstance(detector_kwargs["change_recorder"], ChangeMeasurementService)
    change_repo.list_statuses.assert_awaited()
