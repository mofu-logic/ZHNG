"""設定まわりのサービス層。

数値設定（KPI）とアプリ設定（ログ保持期間・OTel・Swagger）の
読み書き。月あたり所得の目標はここで自動算出する（直接入力した
値を保存しない）。

formatting.py（round_half_up）と config.py にのみ依存する
（循環 import を作らない）。
"""

from app.formatting import round_half_up


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
        target_yield_value_pct=(st.get("target_yield_value", 0.04) * 100),
    )


def update_settings(conn, *, tax_rate_pct, target_dividend_total,
                    target_yield_buy_pct, target_yield_now_pct,
                    target_yield_value_pct, log_retention_days=90,
                    otel_enabled=False, otel_endpoint=None,
                    swagger_enabled=False):
    # Monthly after-tax income target is derived, not entered directly:
    # annual dividend target x (1 - tax rate) / 12.
    monthly = round_half_up(target_dividend_total * (1 - tax_rate_pct / 100) / 12)
    values = {
        "tax_rate": tax_rate_pct / 100,
        "target_dividend_total": target_dividend_total,
        "target_monthly_income": float(monthly),
        "target_yield_buy": target_yield_buy_pct / 100,
        "target_yield_now": target_yield_now_pct / 100,
        "target_yield_value": target_yield_value_pct / 100,
        "log_retention_days": float(log_retention_days),
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
    ):
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (%s, %s)"
            " ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
            (k, v),
        )
    values["otel_enabled"] = bool(otel_enabled)
    values["swagger_enabled"] = bool(swagger_enabled)
    return values


DEFAULT_LOG_RETENTION_DAYS = 90


def _raw_setting(conn, key):
    row = conn.execute(
        "SELECT value FROM settings WHERE key = %s", (key,)
    ).fetchone()
    return row["value"] if row else None


def app_config(conn):
    """The non-KPI app settings: log retention and OTel integration."""
    retention = _raw_setting(conn, "log_retention_days")
    try:
        retention_days = (
            int(float(retention)) if retention else DEFAULT_LOG_RETENTION_DAYS
        )
    except ValueError:
        retention_days = DEFAULT_LOG_RETENTION_DAYS
    return dict(
        log_retention_days=retention_days,
        otel_enabled=_raw_setting(conn, "otel_enabled") == "true",
        otel_endpoint=_raw_setting(conn, "otel_endpoint") or "",
        swagger_enabled=_raw_setting(conn, "swagger_enabled") == "true",
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
