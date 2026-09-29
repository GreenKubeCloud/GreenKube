# tests/automation/test_source_resolver.py
"""Tests for annotation-based Git source discovery."""

import pytest

from greenkube.automation.source_resolver import (
    ANNOTATION_BRANCH,
    ANNOTATION_PATH,
    ANNOTATION_REPO,
    AnnotationSourceResolver,
    K8sAnnotationReader,
    SourceResolutionError,
)
from greenkube.models.metrics import RecommendationRecord, RecommendationType


def _record(**overrides) -> RecommendationRecord:
    defaults = dict(
        pod_name="payments-api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="CPU oversized",
        scope="workload",
        owner_kind="Deployment",
        owner_name="payments-api",
    )
    defaults.update(overrides)
    return RecommendationRecord(**defaults)


class FakeReader:
    def __init__(self, annotations):
        self.annotations = annotations
        self.calls = []

    async def read_annotations(self, namespace, kind, name):
        self.calls.append((namespace, kind, name))
        if isinstance(self.annotations, Exception):
            raise self.annotations
        return self.annotations


class TestAnnotationSourceResolver:
    @pytest.mark.asyncio
    async def test_resolves_repo_path_and_branch(self):
        reader = FakeReader(
            {
                ANNOTATION_REPO: "https://github.com/acme/manifests.git",
                ANNOTATION_PATH: "apps/payments.yaml",
                ANNOTATION_BRANCH: "main",
            }
        )
        source = await AnnotationSourceResolver(reader).resolve(_record())

        assert source.repo_url == "https://github.com/acme/manifests.git"
        assert source.path == "apps/payments.yaml"
        assert source.branch == "main"
        assert reader.calls == [("prod", "Deployment", "payments-api")]

    @pytest.mark.asyncio
    async def test_optional_annotations_absent(self):
        reader = FakeReader({ANNOTATION_REPO: "git@github.com:acme/manifests.git"})
        source = await AnnotationSourceResolver(reader).resolve(_record())
        assert source.path is None
        assert source.branch is None

    @pytest.mark.asyncio
    async def test_missing_repo_annotation_is_actionable(self):
        reader = FakeReader({})
        with pytest.raises(SourceResolutionError) as exc:
            await AnnotationSourceResolver(reader).resolve(_record())
        assert ANNOTATION_REPO in str(exc.value)

    @pytest.mark.asyncio
    async def test_recommendation_without_owner_is_rejected(self):
        with pytest.raises(SourceResolutionError):
            await AnnotationSourceResolver(FakeReader({})).resolve(_record(owner_kind=None, owner_name=None))

    @pytest.mark.asyncio
    async def test_pod_scoped_recommendation_is_rejected_even_with_owner_fields(self):
        with pytest.raises(SourceResolutionError, match="only workload-scoped"):
            await AnnotationSourceResolver(FakeReader({})).resolve(
                _record(scope="pod", owner_kind="Deployment", owner_name="payments-api")
            )


class TestK8sAnnotationReader:
    @pytest.mark.asyncio
    async def test_unsupported_kind_is_rejected(self):
        with pytest.raises(SourceResolutionError):
            await K8sAnnotationReader().read_annotations("prod", "Pod", "api")
