"""Savings ledger v2 currencies (production readiness, C6/C7).

Each v1 accrual is stamped with the currency its recommendation was priced in
(``impact.currency``), not with today's configured currency, and the summary
never adds amounts of different currencies.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from greenkube.core.db import DatabaseManager
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.models.savings import METHOD_PROJECTED, SavingsLedgerRecord
from greenkube.storage.ledger_repository import LedgerRepository
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository
from greenkube.storage.sqlite.savings_repository import SQLiteSavingsLedgerRepository
from greenkube.verify.ledger.identity import ClusterIdentityResolver, forget_cached_identities
from greenkube.verify.ledger.projector import LedgerProjector
from tests.api.test_ledger_api import _client  # pyrefly: ignore[missing-import]

CLUSTER = "api-ledger"
UID = "kube-system-uid-currency"


def _hour(offset: int) -> datetime:
    return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=offset)


@pytest.fixture
async def ledger(tmp_path):
    from greenkube.core.config import Config

    with patch.dict("os.environ", {"DB_TYPE": "sqlite", "DB_PATH": str(tmp_path / "currency.sqlite")}):
        manager = DatabaseManager(Config())
    await manager.setup_sqlite(db_path=str(tmp_path / "currency.sqlite"))
    forget_cached_identities()
    recommendations = SQLiteRecommendationRepository(manager)
    await recommendations.save_recommendations(
        [
            RecommendationRecord(
                pod_name=f"pod-{currency.lower()}",
                namespace="prod",
                type=RecommendationType.RIGHTSIZING_CPU,
                description="cut cpu",
                status=RecommendationStatus.APPLIED,
                impact={"model_version": "2.0.0", "currency": currency, "allocated_cost_annual": 100.0},
            )
            for currency in ("EUR", "GBP")
        ]
    )
    stored = await recommendations.get_recommendations_by_statuses(["applied"])
    ids = {record.impact["currency"]: int(record.id) for record in stored if record.impact and record.id is not None}
    v1 = SQLiteSavingsLedgerRepository(manager)
    start = _hour(-2)
    await v1.save_records(
        [
            SavingsLedgerRecord(
                recommendation_id=rec_id,
                cluster_name=CLUSTER,
                namespace="prod",
                recommendation_type="RIGHTSIZING_CPU",
                cost_saved_dollars=cost,
                co2e_saved_grams=1.0,
                period_seconds=3600,
                timestamp=start + timedelta(hours=1),
                period_start=start,
                period_end=start + timedelta(hours=1),
                slot_start=start,
                measurement_method=METHOD_PROJECTED,
            )
            for rec_id, cost in ((ids["EUR"], 2.0), (ids["GBP"], 3.0))
        ]
    )
    repo = LedgerRepository(manager)

    async def uid():
        return UID

    # The configured currency is USD today: it must not relabel accruals priced in EUR or GBP.
    projector = LedgerProjector(repo, ClusterIdentityResolver(repo, CLUSTER, reader=uid), currency="USD")
    await projector.sync()
    yield v1, repo, ids
    forget_cached_identities()
    await manager.close()


async def test_each_accrual_keeps_the_currency_it_was_priced_in(ledger):
    _, repo, ids = ledger
    entries = await repo.list_entries(UID, filters={"quantity": "cost"}, limit=10)
    by_rec = {entry.recommendation_id: entry for entry in entries}
    assert (by_rec[ids["EUR"]].currency, by_rec[ids["EUR"]].unit) == ("EUR", "EUR")
    assert (by_rec[ids["GBP"]].currency, by_rec[ids["GBP"]].unit) == ("GBP", "GBP")


async def test_the_summary_never_sums_across_currencies(ledger):
    v1, repo, _ = ledger
    with _client(v1, repo) as client:
        summary = client.get("/api/v2/ledger/summary").json()
    cost = [item for item in summary["totals"] if item["quantity"] == "cost"]
    assert sorted((item["currency"], item["accrued"]) for item in cost) == [("EUR", 2.0), ("GBP", 3.0)]
    assert summary["currencies"] == ["EUR", "GBP"]
    assert all(item["method"] == "projected" and item["billed"] is False for item in cost)
    assert "measured_allocated" in summary["methods"] and "realized_billed" in summary["methods"]
