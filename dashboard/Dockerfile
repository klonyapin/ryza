# dashboard/Dockerfile — 運用ダッシュボードの Cloud Run 用イメージ(2026-08-03 代表指示)。
#
# ビルドコンテキストは**リポジトリルート**(dashboard/ だけでは足りない — queries.py が
# src/ryza・config/*.yaml・ops/reminders.yaml・site/data.js を読む)。ビルドは
# ops/deploy-dashboard.sh が Cloud Build 経由で行う:
#   docker build -f dashboard/Dockerfile -t <image> .
#
# 認証は持たない(IAP に全面委譲)。PORT は Cloud Run が注入(既定 8080)。
# ヘルスチェック: Streamlit は /_stcore/health を返す(Cloud Run 既定の TCP probe でも可)。
FROM python:3.12-slim

# uv(公式静的バイナリ)。依存は uv.lock で固定同期する(CI と同流儀)。
COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /bin/

WORKDIR /app

# 依存解決に必要な最小ファイル → レイヤキャッシュを効かせてから残りを copy。
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --locked --no-dev --extra dashboard

# アプリ本体と、queries.py が参照するリポジトリ内データ。
COPY dashboard ./dashboard
COPY config ./config
COPY ops/reminders.yaml ./ops/reminders.yaml
COPY site/data.js ./site/data.js

ENV PATH="/app/.venv/bin:${PATH}"
EXPOSE 8080

# PORT は Cloud Run が与える(ローカル docker run では既定 8080)。
CMD streamlit run dashboard/app.py \
    --server.port "${PORT:-8080}" \
    --server.address 0.0.0.0 \
    --server.headless true \
    --browser.gatherUsageStats false
