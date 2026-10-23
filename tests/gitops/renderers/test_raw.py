import re
from pathlib import Path

import pytest

from greenkube.gitops.contracts import RendererKind, ResourceTarget
from greenkube.gitops.renderers import (
    RawRenderer,
    RawRenderError,
    RawRenderLimitError,
    RenderLimits,
    render_raw,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_render_yaml_documents_normalizes_resources_and_retains_source_map():
    result = render_raw({"apps/payments/resources.yaml": _fixture("multi-document.yaml")})

    assert result.renderer is RendererKind.RAW
    assert [resource.target for resource in result.resources] == [
        ResourceTarget(
            api_version="apps/v1",
            kind="Deployment",
            namespace="payments",
            name="api",
        ),
        ResourceTarget(
            api_version="v1",
            kind="Service",
            namespace="payments",
            name="api",
        ),
    ]
    assert [(resource.source_path, resource.document_index) for resource in result.resources] == [
        ("apps/payments/resources.yaml", 0),
        ("apps/payments/resources.yaml", 1),
    ]

    deployment = result.resources[0].manifest
    assert deployment["metadata"]["annotations"] == {"example.com/note": "stable"}
    assert deployment["metadata"]["labels"] == {
        "app.kubernetes.io/name": "api",
        "team": "platform",
    }
    assert deployment["spec"]["template"]["spec"]["containers"][0]["command"][2] == ("echo ready\nexec app\n")
    assert result.warnings == (
        "apps/payments/resources.yaml document index 0: ignored volatile metadata key "
        "'deployment.kubernetes.io/revision'",
    )
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", result.output_digest)


def test_kind_list_is_flattened_and_each_item_maps_to_the_wrapper_document():
    result = render_raw({"clusters/payments/list.json": _fixture("kind-list.json")})

    assert [resource.target.name for resource in result.resources] == ["settings", "api"]
    assert [resource.target.kind for resource in result.resources] == ["ConfigMap", "Service"]
    assert [(resource.source_path, resource.document_index) for resource in result.resources] == [
        ("clusters/payments/list.json", 0),
        ("clusters/payments/list.json", 0),
    ]
    assert all(resource.manifest["kind"] != "List" for resource in result.resources)
    assert result.warnings == ()


def test_json_and_yaml_resource_order_does_not_change_the_output_digest():
    config_map = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: settings}\ndata: {b: second, a: first}\n"
    service = "apiVersion: v1\nkind: Service\nmetadata: {name: api}\nspec: {ports: [{port: 8080}]}\n"

    forward = render_raw({"b/service.yaml": service, "a/config.yaml": config_map})
    reverse = render_raw({"a/config.yaml": config_map, "b/service.yaml": service})

    assert forward.output_digest == reverse.output_digest
    assert [resource.source_path for resource in forward.resources] == [
        "a/config.yaml",
        "b/service.yaml",
    ]
    assert list(forward.resources[0].manifest["data"]) == ["a", "b"]


def test_missing_namespace_is_not_inferred():
    result = render_raw({"resource.yaml": ("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: settings\n")})

    assert result.resources[0].target.namespace == ""


def test_unquoted_yaml_timestamps_are_normalized_as_iso_strings():
    result = render_raw(
        {
            "resource.yaml": (
                "apiVersion: v1\n"
                "kind: ConfigMap\n"
                "metadata:\n"
                "  name: dated-settings\n"
                "data:\n"
                "  date: 2026-10-23\n"
                "  time: 2026-10-23T02:34:00Z\n"
            )
        }
    )

    assert result.resources[0].manifest["data"] == {
        "date": "2026-10-23",
        "time": "2026-10-23T02:34:00+00:00",
    }


def test_duplicate_resource_identity_is_rejected_with_both_origins():
    with pytest.raises(RawRenderError, match="duplicate resource identity") as exc_info:
        render_raw({"apps/duplicate.yaml": _fixture("duplicate-identities.yaml")})

    assert "document index 0" in str(exc_info.value)
    assert "document index 1" in str(exc_info.value)


def test_resource_without_a_complete_identity_is_rejected():
    with pytest.raises(RawRenderError, match="metadata.name"):
        render_raw({"apps/malformed.yaml": _fixture("malformed-identity.yaml")})


@pytest.mark.parametrize(
    ("source_path", "content", "error"),
    [
        ("duplicate.yaml", "duplicate-yaml-keys.yaml", "invalid YAML"),
        ("duplicate.json", "duplicate-json-keys.json", "duplicate JSON object key"),
    ],
)
def test_duplicate_mapping_keys_are_rejected(source_path, content, error):
    with pytest.raises(RawRenderError, match=error):
        render_raw({source_path: _fixture(content)})


def test_unsafe_yaml_constructor_is_rejected_without_execution(capsys):
    with pytest.raises(RawRenderError, match="invalid YAML"):
        render_raw({"unsafe.yaml": _fixture("unsafe-tag.yaml")})

    assert capsys.readouterr().out == ""


def test_recursive_yaml_alias_is_rejected():
    with pytest.raises(RawRenderError, match="recursive YAML alias"):
        render_raw({"recursive.yaml": _fixture("recursive-alias.yaml")})


@pytest.mark.parametrize("source_path", ["/absolute.yaml", "../outside.yaml", "apps/../secret.yaml", r"apps\api.yaml"])
def test_source_paths_must_be_relative_unambiguous_posix_paths(source_path):
    with pytest.raises(RawRenderError, match="source path"):
        render_raw({source_path: "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: ok}\n"})


def test_input_size_is_bounded_before_parsing():
    limits = RenderLimits(max_total_bytes=32, max_file_bytes=16)

    with pytest.raises(RawRenderLimitError, match="file byte limit"):
        RawRenderer(limits=limits).render({"large.yaml": "x" * 17})


def test_total_input_size_is_bounded_across_sources():
    limits = RenderLimits(max_total_bytes=16, max_file_bytes=16)

    with pytest.raises(RawRenderLimitError, match="total input byte limit"):
        RawRenderer(limits=limits).render({"one.yaml": "x" * 9, "two.yaml": "y" * 8})


def test_document_count_is_bounded():
    limits = RenderLimits(max_total_bytes=1024, max_file_bytes=1024, max_documents=1)

    with pytest.raises(RawRenderLimitError, match="document limit"):
        RawRenderer(limits=limits).render({"multi.yaml": _fixture("multi-document.yaml")})


def test_node_count_and_nesting_depth_are_bounded():
    resource = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: ok}\n"
    deep_resource = resource + "data:\n  outer:\n    inner: value\n"
    node_limited = RenderLimits(max_total_bytes=1024, max_file_bytes=1024, max_nodes=3)
    depth_limited = RenderLimits(max_total_bytes=1024, max_file_bytes=1024, max_depth=2)

    with pytest.raises(RawRenderLimitError, match="node limit"):
        RawRenderer(limits=node_limited).render({"resource.yaml": resource})

    with pytest.raises(RawRenderLimitError, match="nesting depth limit"):
        RawRenderer(limits=depth_limited).render({"resource.yaml": deep_resource})


def test_empty_yaml_documents_are_skipped_with_warnings():
    result = render_raw({"empty.yaml": "# intentionally empty\n---\n---\n"})

    assert result.resources == ()
    assert result.warnings == (
        "empty.yaml document index 0 is empty and was skipped",
        "empty.yaml document index 1 is empty and was skipped",
    )
