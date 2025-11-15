# src/greenkube/core/telemetry.py
"""Initializes OpenTelemetry services for the GreenKube application."""

import logging
import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

# Environment variable for the OTel collector endpoint
# Default: http://localhost:4318. In k8s, this would be http://otel-collector.default.svc.cluster.local:4318
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")


def initialize_telemetry():
    """
    Configures and initializes the TracerProvider and MeterProvider for OpenTelemetry.
    Data will be exported via OTLP/HTTP.
    """
    resource = Resource(attributes={SERVICE_NAME: "greenkube-app"})

    # --- Tracing Configuration ---
    tracer_provider = TracerProvider(resource=resource)
    span_exporter = OTLPSpanExporter(endpoint=f"{OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces")
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    # --- Metrics Configuration ---
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{OTEL_EXPORTER_OTLP_ENDPOINT}/v1/metrics")
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)
    logger.info(f"OpenTelemetry initialized. Exporting to: {OTEL_EXPORTER_OTLP_ENDPOINT}")


# Make the tracer and meter globally accessible
tracer = trace.get_tracer("greenkube.tracer")
meter = metrics.get_meter("greenkube.meter")
