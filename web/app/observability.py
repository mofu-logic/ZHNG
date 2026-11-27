"""Structured operation logging, aligned with OpenTelemetry.

Design:
- All operation events go through the OpenTelemetry Logs API/SDK
  (LoggerProvider), so an OTLP exporter can be attached later without
  touching call sites.
- Two exporters are attached now:
  * stdout: one JSON object per line, following the OTel log data
    model (timestamp / severity_text / severity_number / body /
    attributes / trace_id / span_id / resource). A log collector can
    pick these up from the container logs for external integration.
  * database: the same records are stored in the `app_logs` table so
    the app's own /logs page can show them.
- The stdlib logger used across the app is bridged into the same
  pipeline, so existing logger.info/warning/exception calls become
  structured records too (exceptions carry type/message/stacktrace
  attributes).
- A TracerProvider is installed and the app creates one span per
  HTTP request, which is what puts trace_id/span_id into the log
  records. Spans are not exported yet.
"""
import json
import logging
import sys
from datetime import datetime, timezone

import psycopg
from opentelemetry import trace
from opentelemetry._logs import LogRecord, SeverityNumber, get_logger, set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    LogRecordExporter,
    LogRecordExportResult,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from psycopg.types.json import Jsonb

SERVICE_NAME = "stock-app"

_SEVERITIES = {
    "DEBUG": ("DEBUG", SeverityNumber.DEBUG),
    "INFO": ("INFO", SeverityNumber.INFO),
    "WARNING": ("WARN", SeverityNumber.WARN),
    "WARN": ("WARN", SeverityNumber.WARN),
    "ERROR": ("ERROR", SeverityNumber.ERROR),
}


def _span_ids(lr):
    # Current SDKs populate trace_id/span_id on the record itself (from
    # the span current at emit time); older ones only carry the context.
    trace_id = getattr(lr, "trace_id", None)
    span_id = getattr(lr, "span_id", None)
    if trace_id:
        return format(trace_id, "032x"), format(span_id or 0, "016x")
    context = getattr(lr, "context", None)
    if context is None:
        return "", ""
    span_context = trace.get_current_span(context).get_span_context()
    if not span_context.is_valid:
        return "", ""
    return format(span_context.trace_id, "032x"), format(span_context.span_id, "016x")


def format_record(record):
    """One ReadableLogRecord -> a plain dict in the OTel log data model
    shape (also the JSON written to stdout)."""
    lr = record.log_record
    trace_id, span_id = _span_ids(lr)
    ts = lr.timestamp or lr.observed_timestamp
    severity_number = getattr(lr.severity_number, "value", lr.severity_number)
    return {
        "timestamp": ts.isoformat() if ts else None,
        "severity_text": lr.severity_text,
        "severity_number": severity_number,
        "body": "" if lr.body is None else str(lr.body),
        "attributes": dict(lr.attributes or {}),
        "trace_id": trace_id,
        "span_id": span_id,
        "resource": {"service.name": SERVICE_NAME},
    }


class JsonStdoutExporter(LogRecordExporter):
    def export(self, batch):
        try:
            for record in batch:
                print(
                    json.dumps(format_record(record), ensure_ascii=False, default=str),
                    flush=True,
                )
        except Exception:
            return LogRecordExportResult.FAILURE
        return LogRecordExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


class DbLogExporter(LogRecordExporter):
    """Persist records to the app_logs table. Failures are reported on
    stderr only: this exporter must never raise into the logging path
    (that would recurse) and never take the app down."""

    def __init__(self, database_url):
        self._database_url = database_url

    def export(self, batch):
        try:
            rows = []
            for record in batch:
                d = format_record(record)
                lr = record.log_record
                rows.append(
                    (
                        lr.timestamp or datetime.now(timezone.utc),
                        d["severity_text"],
                        d["severity_number"],
                        d["body"],
                        Jsonb(d["attributes"]),
                        d["trace_id"] or None,
                        d["span_id"] or None,
                    )
                )
            with psycopg.connect(self._database_url) as conn:
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO app_logs(ts, severity, severity_number,"
                        " body, attributes, trace_id, span_id)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        rows,
                    )
                conn.commit()
        except Exception as e:
            print(f"app_logs export failed: {e}", file=sys.stderr, flush=True)
            return LogRecordExportResult.FAILURE
        return LogRecordExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


class OtelBridgeHandler(logging.Handler):
    """Forward stdlib log records (incl. exceptions) into the OTel
    logs pipeline."""

    def emit(self, record):
        try:
            if record.levelno >= logging.ERROR:
                severity = ("ERROR", SeverityNumber.ERROR)
            elif record.levelno >= logging.WARNING:
                severity = ("WARN", SeverityNumber.WARN)
            elif record.levelno >= logging.INFO:
                severity = ("INFO", SeverityNumber.INFO)
            else:
                severity = ("DEBUG", SeverityNumber.DEBUG)
            attributes = {"logger.name": record.name}
            if record.exc_info and record.exc_info[0] is not None:
                attributes["exception.type"] = record.exc_info[0].__name__
                attributes["exception.message"] = str(record.exc_info[1])
                attributes["exception.stacktrace"] = self.format(record)
            get_logger("stock-app.bridge").emit(
                LogRecord(
                    timestamp=datetime.fromtimestamp(
                        record.created, tz=timezone.utc
                    ),
                    severity_text=severity[0],
                    severity_number=severity[1],
                    body=record.getMessage(),
                    attributes=attributes,
                )
            )
        except Exception:
            pass  # logging must never break the app


_provider = None
_otlp_processor = None


def setup_observability(database_url):
    """Install the LoggerProvider (stdout JSON + DB exporters) and a
    TracerProvider. Returns the tracer used for request spans."""
    global _provider
    resource = Resource.create({"service.name": SERVICE_NAME})
    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(SimpleLogRecordProcessor(JsonStdoutExporter()))
    provider.add_log_record_processor(
        SimpleLogRecordProcessor(DbLogExporter(database_url))
    )
    set_logger_provider(provider)
    _provider = provider
    trace.set_tracer_provider(TracerProvider(resource=resource))
    return trace.get_tracer(SERVICE_NAME)


def configure_otlp(enabled, endpoint):
    """Attach (or detach) the OTLP/HTTP log exporter at runtime.

    Called at startup with the stored settings and again whenever the
    settings change, so toggling the OpenTelemetry integration does
    not need a restart. A shut-down processor stays registered but
    inert. Never raises: a bad endpoint must not break logging."""
    global _otlp_processor
    if _otlp_processor is not None:
        try:
            _otlp_processor.shutdown()
        except Exception:
            pass
        _otlp_processor = None
    if not enabled or not endpoint or _provider is None:
        return
    try:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter,
        )
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

        _otlp_processor = BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=endpoint)
        )
        _provider.add_log_record_processor(_otlp_processor)
    except Exception as e:
        print(f"OTLP exporter setup failed: {e}", file=sys.stderr, flush=True)
        _otlp_processor = None


def emit_event(event, severity="INFO", **attributes):
    """Record one structured operation event, e.g.
    emit_event("fetch.finished", run_id=7, ok=63, ng=0)."""
    text, number = _SEVERITIES.get(severity.upper(), _SEVERITIES["INFO"])
    get_logger("stock-app.events").emit(
        LogRecord(
            timestamp=datetime.now(timezone.utc),
            severity_text=text,
            severity_number=number,
            body=event,
            attributes=attributes,
        )
    )
