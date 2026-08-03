# 経営管理部ジョブ ops-weekly(T-004)の実行イメージ。
# Cloud Run Job として起動し、GitHub API とリマインダー・レジストリで週次運用を回す。
#
# **2026-08-04 時点では稼働経路ではない**。週次ジョブは GCE VM(ryza-bot)の systemd timer
# (ops/deploy-ops-weekly.sh)で動く — 形骸化監査(BOARDROOM_AUDIT)が VM 内 PostgreSQL を
# 読むため。本イメージ定義は Cloud SQL 移行後に Cloud Run へ戻す選択肢として残してある。
# 稼働していない定義であることを明記しないと「どちらが本番か」が読み取れない。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 依存の解決を先に行いレイヤキャッシュを効かせる。ops extra に BigQuery クライアントが入る。
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[ops]"

# 運用ジョブが参照するリマインダー・レジストリ(GitHub API 取得のフォールバックにも使える)。
COPY ops/reminders.yaml ./ops/reminders.yaml

# Cloud Run Job のエントリポイント。DRY_RUN / GITHUB_TOKEN / GITHUB_REPO は env で注入。
ENTRYPOINT ["python", "-m", "ryza.ops.weekly"]
