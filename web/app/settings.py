"""設定まわりのサービス層。

数値設定（KPI）とアプリ設定（ログ保持期間・OTel・Swagger・
夜間バッチ開始時刻・優待期限アラート日数）の読み書き。月あたり所得の目標はここで
自動算出する（直接入力した値を保存しない）。

formatting.py（round_half_up）と config.py にのみ依存する
（循環 import を作らない）。
"""

import re

from app.formatting import round_half_up

DEFAULT_FETCH_START_TIME = "01:00"
FETCH_START_TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
_FETCH_START_TIME_RE = re.compile(FETCH_START_TIME_PATTERN)

DEFAULT_BENEFIT_DEADLINE_ALERT_DAYS = 30


def is_valid_fetch_start_time(value):
    """True when value is a valid "HH:MM" fetch start time."""
    return isinstance(value, str) and bool(_FETCH_START_TIME_RE.match(value))


def get_settings(conn):
    """Numeric settings as floats (the KPI values). Non-numeric values
    (e.g. the OTel endpoint) are skipped here and read via app_config."""
    out = {}
    for r in conn.execute("SELECT key, value FROM settings").fetchall():
        try:
            out[r["key"]] = float(r["value"])
        except (TypeError, ValueError):
            continue
    return out


def settings_view(st):
    """The settings as the UI/API present them (percent units, with the
    derived monthly target)."""
    total = st.get("target_dividend_total", 0)
    tax = st.get("tax_rate", 0.20315)
    return dict(
        tax_rate_pct=(tax * 100),
        target_dividend_total=total,
        target_monthly_income=round_half_up(total * (1 - tax) / 12),
        target_yield_buy_pct=(st.get("target_yield_buy", 0) * 100),
        target_yield_now_pct=(st.get("target_yield_now", 0) * 100),
        target_yield_portfolio_pct=(st.get("target_yield_portfolio", 0.04) * 100),
    )


def update_settings(conn, *, tax_rate_pct, target_dividend_total,
                    target_yield_buy_pct, target_yield_now_pct,
                    target_yield_portfolio_pct, log_retention_days=90,
                    otel_enabled=False, otel_endpoint=None,
                    swagger_enabled=False,
                    fetch_start_time=DEFAULT_FETCH_START_TIME,
                    benefit_deadline_alert_days=(
                        DEFAULT_BENEFIT_DEADLINE_ALERT_DAYS)):
    # Monthly after-tax income target is derived, not entered directly:
    # annual dividend target x (1 - tax rate) / 12.
    monthly = round_half_up(target_dividend_total * (1 - tax_rate_pct / 100) / 12)
    values = {
        "tax_rate": tax_rate_pct / 100,
        "target_dividend_total": target_dividend_total,
        "target_monthly_income": float(monthly),
        "target_yield_buy": target_yield_buy_pct / 100,
        "target_yield_now": target_yield_now_pct / 100,
        "target_yield_portfolio": target_yield_portfolio_pct / 100,
        "log_retention_days": float(log_retention_days),
        "benefit_deadline_alert_days": float(benefit_deadline_alert_days),
    }
    for k, v in values.items():
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (%s, %s)"
            " ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
            (k, repr(v)),
        )
    # Non-numeric settings are stored as plain strings.
    for k, v in (
        ("otel_enabled", "true" if otel_enabled else "false"),
        ("otel_endpoint", otel_endpoint or ""),
        ("swagger_enabled", "true" if swagger_enabled else "false"),
        ("fetch_start_time", fetch_start_time),
    ):
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (%s, %s)"
            " ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
            (k, v),
        )
    values["otel_enabled"] = bool(otel_enabled)
    values["swagger_enabled"] = bool(swagger_enabled)
    values["fetch_start_time"] = fetch_start_time
    return values


DEFAULT_LOG_RETENTION_DAYS = 90


def _raw_setting(conn, key):
    row = conn.execute(
        "SELECT value FROM settings WHERE key = %s", (key,)
    ).fetchone()
    return row["value"] if row else None


def app_config(conn):
    """The non-KPI app settings: log retention, OTel integration,
    Swagger, the nightly fetch start time, and the benefit deadline
    alert threshold."""
    retention = _raw_setting(conn, "log_retention_days")
    try:
        retention_days = (
            int(float(retention)) if retention else DEFAULT_LOG_RETENTION_DAYS
        )
    except ValueError:
        retention_days = DEFAULT_LOG_RETENTION_DAYS
    stored_start_time = _raw_setting(conn, "fetch_start_time")
    alert_raw = _raw_setting(conn, "benefit_deadline_alert_days")
    try:
        alert_days = (
            int(float(alert_raw))
            if alert_raw
            else DEFAULT_BENEFIT_DEADLINE_ALERT_DAYS
        )
    except ValueError:
        alert_days = DEFAULT_BENEFIT_DEADLINE_ALERT_DAYS
    if not 0 <= alert_days <= 3650:
        alert_days = DEFAULT_BENEFIT_DEADLINE_ALERT_DAYS
    return dict(
        log_retention_days=retention_days,
        otel_enabled=_raw_setting(conn, "otel_enabled") == "true",
        otel_endpoint=_raw_setting(conn, "otel_endpoint") or "",
        swagger_enabled=_raw_setting(conn, "swagger_enabled") == "true",
        fetch_start_time=(
            stored_start_time
            if is_valid_fetch_start_time(stored_start_time)
            else DEFAULT_FETCH_START_TIME
        ),
        benefit_deadline_alert_days=alert_days,
    )


def settings_full_view(conn):
    """Everything the settings page / API exposes: KPI view + app config."""
    view = settings_view(get_settings(conn))
    view.update(app_config(conn))
    return view


def prune_app_logs(conn, retention_days):
    cur = conn.execute(
        "DELETE FROM app_logs WHERE ts < now() - make_interval(days => %s)",
        (retention_days,),
    )
    return cur.rowcount
