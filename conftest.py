"""テスト用の環境変数設定。

x2b.pyはモジュール読み込み時に環境変数チェックとsys.exitを行うため、
importより前に必ずダミー値を設定しておく。
"""

import os

os.environ.setdefault("BSKY_HANDLE", "test.bsky.social")
os.environ.setdefault("BSKY_APP_PASSWORD", "test-app-password")
os.environ.setdefault("X_LIST_ID", "test-list-id")
