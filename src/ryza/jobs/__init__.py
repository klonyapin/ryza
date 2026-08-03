"""jobs — 定時ジョブのオーケストレータ(T-013)。

``daily`` は日次サイクル(取込 → 前処理 → 分析 → 市場観更新 → 朝刊 → outbox)を常駐実行する。
GCE VM(ryza-bot)上の systemd timer(``ops/deploy-daily.sh``)から起動する。
"""
