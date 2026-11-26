"""Structured operation logging (OpenTelemetry-aligned).

Events must land in the app_logs table (for the /logs page) and as
OTel-shaped JSON lines on stdout (for future external integration),
with severity mapping and exception details intact.
"""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from opentelemetry._logs import SeverityNumber

from app import main
from app.observability import emit_event, format_record
from test_fetch import _seed_two_stocks, make_fake_client


def _rows(db, body):
    return db.execute(
        "SELECT * FROM app_logs WHERE body = %s", (body,)
    ).fetchall()


def test_emit_event_writes_db_row(db, capsys):
    emit_event("test.event", answer=42, label="x")
    rows = _rows(db, "test.event")
    assert len(rows) == 1
    row = rows[0]
    assert row["severity"] == "INFO"
    assert row["severity_number"] == 9
    assert row["attributes"]["answer"] == 42
    assert row["attributes"]["label"] == "x"


def test_emit_event_stdout_json_shape(db, capsys):
    emit_event("test.shape", severity="WARNING", code="9999")
    lines = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if '"test.shape"' in line
    ]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["severity_text"] == "WARN"
    assert rec["severity_number"] == 13
    assert rec["body"] == "test.shape"
    assert rec["attributes"] == {"code": "9999"}
    assert rec["resource"] == {"service.name": "stock-app"}
    assert "timestamp" in rec and "trace_id" in rec and "span_id" in rec


def test_emit_event_error_severity(db):
    emit_event("test.error", severity="ERROR")
    row = _rows(db, "test.error")[0]
    assert row["severity"] == "ERROR"
    assert row["severity_number"] == 17


def test_bridge_handler_records_exception(db):
    try:
        raise ValueError("boom")
    except ValueError:
        main.logger.exception("something failed")
    rows = _rows(db, "something failed")
    assert len(rows) == 1
    attrs = rows[0]["attributes"]
    assert attrs["exception.type"] == "ValueError"
    assert attrs["exception.message"] == "boom"
    assert "ValueError" in attrs["exception.stacktrace"]
    assert rows[0]["severity"] == "ERROR"


def test_format_record_shape():
    record = SimpleNamespace(
        log_record=SimpleNamespace(
            timestamp=datetime(2026, 11, 26, 1, 0, tzinfo=timezone.utc),
            observed_timestamp=None,
            severity_text="INFO",
            severity_number=SeverityNumber.INFO,
            body="hello",
            attributes={"a": 1},
            context=None,
        )
    )
    d = format_record(record)
    assert d["body"] == "hello"
    assert d["severity_number"] == 9
    assert d["attributes"] == {"a": 1}
    assert d["trace_id"] == "" and d["span_id"] == ""
    assert d["timestamp"].startswith("2026-11-26T01:00")


def _fake_record(body="x", severity_number=SeverityNumber.INFO):
    return SimpleNamespace(
        log_record=SimpleNamespace(
            timestamp=datetime(2026, 11, 26, 1, 0, tzinfo=timezone.utc),
            observed_timestamp=None,
            severity_text="INFO",
            severity_number=severity_number,
            body=body,
            attributes={},
            context=None,
            trace_id=None,
            span_id=None,
        )
    )


def test_exporter_boilerplate_and_failures(db, monkeypatch):
    from app.observability import DbLogExporter, JsonStdoutExporter

    out = JsonStdoutExporter()
    assert out.export([_fake_record()]) is not None
    out.shutdown()
    assert out.force_flush() is True

    def boom(*a, **k):
        raise RuntimeError("print failed")

    monkeypatch.setattr("builtins.print", boom)
    from opentelemetry.sdk._logs.export import LogExportResult

    assert out.export([_fake_record()]) == LogExportResult.FAILURE
    monkeypatch.undo()

    # DB exporter against an unreachable database fails soft.
    bad = DbLogExporter("postgresql://stock:x@127.0.0.1:1/nope")
    assert bad.export([_fake_record()]) == LogExportResult.FAILURE
    bad.shutdown()
    assert bad.force_flush() is True

    good = DbLogExporter(main.DATABASE_URL)
    assert good.export([_fake_record(body="exporter.ok")]) == LogExportResult.SUCCESS
    assert len(_rows(db, "exporter.ok")) == 1


