"""``/api/v2/ledger`` (C7): entries with cursor pagination, summary, CSV and FOCUS exports.

The exports must reconcile with ``GET /api/v1/recommendations/savings``.
"""

from __future__ import annotations

import csv
import io
import math
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.dependencies import get_recommendation_repository, get_savings_ledger_repository
from greenkube.api.routers.ledger import get_ledger_repo
from greenkube.core.db import DatabaseManager
from greenkube.core.savings_attributor import SavingsAttributor
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.storage.ledger_repository import LedgerRepository
from greenkube.storage.sqlite.savings_repository import SQLiteSavingsLedgerRepository
from greenkube.verify.ledger.identity import ClusterIdentityResolver, forget_cached_identities
from greenkube.verify.ledger.projector import LedgerProjector
from greenkube.verify.ledger.verification import verify_ledger

CLUSTER = "api-ledger"
UID = "kube-system-uid-api"
YEAR_SECONDS = 365.25 * 24 * 3600
HOURS_PER_YEAR = YEAR_SECONDS / 3600


def _hour(offset: int) -> datetime:
    return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=offset)


def _record(rec_id: int, applied_at: datetime, cost: float, co2e: float, **updates) -> RecommendationRecord:
    updates.setdefault("verification_status", "pending")
    return RecommendationRecord(
        id=rec_id,
        namespace="prod" if rec_id % 2 else "batch",
        pod_name=f"pod-{rec_id}",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="cut cpu",
        status=updates.pop("status", RecommendationStatus.APPLIED),
        applied_at=applied_at,
        carbon_saved_co2e_grams=co2e * HOURS_PER_YEAR,
        cost_saved=cost * HOURS_PER_YEAR,
        potential_savings_co2e_grams=co2e * HOURS_PER_YEAR,
        potential_savings_cost=cost * HOURS_PER_YEAR,
        **updates,
    )


@pytest.fixture
async def ledger(tmp_path):
    from greenkube.core.config import Config

    with patch.dict("os.environ", {"DB_TYPE": "sqlite", "DB_PATH": str(tmp_path / "api.sqlite")}):
        manager = DatabaseManager(Config())
    await manager.setup_sqlite(db_path=str(tmp_path / "api.sqlite"))
    v1 = SQLiteSavingsLedgerRepository(manager)
    repo = LedgerRepository(manager)
    forget_cached_identities()

    verified_at = _hour(-12) + timedelta(minutes=30)
    pending = [
        _record(1, _hour(-40) + timedelta(minutes=12), 0.02, 1.5),
        _record(2, _hour(-30), 0.5, 0.0),
        _record(3, _hour(-20) + timedelta(minutes=5), -0.01, -0.2),
    ]
    attributor = SavingsAttributor(v1, CLUSTER, initial_backfill=timedelta(hours=48), max_slots_per_run=60)
    await attributor.accrue(pending, now=verified_at - timedelta(minutes=1))
    verified = pending[0].model_copy(
        update={
            "status": RecommendationStatus.VERIFIED,
            "verified_at": verified_at,
            "verification_status": "passed",
            "measured_cost_saved": 0.015 * HOURS_PER_YEAR,
            "measured_co2e_saved_grams": 1.1 * HOURS_PER_YEAR,
        }
    )
    await attributor.accrue([verified, *pending[1:]], now=_hour(0) + timedelta(minutes=1))
    await v1.supersede_for_recommendation(2)

    async def uid():
        return UID

    async def records():
        return [verified]

    projector = LedgerProjector(repo, ClusterIdentityResolver(repo, CLUSTER, reader=uid), records=records)
    await projector.sync()
    await verify_ledger(repo, checkpoints={})
    yield v1, repo
    forget_cached_identities()
    await manager.close()


@contextmanager
def _client(v1, repo):
    with patch.dict("os.environ", {"GREENKUBE_API_KEY": "", "CLUSTER_NAME": CLUSTER}, clear=False):
        from greenkube.core.config import Config

        cfg = Config()
        with patch("greenkube.core.config.config", cfg), patch("greenkube.core.config.get_config", return_value=cfg):
            app = create_app()
            reco_repo = MagicMock()
            reco_repo.get_applied_recommendations = AsyncMock(return_value=[])
            app.dependency_overrides[get_ledger_repo] = lambda: repo
            app.dependency_overrides[get_savings_ledger_repository] = lambda: v1
            app.dependency_overrides[get_recommendation_repository] = lambda: reco_repo
            with TestClient(app) as client:
                yield client
            app.dependency_overrides.clear()


