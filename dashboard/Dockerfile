# dashboard/Dockerfile — 運用ダッシュボードの Cloud Run 用イメージ(2026-08-03 代表指示)。
#
# ビルドコンテキストは**リポジトリルート**(dashboard/ だけでは足りない — queries.py が
# src/ryza・config/*.yaml・ops/reminders.yaml・site/data.js を読む)。ビルドは
# ops/deploy-dashboard.sh が Cloud Build 経由で行う:
#   docker build -f dashboard/Dockerfile -t <image> .
#
# 認証は持たない(IAP に全面委譲)。PORT は Cloud Run が注入(既定 8080)。
# ヘルスチェック: Streamlit は /_stcore/health を返す(Cloud Run 既定の TCP probe でも可)。
#
# ベースイメージは**ダイジェスト固定**(独立役員審査 2026-08-03 低-10)。タグは可変で、
# 同じコミットから作ったイメージが別物になりうる — code_version(コミット SHA)と
# 稼働イメージの対応が崩れるため(不変原則3)。タグも併記して意図を残す。
# 更新手順(タグを上げるときは digest も取り直す):
#   TOK=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull" | jq -r .token)
#   curl -sI -H "Authorization: Bearer $TOK" \
#        -H "Accept: application/vnd.oci.image.index.v1+json" \
#        https://registry-1.docker.io/v2/library/python/manifests/3.12-slim | grep -i docker-content-digest
#   (ghcr.io は https://ghcr.io/token?scope=repository:astral-sh/uv:pull で同様。
#    docker があれば `docker manifest inspect <image> -v | jq -r .Descriptor.digest` でも可)
# 取得日: 2026-08-03
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

# uv(公式静的バイナリ)。依存は uv.lock で固定同期する(CI と同流儀)。
COPY --from=ghcr.io/astral-sh/uv:0.7@sha256:629240833dd25d03949509fc01ceff56ae74f5e5f0fd264da634dd2f70e9cc70 /uv /uvx /bin/

WORKDIR /app

# 依存解決に必要な最小ファイル → レイヤキャッシュを効かせてから残りを copy。
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --locked --no-dev --extra dashboard

# アプリ本体(dashboard/static/fonts の self-host WOFF2 を含む)と、queries.py が
# 参照するリポジトリ内データ。
COPY dashboard ./dashboard
# DADS トークンのテーマ設定。Streamlit は**カレントディレクトリの** .streamlit/config.toml
# を読むため、WORKDIR(/app)直下に置く必要がある。これが無いとクラウド側だけ既定テーマに
# 戻り、ローカルと見た目が食い違う(2026-08-03 デザイン改修)。
COPY .streamlit ./.streamlit
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
