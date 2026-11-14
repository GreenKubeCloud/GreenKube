"""Rollups of ``opencost`` and ``opencost_derived`` rows stay trusted; any other mix is ``mixed``."""

import sqlite3

import pytest

from greenkube.core.workload_usage import _source_aggregate
from greenkube.models.provenance import combine_cost_sources, cost_source_aggregate_sql, is_cost_trusted

CASES = [
    ([], None),
    ([None, None], None),
    (["opencost"], "opencost"),
    (["opencost", "opencost"], "opencost"),
    (["opencost", "opencost_derived"], "opencost_derived"),
    (["opencost_derived", "opencost", "opencost_derived"], "opencost_derived"),
    (["opencost", "kubecost"], "mixed"),
    (["opencost_derived", "pricing_catalog"], "mixed"),
    (["opencost_derived", "none"], "mixed"),
    (["opencost", "opencost_derived", "mixed"], "mixed"),
    (["opencost", "opencost_derived", None], "mixed"),
    (["custom_rate", "pricing_catalog"], "mixed"),
]


@pytest.mark.parametrize(("sources", "expected"), CASES)
def test_python_rollups(sources, expected) -> None:
    assert combine_cost_sources(sources) == expected
    assert _source_aggregate(sources) == expected


@pytest.mark.parametrize(("sources", "expected"), CASES)
def test_sql_rollup_matches_python(sources, expected) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (cost_source TEXT)")
    conn.executemany("INSERT INTO t VALUES (?)", [(value,) for value in sources])
    (value,) = conn.execute(f"SELECT {cost_source_aggregate_sql('cost_source')} FROM t").fetchone()
    assert value == (expected if sources else None)


def test_only_the_opencost_family_mix_is_trusted() -> None:
    assert is_cost_trusted({"cost_source": combine_cost_sources(["opencost", "opencost_derived"]), "total_cost": 1e-6})
    assert not is_cost_trusted({"cost_source": combine_cost_sources(["opencost", "pricing_catalog"]), "total_cost": 1})