def _rows(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


def _sum(rows, *, method: str, quantity: str, book: str = "accrued", column: str = "window_value") -> float:
    return math.fsum(
        float(row[column])
        for row in rows
        if row["method"] == method and row["quantity"] == quantity and row["book"] == book
    )


async def test_entries_are_paginated_newest_first_with_a_cursor(ledger):
    v1, repo = ledger
    with _client(v1, repo) as client:
        seen: list[int] = []
        cursor = None
        while True:
            params = {"limit": 25, **({"cursor": cursor} if cursor else {})}
            body = client.get("/api/v2/ledger/entries", params=params).json()
            assert body["cluster_uid"] == UID
            seen.extend(item["seq"] for item in body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert seen == sorted(seen, reverse=True)
        assert seen == list(range(len(seen), 0, -1))

        restatements = client.get("/api/v2/ledger/entries", params={"entry_kind": "restatement", "limit": 5}).json()
        assert restatements["items"] and all(item["restates_entry_id"] for item in restatements["items"])
        assert client.get("/api/v2/ledger/entries", params={"method": "bogus"}).status_code == 422
        assert client.get("/api/v2/ledger/entries", params={"cursor": "%%%"}).status_code == 400
        assert client.get("/api/v2/ledger/entries", params={"last": "7d", "start": "2026-01-01"}).status_code == 400


async def test_summary_reconciles_with_the_savings_report_and_reports_the_chain(ledger):
    v1, repo = ledger
    with _client(v1, repo) as client:
        # All time reconciles exactly. With last=18h both endpoints compute "now" a few
        # milliseconds apart, which moves the prorated share of the partial first slot.
        for params, rel in (({}, 1e-9), ({"last": "18h"}, 1e-4)):
            report = client.get("/api/v1/recommendations/savings", params=params).json()
            summary = client.get("/api/v2/ledger/summary", params=params).json()
            totals = {(item["method"], item["quantity"]): item for item in summary["totals"]}
            assert totals[("measured_allocated", "cost")]["accrued"] == pytest.approx(
                report["realized_to_date"]["cost"], rel=rel
            )
            assert totals[("measured_allocated", "co2e_g")]["accrued"] == pytest.approx(
                report["realized_to_date"]["co2e_grams"], rel=rel
            )
            assert totals[("projected", "cost")]["accrued"] == pytest.approx(
                report["projected_to_date"]["cost"], rel=rel
            )
        summary = client.get("/api/v2/ledger/summary").json()
        totals = {(item["method"], item["quantity"]): item for item in summary["totals"]}
        # BUG-39: the measurement restated the projection of recommendation 1 (signed, never floored).
        assert totals[("measured_allocated", "cost")]["restated"] > totals[("measured_allocated", "cost")]["accrued"]
        assert totals[("projected", "cost")]["restated"] < totals[("projected", "cost")]["accrued"]
        assert summary["entries_by_kind"]["restatement"] > 0
        assert summary["chain"]["ok"] is True
        assert summary["chain"]["head_seq"] == summary["chain"]["verified_head_seq"]


async def test_csv_and_focus_exports_reconcile_with_the_savings_report(ledger):
    v1, repo = ledger
    with _client(v1, repo) as client:
        for params, rel in (({}, 1e-9), ({"last": "18h"}, 1e-4)):
            report = client.get("/api/v1/recommendations/savings", params=params).json()
            exported = client.get("/api/v2/ledger/export", params=params)
            assert exported.status_code == 200
            assert exported.headers["content-type"].startswith("text/csv")
            rows = _rows(exported.text)
            assert _sum(rows, method="measured_allocated", quantity="cost") == pytest.approx(
                report["realized_to_date"]["cost"], rel=rel
            )
            assert _sum(rows, method="projected", quantity="co2e_g") == pytest.approx(
                report["projected_to_date"]["co2e_grams"], rel=rel
            )

            focus = _rows(client.get("/api/v2/ledger/export", params={"format": "focus", **params}).text)
            accrued = [row for row in focus if row["x-greenkube-book"] == "accrued"]
            measured = [row for row in accrued if row["x-greenkube-method"] == "measured_allocated"]
            assert math.fsum(float(row["x-greenkube-saving"]) for row in measured) == pytest.approx(
                report["realized_to_date"]["cost"], rel=rel
            )
            assert math.fsum(float(row["x-greenkube-co2e-g"]) for row in measured) == pytest.approx(
                report["realized_to_date"]["co2e_grams"], rel=rel
            )
            assert math.fsum(float(row["EffectiveCost"]) for row in measured) == pytest.approx(
                -report["realized_to_date"]["cost"], rel=rel
            )
        focus = _rows(client.get("/api/v2/ledger/export", params={"format": "focus"}).text)
        assert {"BillingAccountId", "ChargePeriodStart", "EffectiveCost", "x-greenkube-pr-url"} <= set(focus[0])
        assert {row["ChargeCategory"] for row in focus} == {"Credit", "Adjustment"}
        assert all(row["BillingAccountId"] == UID for row in focus)


async def test_an_unresolved_cluster_returns_an_empty_ledger(tmp_path):
    from greenkube.core.config import Config

    with patch.dict("os.environ", {"DB_TYPE": "sqlite", "DB_PATH": str(tmp_path / "empty.sqlite")}):
        manager = DatabaseManager(Config())
    await manager.setup_sqlite(db_path=str(tmp_path / "empty.sqlite"))
    try:
        with _client(SQLiteSavingsLedgerRepository(manager), LedgerRepository(manager)) as client:
            body = client.get("/api/v2/ledger/entries").json()
            assert body["items"] == [] and body["cluster_uid"] is None
            assert client.get("/api/v2/ledger/summary").json()["totals"] == []
            assert _rows(client.get("/api/v2/ledger/export").text) == []
    finally:
        await manager.close()


async def test_a_single_window_bound_is_rejected_everywhere(ledger):
    v1, repo = ledger
    start = _hour(-10).isoformat()
    with _client(v1, repo) as client:
        for path in ("/api/v2/ledger/entries", "/api/v2/ledger/summary", "/api/v2/ledger/export"):
            assert client.get(path, params={"start": start}).status_code == 400
            assert client.get(path, params={"end": start}).status_code == 400


async def test_exports_guard_formula_cells_and_rewrite_pull_request_urls(ledger, monkeypatch):
    from greenkube.verify.ledger.model import LedgerEntryDraft

    v1, repo = ledger
    start = _hour(-3)
    await repo.append(
        UID,
        CLUSTER,
        [
            LedgerEntryDraft(
                entry_key=f"injection:{quantity}",
                group_key="injection",
                entry_kind="accrual",
                book="accrued",
                reason="accrual",
                method="user_declared",
                quantity=quantity,
                value=-1.5,
                unit="USD" if quantity == "cost" else "gCO2e",
                currency="USD" if quantity == "cost" else None,
                method_version='=HYPERLINK("http://example.invalid")',
                period_start=start,
                period_end=start + timedelta(hours=1),
                created_by="test",
                recommendation_id=99,
                recommendation_ids=[99],
                pr_url="https://gitea.internal/api/v1/repos/o/r/pulls/7",
            )
            for quantity in ("cost", "co2e_g")
        ],
    )
    monkeypatch.setattr(
        "greenkube.api.routers.automation.external_pull_request_url",
        lambda url: url.replace("/api/v1/repos/", "/") if url else url,
    )
    with _client(v1, repo) as client:
        rows = [row for row in _rows(client.get("/api/v2/ledger/export").text) if row["recommendation_id"] == "99"]
        assert len(rows) == 2
        assert all(row["method_version"].startswith("'=") for row in rows)
        assert all(row["pr_url"] == "https://gitea.internal/o/r/pulls/7" for row in rows)
        assert all(float(row["value"]) == -1.5 for row in rows)  # signed numbers stay numeric
        focus = _rows(client.get("/api/v2/ledger/export", params={"format": "focus"}).text)
        injected = [row for row in focus if row["ResourceId"] == "recommendation/99"]
        assert len(injected) == 1
        assert injected[0]["x-greenkube-method-version"].startswith("'=")
        assert injected[0]["x-greenkube-pr-url"] == "https://gitea.internal/o/r/pulls/7"
        assert float(injected[0]["EffectiveCost"]) == 1.5
