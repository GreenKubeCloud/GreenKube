# GreenKube Architecture (Community Edition)

This document describes the technical architecture of the GreenKube open-source version. The goal is to create a lightweight, modular, and extensible tool.

## Overview

GreenKube operates as an agent that collects, processes, and reports data. It does not run continuously in the cluster (in its initial version) but is launched on-demand by the user via a Command-Line Interface (CLI).

The architecture can be divided into two main data flows:

1.  **Energy Flow**: Measures the energy consumption of workloads.
2.  **Cost Flow**: Measures the costs of resources (will be further developed in future versions).

The carbon footprint calculation is at the intersection of these flows and external data.

## Core Components

The source code is organized into distinct modules, each with a clear responsibility.

### 1. `collectors/` - The Data Acquisition Layer

This module is responsible for collecting raw data from various sources. Each collector is a specialized class.

* **`KeplerCollector`**:
    * **Role**: To query the Prometheus endpoint exposed by **Kepler**.
    * **Data Collected**: Energy consumption in Joules for each pod, container, and namespace (`kepler_container_joules_total`).
    * **Technology**: HTTP `GET` calls to the Prometheus API.

* **`GridIntensityCollector`**:
    * **Role**: To retrieve the carbon intensity of the electrical grid.
    * **Data Collected**: The amount of gCO2e per kWh (grams of CO2 equivalent per kilowatt-hour).
    * **Technology**: HTTP `GET` calls to public third-party APIs (e.g., Electricity Maps, WattTime). The collector must determine the cloud region to make the correct API call.

* **`CloudProviderCollector`**:
    * **Role**: To obtain metadata specific to the cloud provider.
    * **Data Collected**: The Power Usage Effectiveness (PUE) of the data center, the region, the instance type.
    * **Technology**: Calls to cloud provider APIs (AWS, GCP, Azure) or the use of well-documented default values.

### 2. `models/` - The Data Structure

This module uses Pydantic to define clear and typed data models. This ensures data consistency across the application.

* `EnergyMetric`: Represents an energy measurement for a pod.
* `CarbonIntensity`: Represents the carbon intensity for a given region.
* `CarbonFootprintResult`: The final model that combines all information for a given workload.

### 3. `core/` - The Application Brain

This is where the main business logic is implemented.

* **`Calculator`**:
    * **Role**: Contains the logic for converting energy into CO2e emissions.
    * **Key Formula**: `CO2e_Emission = (Energy_kWh * PUE) * Carbon_Intensity`
    * **Inputs**: Data models from the collectors.
    * **Output**: A list of `CarbonFootprintResult` objects.

* **`Processor`**:
    * **Role**: To orchestrate the workflow.
    * **Logic**: Calls the collectors, passes the data to the calculator, and sends the results to the reporters.

### 4. `reporters/` - The Presentation Layer

This module is responsible for formatting the results for the end-user.

* **`ConsoleReporter`**: Uses the `rich` library to display a summarized and colorful table in the terminal.
* **`CsrdReporter`**: Formats the data to generate a file (CSV, JSON) containing the information required for a basic sustainability report.

## Scalability

This modular architecture allows for easy evolution:

* **Add a data source**: Simply create a new collector.
* **Change the calculation logic**: Only the `Calculator` needs to be modified.
* **Add a report format**: Simply create a new reporter.