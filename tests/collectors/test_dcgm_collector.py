"""DCGM series parsing and pod attribution (roadmap B5)."""

from greenkube.collectors.dcgm_collector import power_query, samples_by_step, series_identity, utilization_query


def test_queries_average_over_the_step():
    assert power_query("5m") == "avg_over_time(DCGM_FI_DEV_POWER_USAGE[5m])"
    assert utilization_query("300s") == "avg_over_time(DCGM_FI_DEV_GPU_UTIL[300s])"


def test_honor_labels_attribution():
    identity = series_identity(
        {"gpu": "0", "UUID": "GPU-1", "modelName": "NVIDIA L4", "namespace": "ml", "pod": "train-0", "Hostname": "n1"}
    )
    assert (identity.device, identity.model_name) == ("GPU-1", "NVIDIA L4")
    assert (identity.namespace, identity.pod) == ("ml", "train-0")
    assert identity.node_hints == ("n1",)


def test_exported_labels_win_over_the_exporter_target_labels():
    identity = series_identity(
        {
            "gpu": "0",
            "namespace": "gpu-operator",
            "pod": "nvidia-dcgm-exporter-abcde",
            "exported_namespace": "ml",
            "exported_pod": "train-0",
        }
    )
    assert (identity.namespace, identity.pod) == ("ml", "train-0")
    assert identity.exporter_pod == "nvidia-dcgm-exporter-abcde"


def test_gpu_seen_only_through_its_exporter_is_unattributed():
    identity = series_identity({"gpu": "1", "namespace": "gpu-operator", "pod": "nvidia-dcgm-exporter-abcde"})
    assert identity.pod is None and identity.namespace is None
    assert identity.exporter_pod == "nvidia-dcgm-exporter-abcde"


def test_power_and_utilization_are_joined_per_gpu_and_step():
    labels = {"gpu": "0", "UUID": "GPU-1", "namespace": "ml", "pod": "train-0"}

    def iterate(series):
        for ts, value in series["values"]:
            yield int(ts), float(value)

    steps = samples_by_step(
        [{"metric": labels, "values": [[0, "150"], [300, "160"]]}],
        [{"metric": labels, "values": [[0, "90"]]}, {"metric": {"namespace": "x", "pod": "cpu"}, "values": [[0, "1"]]}],
        iterate,
    )
    assert sorted(steps) == [0, 300]
    (first,) = steps[0]
    assert (first.power_watts, first.utilization_pct) == (150.0, 90.0)
    (second,) = steps[300]
    assert (second.power_watts, second.utilization_pct) == (160.0, None)
