"""Change measurement never compares windows priced on different bases (COST-01 acceptance d)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from greenkube.verify.confounders import ConfounderDetector, is_contaminated
from greenkube.verify.model import HourBucket, TimeWindow
from greenkube.verify.series import hourly_buckets

HOUR = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)


def _row(moment: datetime, source: str):
    return SimpleNamespace(
        timestamp=moment,
        duration_seconds=3600,
        sample_count=1,
        cpu_request=1000,
        memory_request=0,
        cpu_usage_millicores=None,
        memory_usage_bytes=None,
        usage_source="prometheus",
        joules=0.0,
        node_instance_type=None,
        total_cost=0.05,
        cost_cpu=0.05,
        cost_ram=0.0,
        cost_source=source,
        co2e_grams=0.0,
        intensity_source="realtime",
        power_source="instance_profile",
        grid_intensity=None,
        pue=None,
        embodied_co2e_grams=None,
    )


def test_hour_buckets_record_their_price_basis():
    window = TimeWindow(start=HOUR, end=HOUR + timedelta(hours=2))
    rows = [_row(HOUR, "opencost"), _row(HOUR + timedelta(hours=1), "pricing_catalog")]
    buckets = hourly_buckets(rows, window, 3600)
    assert buckets[0].cost_bases == {"allocated": 1.0}
    assert buckets[1].cost_bases == {"list_price": 1.0}


def test_baseline_on_opencost_and_after_window_on_list_prices_is_contaminated():
    pre = [HourBucket(hour=HOUR, coverage=1, cost=0.05, cost_coverage=1, cost_bases={"allocated": 1.0})]
    post = [
        HourBucket(
            hour=HOUR + timedelta(days=7), coverage=1, cost=0.03, cost_coverage=1, cost_bases={"list_price": 1.0}
        )
    ]
    findings = ConfounderDetector._price_basis(pre, post)
    assert [item.kind for item in findings] == ["price_basis_change"]
    assert is_contaminated(findings)


def test_same_price_basis_is_not_a_confounder():
    pre = [HourBucket(hour=HOUR, cost=0.05, cost_coverage=1, cost_bases={"allocated": 1.0})]
    post = [HourBucket(hour=HOUR, cost=0.03, cost_coverage=1, cost_bases={"allocated": 1.0})]
    assert ConfounderDetector._price_basis(pre, post) == []
    legacy = [HourBucket(hour=HOUR, cost=0.03, cost_coverage=1)]
    assert ConfounderDetector._price_basis(pre, legacy) == []
