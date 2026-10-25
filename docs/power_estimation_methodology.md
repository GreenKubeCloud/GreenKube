# Power Estimation Methodology for OVH and Scaleway

Since Cloud Carbon Footprint (CCF) does not provide default estimation constants for other cloud providers than AWS, GCP and Azure, we have derived provider-level estimates using the CCF methodology for "unknown micro-architectures".

## CPU-to-energy estimate

GreenKube estimates node power from observed CPU use and an instance power profile. For a collection step:

```text
node_utilization = min(total_node_cpu_cores / instance_vcores, 1)
node_power_watts = min_watts + node_utilization × (max_watts - min_watts)
node_energy_joules = node_power_watts × step_duration_seconds
```

For nodes with CPU usage, the estimated node energy is allocated to pods in proportion to their observed CPU use. If CPU use is zero but pods are present, the minimum (idle) node power is divided evenly between them. If the node has no pods, that idle energy is recorded as `System/Unallocated`.

Known instance profiles use the provider's per-vCPU constants multiplied by the instance vCPU count. The currently loaded provider constants are:

| Provider | Min Watts/vCPU | Max Watts/vCPU |
|---|---:|---:|
| AWS | 0.74 | 3.50 |
| GCP | 0.71 | 4.26 |
| Azure | 0.78 | 3.76 |
| OVH | 0.80 | 3.52 |
| Scaleway | 0.60 | 2.78 |

Instance types absent from the bundled profile data use `config.defaults.instance` (default: 1 vCPU, 1–10 W); inferred `cpu-N` node profiles scale those configured per-vCPU defaults by `N`.

For operational emissions, GreenKube converts joules to kWh (`joules / 3,600,000`) and multiplies by grid intensity and PUE. These are model-based estimates, not direct power-meter readings.

## Methodology

The CCF methodology states:
> "When we don’t know the underlying processor micro-architecture, we use the average or median of all micro-architectures used by that cloud provider."

We applied this approach by:
1.  Identifying the CPU micro-architectures commonly used by OVH and Scaleway based on their public documentation and hardware specifications.
2.  Retrieving the specific Min/Max Watts per vCPU for these micro-architectures from the CCF dataset (derived from SPECpower benchmarks).
3.  Calculating the average Min and Max Watts across the identified micro-architectures for each provider.

## Data Sources

- **Hardware Info:** Public documentation from OVHcloud and Scaleway.
- **Power Constants:** Cloud Carbon Footprint Coefficients (`ccf-coefficients` repository).

## Calculations

### OVHcloud
**Identified Architectures:** Intel Haswell, Broadwell, Skylake, Cascade Lake; AMD EPYC Milan, Genoa.

| Micro-architecture | Min Watts | Max Watts |
|--------------------|-----------|-----------|
| Haswell            | 1.90      | 5.99      |
| Broadwell          | 0.71      | 3.54      |
| Skylake            | 0.64      | 4.05      |
| Cascade Lake       | 0.64      | 3.80      |
| AMD EPYC Milan     | 0.45      | 1.87      |
| AMD EPYC Genoa*    | 0.45      | 1.87      |

*\*Genoa values estimated using Milan as a proxy due to lack of specific data.*

**Average for OVH:**
- **Min Watts:** 0.80
- **Max Watts:** 3.52

### Scaleway
**Identified Architectures:** AMD EPYC 1st Gen (Naples), 2nd Gen (Rome), 3rd Gen (Milan); Intel Xeon Gold (Skylake/Cascade Lake).

| Micro-architecture | Min Watts | Max Watts |
|--------------------|-----------|-----------|
| AMD EPYC 1st Gen   | 0.82      | 2.55      |
| AMD EPYC 2nd Gen   | 0.47      | 1.64      |
| AMD EPYC 3rd Gen   | 0.45      | 1.87      |
| Intel Skylake      | 0.64      | 4.05      |
| Intel Cascade Lake | 0.64      | 3.80      |

**Average for Scaleway:**
- **Min Watts:** 0.60
- **Max Watts:** 2.78

## Usage
The OVH and Scaleway values are stored in `provider_power_estimates.csv` and are used with the bundled instance profiles for known instance types. An unknown instance type uses the configured generic profile described above.

## GHG Protocol Scope Classification

GreenKube classifies carbon emissions according to the [GHG Protocol Corporate Accounting and Reporting Standard](https://ghgprotocol.org/corporate-standard):

| GHG Scope | GreenKube field | Description |
|---|---|---|
| **Scope 2** (location-based estimate) | `co2e_grams` | Indirect emissions from purchased electricity — computed as `grid_intensity × energy_kWh × PUE`. Grid intensity is sourced from Electricity Maps (default) or optional Wattnet, with the configurable `DEFAULT_INTENSITY` fallback. |
| **Scope 3, Category 1** (purchased goods & services) | `embodied_co2e_grams` | Upstream hardware manufacturing emissions allocated to the pod by CPU share — sourced from the [Boavizta API](https://api.boavizta.org) and amortised over the hardware lifespan. Falls back to `DEFAULT_EMBODIED_EMISSIONS_KG` (default: **100 kg CO₂e**) when Boavizta does not recognise the provider or instance type. The Boavizta `/v1/cloud/instance` endpoint returns per-instance allocated GWP (typically 50–170 kg for common cloud VMs such as `aws/m5.large`); the fallback is calibrated to the same scale to avoid over-estimating Scope 3. |
| **Scope 2 + Scope 3** | `total_co2e_grams` | Full pod carbon footprint — computed field on `CombinedMetric`, also exposed as `total_co2e_all_scopes` in API summary and timeseries responses. |

**Scope 1** (direct combustion) is not applicable for cloud/virtualised Kubernetes workloads and is therefore not tracked.

For annual analysis, `total_co2e_all_scopes` combines the estimated Scope 2 and Scope 3 figures. The data export (`GET /api/v1/report/export`) includes `co2e_grams` and `embodied_co2e_grams` separately in each row so their underlying estimates can be examined independently.
