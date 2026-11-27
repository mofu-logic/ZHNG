"""アプリ全体で使う設定と定数。

環境変数と標準ライブラリのみに依存する。DB や HTTP には依存しない。

DATABASE_URL は main.py にあったときと同じくモジュール import 時に
環境変数から読む。テストは conftest.py が環境変数を設定してから
app.main（経由でこのモジュール）を import するため、前提は崩れない。
"""

import os
from zoneinfo import ZoneInfo

DATABASE_URL = os.environ["DATABASE_URL"]

JST = ZoneInfo("Asia/Tokyo")

# 約定状態の語彙は「注文中／一部約定／約定済」の3つに統一する。
TRADE_STATUSES = ("注文中", "一部約定", "約定済")
