"""Structural check that a later revision still carries a plan's file changes (D1)."""

import pytest

from greenkube.gitops.planned_values import planned_values_present

ORIGINAL = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: payments
spec:
  template:
    spec:
      containers:
        - name: api
          image: registry.example/api:1.0.0
          resources:
            requests:
              cpu: 500m
              memory: 512Mi
            limits:
              cpu: "2"
        - name: sidecar
          image: registry.example/proxy:3.1
"""

PLANNED = ORIGINAL.replace("cpu: 500m", "cpu: 300m").replace('            limits:\n              cpu: "2"\n', "")


def test_observed_planned_content_is_present():
    assert planned_values_present(ORIGINAL, PLANNED, PLANNED) is True


def test_reverted_content_is_not_present():
    assert planned_values_present(ORIGINAL, PLANNED, ORIGINAL) is False


def test_unrelated_edits_in_the_same_file_keep_the_planned_values():
    """Image write-back and other later commits often edit the same manifest."""
    observed = (
        PLANNED.replace("api:1.0.0", "api:1.1.0")
        .replace("proxy:3.1", "proxy:3.2")
        .replace("  namespace: payments\n", "  namespace: payments\n  labels:\n    team: core\n")
    )

    assert planned_values_present(ORIGINAL, PLANNED, observed) is True


def test_overwritten_planned_value_is_not_present():
    assert planned_values_present(ORIGINAL, PLANNED, PLANNED.replace("cpu: 300m", "cpu: 400m")) is False


def test_restored_removed_value_is_not_present():
    observed = PLANNED.replace("memory: 512Mi\n", 'memory: 512Mi\n            limits:\n              cpu: "2"\n')

    assert planned_values_present(ORIGINAL, PLANNED, observed) is False


def test_named_list_items_and_documents_match_by_identity_not_position():
    reordered = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: added-later
  namespace: payments
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: payments
spec:
  template:
    spec:
      containers:
        - name: sidecar
          image: registry.example/proxy:3.1
        - name: api
          image: registry.example/api:1.0.0
          resources:
            requests:
              memory: 512Mi
              cpu: 300m
"""

    assert planned_values_present(ORIGINAL, PLANNED, reordered) is True


def test_equivalent_scalar_spelling_is_accepted():
    original = "resources:\n  requests:\n    cpu: 2\n"
    planned = "resources:\n  requests:\n    cpu: 1\n"

    assert planned_values_present(original, planned, 'resources:\n  requests:\n    cpu: "1"\n') is True


def test_created_file_must_keep_its_planned_values():
    created = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\n"
        "spec:\n  template:\n    spec:\n      containers:\n        - name: api\n"
        "          resources:\n            requests:\n              cpu: 300m\n"
    )
    extended = created + "              memory: 256Mi\n"

    assert planned_values_present(None, created, extended) is True
    assert planned_values_present(None, created, created.replace("300m", "250m")) is False
    assert planned_values_present(None, created, "") is False


def test_json_content_is_compared_structurally():
    original = '{"spec": {"replicas": 3, "cpu": "500m"}}'
    planned = '{"spec": {"replicas": 3, "cpu": "300m"}}'

    assert planned_values_present(original, planned, '{"spec": {"cpu": "300m", "replicas": 5}}') is True
    assert planned_values_present(original, planned, original) is False


@pytest.mark.parametrize(
    "observed",
    [
        "k: 2\na: [unclosed\n",
        "k: 2\nk: 2\n",
        # Alias expansion must not be flattened without bound.
        "k: 2\n"
        "a: &a [x, x, x, x, x, x, x, x, x, x]\n"
        "b: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a, *a]\n"
        "c: &c [*b, *b, *b, *b, *b, *b, *b, *b, *b, *b]\n"
        "d: &d [*c, *c, *c, *c, *c, *c, *c, *c, *c, *c]\n"
        "e: &e [*d, *d, *d, *d, *d, *d, *d, *d, *d, *d]\n",
        "k: 2\nz: " + "[" * 100 + "]" * 100 + "\n",
    ],
)
def test_unparseable_or_unbounded_content_never_counts_as_present(observed):
    assert planned_values_present("k: 1\n", "k: 2\n", "k: 2\n") is True
    assert planned_values_present("k: 1\n", "k: 2\n", observed) is False
