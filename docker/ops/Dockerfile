# 経営管理部ジョブ ops-weekly(T-004)の実行イメージ。
# Cloud Run Job として起動し、GitHub API とリマインダー・レジストリで週次運用を回す。
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
