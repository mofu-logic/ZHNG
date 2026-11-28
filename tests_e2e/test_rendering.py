"""描画の回帰テスト。

文字列ベースの既存テストでは検出できない「実際に描画すると崩れる」
問題を、ブラウザで描画して数値で固定する。過去に実物で見つかった
不具合（日付セルの折り返し、設定スイッチの位置ずれ）の再発防止。
"""

from helpers import single_line_info

EXPECTED_SWITCH_INSET_PX = 53
TOLERANCE_PX = 1


def test_settings_switches_aligned_right(page, app_server):
    """設定画面の3スイッチは、行の右端から同じ距離（53px）にそろう。"""
    page.goto(app_server + "/settings")
    distances = page.locator("input.switch").evaluate_all(
        """(els) => els.map(el => {
          const r = el.getBoundingClientRect();
          const row = el.closest('tr').getBoundingClientRect();
          return {name: el.name, distance: row.right - r.right};
        })"""
    )
    assert len(distances) == 3, distances
    for d in distances:
        assert abs(d["distance"] - EXPECTED_SWITCH_INSET_PX) <= TOLERANCE_PX, (
            f"{d['name']} の右端距離が {d['distance']}px で"
            f" {EXPECTED_SWITCH_INSET_PX}px からずれている"
        )
    values = [d["distance"] for d in distances]
    assert max(values) - min(values) <= TOLERANCE_PX, (
        f"スイッチの右端距離がそろっていない: {distances}"
    )


def test_history_dates_single_line(page, app_server):
    """売買履歴の日付セルは折り返さず1行に収まる。"""
    page.goto(app_server + "/history")
    cells = single_line_info(page, "#buys tbody td.date")
    assert len(cells) == 3, cells
    for cell in cells:
        assert cell["text"], "日付セルが空（シードの日付が出ていない）"
        assert cell["lines"] == 1, f"日付セルが折り返している: {cell}"


def test_holdings_dates_single_line(page, app_server):
    """保有株一覧の日付セル（買付日）は折り返さず1行に収まる。"""
    page.goto(app_server + "/")
    cells = single_line_info(page, "#holdings tbody td.date")
    assert len(cells) == 3, cells
    for cell in cells:
        assert cell["text"], "買付日セルが空（シードの日付が出ていない）"
        assert cell["lines"] == 1, f"買付日セルが折り返している: {cell}"
