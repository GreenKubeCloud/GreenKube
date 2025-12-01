# Power Estimation Methodology for OVH and Scaleway

Since Cloud Carbon Footprint (CCF) does not provide default estimation constants for other cloud providers than AWS, GCP and Azure, we have derived provider-level estimates using the CCF methodology for "unknown micro-architectures".

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
These values are stored in `provider_power_estimates.csv` and are used to estimate energy consumption for any instance belonging to these providers, scaled by the number of vCPUs.
