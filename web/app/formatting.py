"""数値の表示整形と Jinja フィルタ。

画面に表示する数値の整形はここに集約する。純粋関数だけからなり、
DB や HTTP には依存しない。
"""


def fnum(v):
    return None if v is None else float(v)


def fmt_price(v):
    if v is None:
        return "―"
    v = float(v)
    s = f"{v:,.2f}".rstrip("0").rstrip(".")
    return s


def fmt_yen(v):
    if v is None:
        return "―"
    v = float(v)
    if v == int(v):
        return f"{int(v):,}"
    return f"{v:,.1f}"


def fmt_signed(v):
    if v is None:
        return "―"
    v = float(v)
    sign = "+" if v > 0 else ""
    return sign + fmt_yen(v)


def fmt_pct(v):
    if v is None:
        return "―"
    return f"{float(v) * 100:.2f}%"


def fmt_pct_value(v):
    """Format a value that is already in percent units (e.g. snapshot yields)."""
    if v is None:
        return "―"
    return f"{float(v):.2f}%"


def fmt_pct_signed(v):
    if v is None:
        return "―"
    v = float(v)
    return ("+" if v > 0 else "") + f"{v:.2f}%"


def round_half_up(v):
    return int(v + 0.5)


def register_jinja_filters(env):
    """このモジュールの整形関数を Jinja フィルタとして登録する。

    main.py が Jinja2Templates を作った直後に呼ぶこと。
    フィルタ名と対応関数の組み合わせは従来どおり。
    """
    env.filters["pctv"] = fmt_pct_value
    env.filters["pcts"] = fmt_pct_signed
    env.filters["signed2"] = lambda v: (
        "" if v is None else ("+" if float(v) > 0 else "") + fmt_price(v)
    )
    env.filters["price"] = fmt_price
    env.filters["yen"] = fmt_yen
    env.filters["signed"] = fmt_signed
    env.filters["pct"] = fmt_pct
