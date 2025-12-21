# GreenKube Architecture

This document describes the technical architecture of the GreenKube open-source
version. The goal is to create a lightweight, modular, and extensible tool to
estimate pod-level carbon emissions using Prometheus metrics and grid intensity
data.

## Overview

GreenKube operates as an **asynchronous** agent that collects, processes, and reports data. It
is typically launched on-demand via the CLI or run as a scheduled service. It is designed
around small, focused components that are easy to test and replace, utilizing Python's `asyncio`
for high-performance, non-blocking input/output operations.

The system has two primary flows: energy estimation (from Prometheus) and
optional cost annotation (from OpenCost or other sources). The carbon
calculation sits at the intersection of those flows and external intensity
data sources (e.g., Electricity Maps).

## Core Components

### Collectors
All collectors are fully asynchronous, allowing for concurrent data fetching.

- **PrometheusCollector**
    - Asynchronously queries Prometheus for CPU usage series and node labels.
    - Robust to external Prometheus installations: tries multiple API endpoints, supports TLS/auth,
      and uses `httpx` for non-blocking requests.
    - Emits a `PrometheusMetric` consumed by the estimator.

- **NodeCollector**
    - Asynchronously reads node metadata (zones, labels, instance-type labels) using the
      `kubernetes_asyncio` client.
    - Collects comprehensive node information (CPU capacity, architecture, provider)
      to build accurate instance profiles without blocking the main event loop.

- **PodCollector**
    - Gathers pod resource request metrics from the K8s API asynchronously.

- **OpenCostCollector** (optional)
    - Asynchronously collects cost metrics to annotate results with estimated cost.

- **ElectricityMapsCollector**
    - Asynchronously fetches carbon intensity data from the Electricity Maps API.

### Estimator
- **BasicEstimator**
    - Converts `PrometheusMetric` (CPU usage rates) into `EnergyMetric` objects
        (joules per pod). When instance type information is missing, a default
        instance profile is used so the pipeline continues safely.

### Processor
- **DataProcessor**
    - Orchestrates the entire pipeline using `asyncio.gather` for parallel execution.
    - Concurrently fetches data from all collectors (Prometheus, K8s, OpenCost).
    - Reconstructs historical node states using `NodeRepository` backfilling.
    - Aggregates `is_estimated` flags and `estimation_reasons` into the `CombinedMetric`.
    - Groups energy metrics by Electricity Maps zone and asynchronously prefetches intensity data.
    - Populates the calculator's per-run intensity cache for efficient lookups.

### Calculator
- **CarbonCalculator**
    - Converts Joules → kWh → CO2e using the grid intensity and PUE.
    - Maintains a per-run in-memory cache of (zone, timestamp) → intensity.

### Repositories
Repositories use asynchronous drivers for high-performance database interactions.

- **CarbonIntensityRepository**: Abstract base class.
    - **PostgresCarbonIntensityRepository** (Default): Uses `asyncpg` for robust, async PostgreSQL interactions.
    - **SQLiteCarbonIntensityRepository**: Uses `aiosqlite` for asynchronous local storage.
    - **ElasticsearchCarbonIntensityRepository**: Uses asynchronous Elasticsearch client.
- **NodeRepository**: Abstract base class for node snapshots.
    - **PostgresNodeRepository**: Async PostgreSQL implementation.
    - **SQLiteNodeRepository**: Async SQLite implementation.

## Data Flow
1. **Parallel Collection**: `DataProcessor` triggers `PrometheusCollector`, `NodeCollector`, `PodCollector`, and `OpenCostCollector` concurrently.
2. **Node State**: `DataProcessor` retrieves historical node state from `NodeRepository` (awaiting DB results).
3. **Estimation**: `BasicEstimator` processes memory-resident data to produce `EnergyMetric` objects.
4. **Intensity Prefetch**: `DataProcessor` groups metrics by zone and asynchronously prefetches carbon intensity data from the repository or external API.
5. **Calculation**: `CarbonCalculator` computes emissions using cached data.
6. **Aggregation**: Results are combined into `CombinedMetric` objects.
7. **Persistence**: `CombinedMetric`s are asynchronously written to the database.

## Normalization and caching
- NORMALIZATION_GRANULARITY (env-configurable) controls timestamp bucketing:
    - 'hour' (default): round down to the start of the hour.
    - 'day': round down to midnight UTC of the day.
    - 'none': use the original timestamp.
- The processor stores both 'Z' and '+00:00' ISO timestamp string variants in
    the calculator cache to be tolerant of callers/tests that use either form.

## Goals and design choices
- **Asynchronous & Non-Blocking**: Fully leverage `asyncio` to handle I/O bound tasks (API calls, DB queries) efficiently.
- **Resilience**: Robust handling of missing data, API failures, and diverse deployment environments.
- **Database Agnosticism**: Core logic is independent of storage backend, supporting Postgres, SQLite, and Elasticsearch.
- **Transparency**: Clear flagging of estimated values versus measured data.

## Notes for contributors
- **Async First**: New I/O operations must be asynchronous. Use `await` and `async def`.
- **Testing**: Use `pytest-asyncio` for testing async components. properly mock async calls using `AsyncMock`.