def test_bridge_handler_debug_and_failure_safety(db, monkeypatch):
    import logging as py_logging

    from app import observability

    handler = observability.OtelBridgeHandler()
    record = py_logging.LogRecord(
        name="stock-app", level=py_logging.DEBUG, pathname=__file__,
        lineno=1, msg="dbg message", args=(), exc_info=None,
    )
    handler.emit(record)
    rows = _rows(db, "dbg message")
    assert len(rows) == 1
    assert rows[0]["severity"] == "DEBUG"

    def broken_logger(name):
        raise RuntimeError("no provider")

    monkeypatch.setattr(observability, "get_logger", broken_logger)
    handler.emit(record)  # must not raise


def test_format_record_context_fallback():
    from opentelemetry import trace
    from opentelemetry.context import get_current

    with main.tracer.start_as_current_span("req"):
        ctx = get_current()
        span_context = trace.get_current_span(ctx).get_span_context()
        expected = format(span_context.trace_id, "032x")
    record = _fake_record()
    record.log_record.context = ctx
    d = format_record(record)
    assert d["trace_id"] == expected
    assert d["span_id"] != ""


def _seed_log(db, body, severity, number, attrs="{}"):
    db.execute(
        "INSERT INTO app_logs(ts, severity, severity_number, body, attributes)"
        " VALUES (now(), %s, %s, %s, %s::jsonb)",
        (severity, number, body, attrs),
    )
    db.commit()


def test_logs_page_and_filters(client, db):
    _seed_log(db, "app.started", "INFO", 9)
    _seed_log(db, "fetch.code_failed", "ERROR", 17, '{"code": "9999"}')

    r = client.get("/logs")
    assert r.status_code == 200
    assert "app.started" in r.text
    assert "fetch.code_failed" in r.text

    r = client.get("/logs", params={"severity": "ERROR"})
    assert "fetch.code_failed" in r.text
    assert "app.started" not in r.text

    r = client.get("/logs", params={"q": "9999"})
    assert "fetch.code_failed" in r.text
    assert "app.started" not in r.text


def test_http_request_event_logged(client, db):
    seed = db.execute("SELECT count(*) c FROM app_logs").fetchone()["c"]
    assert client.get("/healthz").status_code == 200
    assert client.get("/logs").status_code == 200
    rows = _rows(db, "http.request")
    paths = [r["attributes"]["path"] for r in rows]
    assert "/logs" in paths  # the page view itself is logged
    assert "/healthz" not in paths  # health checks are excluded
    assert all(r["attributes"]["status"] == 200 for r in rows)


def test_settings_update_event(client, db):
    client.put(
        "/api/v1/settings",
        json={
            "tax_rate_pct": 20.315,
            "target_dividend_total": 1200000,
            "target_yield_buy_pct": 3.0,
            "target_yield_now_pct": 3.5,
            "target_yield_value_pct": 4.0,
        },
    )
    rows = _rows(db, "settings.updated")
    assert len(rows) == 1
    assert rows[0]["attributes"]["target_dividend_total"] == 1200000.0


def test_fetch_events(db, monkeypatch):
    _seed_two_stocks(db)

    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    main._fetch_lock = asyncio.Lock()
    monkeypatch.setattr(main.httpx, "AsyncClient", make_fake_client())
    asyncio.run(main.fetch_all("batch"))

    started = _rows(db, "fetch.started")
    finished = _rows(db, "fetch.finished")
    assert len(started) == 1
    assert started[0]["attributes"]["total"] == 2
    assert len(finished) == 1
    assert finished[0]["attributes"]["ok"] == 2
    assert finished[0]["attributes"]["ng"] == 0
