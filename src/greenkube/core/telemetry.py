# src/greenkube/core/telemetry.py
"""Initialise les services OpenTelemetry pour l'application GreenKube."""

import os
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.resources import Resource, SERVICE_NAME

# Variable d'environnement pour l'endpoint du collecteur OTel
# Par défaut : http://localhost:4318. En k8s, ce sera http://otel-collector.default.svc.cluster.local:4318
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

def initialize_telemetry():
    """
    Configure et initialise le TracerProvider et le MeterProvider pour OpenTelemetry.
    Les données seront exportées via OTLP/HTTP.
    """
    resource = Resource(attributes={
        SERVICE_NAME: "greenkube-app"
    })

    # --- Configuration du Tracing ---
    tracer_provider = TracerProvider(resource=resource)
    span_exporter = OTLPSpanExporter(endpoint=f"{OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces")
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    # --- Configuration des Métriques ---
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{OTEL_EXPORTER_OTLP_ENDPOINT}/v1/metrics")
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)
    print(f"OpenTelemetry initialisé. Export vers : {OTEL_EXPORTER_OTLP_ENDPOINT}")

# Rendre le tracer et le meter accessibles globalement
tracer = trace.get_tracer("greenkube.tracer")
meter = metrics.get_meter("greenkube.meter")

