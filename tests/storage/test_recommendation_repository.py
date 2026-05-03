# tests/storage/test_recommendation_repository.py
"""
Tests for the RecommendationRepository implementations (SQLite and Postgres).
Uses TDD methodology — tests written before implementation.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from greenkube.models.metrics import (
    Recommendation,
    RecommendationRecord,
    RecommendationStatus,
    RecommendationType,
)


def _make_record(
    pod_name: str = "test-pod",
    namespace: str = "default",
    rec_type: RecommendationType = RecommendationType.ZOMBIE_POD,
    priority: str = "high",
    created_at: datetime = None,
    potential_savings_cost: float = 0.5,
    potential_savings_co2e_grams: float = 1.0,
) -> RecommendationRecord:
    """Helper to create a RecommendationRecord for testing."""
    return RecommendationRecord(
        pod_name=pod_name,
        namespace=namespace,
        type=rec_type,
        description=f"Test recommendation for {pod_name}",
        reason="Test reason",
        priority=priority,
        potential_savings_cost=potential_savings_cost,
        potential_savings_co2e_grams=potential_savings_co2e_grams,
        created_at=created_at or datetime(2026, 2, 20, 12, 0, 0, tzinfo=timezone.utc),
    )


def _make_applied_record() -> RecommendationRecord:
    """Helper to create an applied RecommendationRecord with realized savings."""
    return RecommendationRecord(
        pod_name="historical-pod",
        namespace="commerce",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="Historical applied recommendation",
        reason="Observed overprovisioning for several months",
        priority="high",
        status=RecommendationStatus.APPLIED,
        potential_savings_cost=312.5,
        potential_savings_co2e_grams=1840.0,
        current_cpu_request_millicores=1200,
        recommended_cpu_request_millicores=650,
        actual_cpu_request_millicores=700,
        current_memory_request_bytes=2 * 1024**3,
        recommended_memory_request_bytes=1536 * 1024**2,
        actual_memory_request_bytes=1536 * 1024**2,
        applied_at=datetime(2026, 2, 25, 9, 30, 0, tzinfo=timezone.utc),
        carbon_saved_co2e_grams=1800.0,
        cost_saved=275.0,
        created_at=datetime(2026, 2, 20, 12, 0, 0, tzinfo=timezone.utc),
    )


class TestRecommendationRecordModel:
    """Tests for the RecommendationRecord Pydantic model."""

    def test_create_record_with_defaults(self):
        """Should create a record with default values."""
        rec = RecommendationRecord(
            pod_name="my-pod",
            namespace="default",
            type=RecommendationType.ZOMBIE_POD,
            description="A zombie pod",
        )
        assert rec.pod_name == "my-pod"
        assert rec.namespace == "default"
        assert rec.type == RecommendationType.ZOMBIE_POD
        assert rec.priority == "medium"
        assert rec.id is None
        assert rec.created_at is not None

    def test_from_recommendation(self):
        """Should convert a Recommendation to a RecommendationRecord."""
        rec = Recommendation(
            pod_name="zombie-pod",
            namespace="prod",
            type=RecommendationType.ZOMBIE_POD,
            description="Pod is a zombie",
            reason="Cost with no energy",
            priority="high",
            potential_savings_cost=1.5,
            potential_savings_co2e_grams=0.5,
        )
        ts = datetime(2026, 2, 20, 10, 0, 0, tzinfo=timezone.utc)
        record = RecommendationRecord.from_recommendation(rec, created_at=ts)
        assert record.pod_name == "zombie-pod"
        assert record.namespace == "prod"
        assert record.type == RecommendationType.ZOMBIE_POD
        assert record.priority == "high"
        assert record.potential_savings_cost == 1.5
        assert record.created_at == ts

    def test_from_recommendation_default_timestamp(self):
        """Should use current time if no created_at is provided."""
        rec = Recommendation(
            pod_name="pod-1",
            namespace="default",
            type=RecommendationType.RIGHTSIZING_CPU,
            description="Oversized CPU",
        )
        record = RecommendationRecord.from_recommendation(rec)
        assert record.created_at is not None
        assert record.created_at.tzinfo is not None


class TestSQLiteRecommendationRepository:
    """Tests for the SQLite recommendation repository."""

    @pytest.fixture
    def mock_db_manager(self):
        """Returns a mock DatabaseManager for SQLite."""
        from contextlib import asynccontextmanager

        manager = AsyncMock()
        mock_conn = AsyncMock()

        @asynccontextmanager
        async def fake_scope():
            yield mock_conn

        manager.connection_scope = fake_scope
        manager._mock_conn = mock_conn
        return manager

    @pytest.fixture
    def repo(self, mock_db_manager):
        """Returns a SQLiteRecommendationRepository with mocked DB."""
        from greenkube.storage.sqlite.recommendation_repository import (
            SQLiteRecommendationRepository,
        )

        return SQLiteRecommendationRepository(mock_db_manager)

    @pytest.mark.asyncio
    async def test_save_recommendations_returns_count(self, repo, mock_db_manager):
        """Should return the number of records saved."""
        records = [_make_record(), _make_record(pod_name="pod-2")]
        count = await repo.save_recommendations(records)
        assert count == 2

    @pytest.mark.asyncio
    async def test_save_empty_list_returns_zero(self, repo):
        """Should return 0 for an empty list."""
        count = await repo.save_recommendations([])
        assert count == 0

    @pytest.mark.asyncio
    async def test_save_recommendations_persists_applied_lifecycle_fields(self, repo, mock_db_manager):
        """SQLite should persist applied recommendation lifecycle fields when seeding demo data."""
        record = _make_applied_record()

        await repo.save_recommendations([record])

        query, params = mock_db_manager._mock_conn.execute.await_args.args
        assert "applied_at" in query
        assert "actual_cpu_request_millicores" in query
        assert "actual_memory_request_bytes" in query
        assert "carbon_saved_co2e_grams" in query
        assert "cost_saved" in query
        assert "applied" in params
        assert "2026-02-25T09:30:00Z" in params
        assert 1800.0 in params
        assert 275.0 in params

    @pytest.mark.asyncio
    async def test_get_recommendations_with_filters(self, repo, mock_db_manager):
        """Should apply type and namespace filters."""
        mock_conn = mock_db_manager._mock_conn
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        mock_conn.row_factory = None

        start = datetime(2026, 2, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 28, tzinfo=timezone.utc)
        results = await repo.get_recommendations(
            start=start,
            end=end,
            rec_type="ZOMBIE_POD",
            namespace="default",
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_get_savings_summary_filters_by_applied_at_window(self, repo, mock_db_manager):
        """SQLite savings summary should filter applied recommendations by applied_at."""
        mock_conn = mock_db_manager._mock_conn
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        mock_conn.row_factory = None

        start = datetime(2026, 2, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 8, tzinfo=timezone.utc)
        await repo.get_savings_summary(namespace="default", start=start, end=end)

        query, params = mock_conn.execute.await_args.args
        assert "applied_at >= ?" in query
        assert "applied_at < ?" in query
        assert params[-2:] == ["2026-02-01T00:00:00Z", "2026-02-08T00:00:00Z"]


class TestPostgresRecommendationRepository:
    """Tests for the Postgres recommendation repository."""

    @pytest.fixture
    def mock_db_manager(self):
        """Returns a mock DatabaseManager for Postgres."""
        from contextlib import asynccontextmanager

        manager = AsyncMock()
        mock_conn = AsyncMock()

        @asynccontextmanager
        async def fake_scope():
            yield mock_conn

        manager.connection_scope = fake_scope
        manager._mock_conn = mock_conn
        return manager

    @pytest.fixture
    def repo(self, mock_db_manager):
        """Returns a PostgresRecommendationRepository with mocked DB."""
        from greenkube.storage.postgres.recommendation_repository import (
            PostgresRecommendationRepository,
        )

        return PostgresRecommendationRepository(mock_db_manager)

    @pytest.mark.asyncio
    async def test_save_recommendations_returns_count(self, repo, mock_db_manager):
        """Should return the number of records saved."""
        records = [_make_record(), _make_record(pod_name="pod-2")]
        count = await repo.save_recommendations(records)
        assert count == 2

    @pytest.mark.asyncio
    async def test_save_empty_list_returns_zero(self, repo):
        """Should return 0 for an empty list."""
        count = await repo.save_recommendations([])
        assert count == 0

    @pytest.mark.asyncio
    async def test_save_recommendations_persists_applied_lifecycle_fields(self, repo, mock_db_manager):
        """Postgres should persist applied recommendation lifecycle fields when seeding demo data."""
        record = _make_applied_record()

        await repo.save_recommendations([record])

        query, data = mock_db_manager._mock_conn.executemany.await_args.args
        saved = data[0]
        assert "applied_at" in query
        assert "actual_cpu_request_millicores" in query
        assert "actual_memory_request_bytes" in query
        assert "carbon_saved_co2e_grams" in query
        assert "cost_saved" in query
        assert record.applied_at in saved
        assert record.cost_saved in saved
        assert record.carbon_saved_co2e_grams in saved

    @pytest.mark.asyncio
    async def test_get_recommendations_with_filters(self, repo, mock_db_manager):
        """Should apply type and namespace filters."""
        mock_conn = mock_db_manager._mock_conn
        mock_conn.fetch = AsyncMock(return_value=[])

        start = datetime(2026, 2, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 28, tzinfo=timezone.utc)
        results = await repo.get_recommendations(
            start=start,
            end=end,
            rec_type="ZOMBIE_POD",
            namespace="default",
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_get_savings_summary_filters_by_applied_at_window(self, repo, mock_db_manager):
        """Postgres savings summary should filter applied recommendations by applied_at."""
        mock_conn = mock_db_manager._mock_conn
        mock_conn.fetch = AsyncMock(return_value=[])

        start = datetime(2026, 2, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 8, tzinfo=timezone.utc)
        await repo.get_savings_summary(namespace="default", start=start, end=end)

        query = mock_conn.fetch.await_args.args[0]
        params = mock_conn.fetch.await_args.args[1:]
        assert "applied_at >= $2" in query
        assert "applied_at < $3" in query
        assert params == ("default", start, end)
