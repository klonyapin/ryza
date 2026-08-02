"""経営管理部の運用ジョブ群。

- ``weekly``: 週次運用ジョブ ops-weekly(リマインダー評価・発火 + 週次ダイジェスト)。
- ``github``: GitHub REST API 薄クライアント(fine-grained PAT、stdlib のみ)。

GCP 上(Cloud Run Job + Cloud Scheduler)で動くことを前提に、アプリ本体や DB セッションに
依存しない。将来の日次バッチはこのモジュール構成を雛形にする。
"""

from __future__ import annotations
