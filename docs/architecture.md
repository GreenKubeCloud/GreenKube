# GreenKube Architecture (Community Edition)

This document describes the technical architecture of the GreenKube open-source
version. The goal is to create a lightweight, modular, and extensible tool to
estimate pod-level carbon emissions using Prometheus metrics and grid intensity
data.

## Overview

GreenKube operates as an agent that collects, processes, and reports data. It
is typically launched on-demand via the CLI and is organized around small,
focused components that are easy to test and replace.

The system has two primary flows: energy estimation (from Prometheus) and
optional cost annotation (from OpenCost or other sources). The carbon
calculation sits at the intersection of those flows and external intensity
data sources (e.g., Electricity Maps).

## Core Components

### Collectors
- PrometheusCollector
    - Queries Prometheus for CPU usage series and node labels.
    - Robust to external Prometheus installations: it tries multiple API
        endpoint forms, supports TLS/auth settings, and falls back to a PromQL
        expression that doesn't require a `container` label when needed.
    - Emits a PrometheusMetric consumed by the estimator.

- NodeCollector
    - Reads node metadata (zones, labels, instance-type labels) using the
        Kubernetes API and is used as a fallback to populate instance-type
        information when Prometheus doesn't provide it.

- PodCollector
    - Gathers pod resource request metrics from the K8s API and aggregates
        container-level requests to the pod-level.

- OpenCostCollector (optional)
    - Collects cost metrics to annotate results with estimated cost.

### Estimator
- BasicEstimator
    - Converts PrometheusMetric (CPU usage rates) into EnergyMetric objects
        (joules per pod). When instance type information is missing, a default
        instance profile is used so the pipeline continues safely.

### Processor
- DataProcessor
    - Orchestrates collection, estimation, prefetching of intensities, and the
        final combination of metrics.
    - Groups energy metrics by Electricity Maps zone and prefetches a single
        intensity per zone per run to reduce external calls.
    - Populates the calculator's per-run intensity cache to make repeated
        lookups cheap.

### Calculator
- CarbonCalculator
    - Converts Joules → kWh → CO2e using the grid intensity and PUE.
    - Maintains a per-run in-memory cache of (zone, timestamp) → intensity to
        avoid repeated repository queries.

### Repositories
- Implementations (e.g., SQLiteRepository, ElasticsearchRepository) provide
    get_for_zone_at_time(zone, timestamp) to retrieve grid intensity (gCO2e/kWh)
    for a normalized timestamp.

## Data Flow
1. PrometheusCollector scrapes CPU series and returns PrometheusMetric.
2. BasicEstimator estimates EnergyMetric(s) from Prometheus data.
3. DataProcessor groups energy metrics by zone and selects a representative
     timestamp for the zone (latest among metrics) to prefetch intensity.
4. DataProcessor normalizes timestamps according to NORMALIZATION_GRANULARITY
     and prefetches one intensity per zone per run, populating the
     CarbonCalculator._intensity_cache for all metric timestamps in that zone.
5. CarbonCalculator.calculate_emissions uses cached intensities when
     available, otherwise calls the repository.
6. DataProcessor combines CO2e, cost, and request data into a CombinedMetric
     per pod and returns the list of combined metrics.

## Normalization and caching
- NORMALIZATION_GRANULARITY (env-configurable) controls timestamp bucketing:
    - 'hour' (default): round down to the start of the hour.
    - 'day': round down to midnight UTC of the day.
    - 'none': use the original timestamp.
- The processor stores both 'Z' and '+00:00' ISO timestamp string variants in
    the calculator cache to be tolerant of callers/tests that use either form.
    Repository calls use the '+00:00' form for compatibility with existing
    backends/tests.

## Goals and design choices
- Replace Kepler dependency with Prometheus-based estimation.
- Be resilient to diverse Prometheus deployments and provide clear
    diagnostics for malformed series.
- Reduce external Electricity Maps lookups by grouping metrics and
    prefetching once per zone per run.
- Use conservative defaults (DEFAULT instance profile) when instance-type is
    unknown so the pipeline remains operational.

## Notes for contributors
- Tests are TDD-first: add tests before implementing behavior.
- When adding a new collector or repository, implement the corresponding
    unit tests and an integration-like test that exercises the component in a
    mocked environment.