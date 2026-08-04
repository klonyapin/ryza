#!/usr/bin/env bash
#
# deploy-bot.sh — Ryza 本体 Discord Bot(T-006)を GCE e2-micro に常駐デプロイする。
#
# 冪等: 何度再実行してもよい。VM が無ければ作成し、あれば作成をスキップして
# 「コード同期 + マイグレーション適用 + systemd 再起動」だけを行う(受け入れ基準3)。
#
# 構成(30-press-discord.md §5 / 00-system-design §10):
#   - GCE e2-micro(us-west1・無料枠)に discord.py Bot を systemd(Restart=always)で常駐。
#   - DB は当面 VM 内 PostgreSQL に同居。将来の Cloud SQL 移行は RYZA_DATABASE_URL の差し替えのみ。
#   - Bot トークンは Secret Manager('discord-bot-token')から起動時取得(VM のメタデータには置かない)。
#   - 起動時に4チャンネル(報道/承認/運営/dev)を指定カテゴリ配下へ ensure し、#運営 へ再起動通知。
#
# 前提(設計リードが事前に用意 — スクリプトはここを検証して中断する):
#   - gcloud 認証済み・API 有効化済み(compute, secretmanager)
#   - Secret Manager に `discord-bot-token` を登録済み(値の投入はユーザー作業):
#       printf %s '<BOT_TOKEN>' | gcloud secrets create discord-bot-token \
#         --data-file=- --replication-policy=automatic
#
# 使い方(実デプロイと Discord 疎通の実行は設計リード担当。本スクリプトは手順の自動化まで):
#   RYZA_OWNER_IDS=111111111111111111 ./ops/deploy-bot.sh
#   # カテゴリ・オーナーは env で上書き可(ハードコードしない):
#   RYZA_OWNER_IDS=... RYZA_DISCORD_CATEGORY_ID=... GUILD_ID=... ./ops/deploy-bot.sh
#
# デプロイ後の確認(VM 上):
#   gcloud compute ssh ryza-bot --zone us-west1-a --command 'systemctl status ryza-bot --no-pager'
#   gcloud compute ssh ryza-bot --zone us-west1-a --command 'journalctl -u ryza-bot -n 50 --no-pager'
#
set -euo pipefail

# ── 設定(env で上書き可・ハードコードしない) ──────────────────────────────────
PROJECT="${PROJECT:-ryza-main}"
ZONE="${ZONE:-us-west1-a}"              # e2-micro 無料枠は us-west1/us-central1/us-east1
VM="${VM:-ryza-bot}"
MACHINE="${MACHINE:-e2-micro}"
IMAGE_FAMILY="${IMAGE_FAMILY:-debian-12}"
IMAGE_PROJECT="${IMAGE_PROJECT:-debian-cloud}"
SECRET="${SECRET:-discord-bot-token}"

# Bot 実行時の環境変数(VM の /etc/ryza/bot.env に配置)。
CATEGORY_ID="${RYZA_DISCORD_CATEGORY_ID:-1533512287816782017}"
OWNER_IDS="${RYZA_OWNER_IDS:?RYZA_OWNER_IDS=<Discord ユーザーID,カンマ区切り> を指定してください}"
GUILD_ID="${GUILD_ID:-}"               # 任意: スラッシュコマンド即時同期先ギルド
DATABASE_URL="${RYZA_DATABASE_URL:-postgresql://ryza:ryza@localhost:5432/ryza}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH=(gcloud compute ssh "${VM}" --zone "${ZONE}" --project "${PROJECT}")

echo "== ryza-bot deploy =="
echo "project=${PROJECT} zone=${ZONE} vm=${VM} machine=${MACHINE}"

gcloud config set project "${PROJECT}" >/dev/null

# ── 0. Secret の存在確認(未登録なら中断) ────────────────────────────────────
if ! gcloud secrets describe "${SECRET}" >/dev/null 2>&1; then
  echo "ERROR: Secret '${SECRET}' が未登録です。先に Bot トークンを登録してください:" >&2
  echo "  printf %s '<BOT_TOKEN>' | gcloud secrets create ${SECRET} --data-file=- --replication-policy=automatic" >&2
  exit 1
fi

# デフォルトの Compute SA に Secret Accessor を付与(起動時トークン取得のため・冪等)。
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
RUNTIME_SA="${RUNTIME_SA:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"
echo "-- Secret Accessor を SA に付与(冪等): ${RUNTIME_SA}"
gcloud secrets add-iam-policy-binding "${SECRET}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor" >/dev/null

# ── 1. VM(無ければ作成・あればスキップ=冪等) ───────────────────────────────
if gcloud compute instances describe "${VM}" --zone "${ZONE}" >/dev/null 2>&1; then
  echo "-- VM '${VM}' は既存(作成をスキップし、コード更新+再起動のみ)"
else
  echo "-- VM '${VM}' を作成(${MACHINE}, ${IMAGE_FAMILY})"
  gcloud compute instances create "${VM}" \
    --zone="${ZONE}" \
    --machine-type="${MACHINE}" \
    --image-family="${IMAGE_FAMILY}" \
    --image-project="${IMAGE_PROJECT}" \
    --service-account="${RUNTIME_SA}" \
    --scopes="cloud-platform" \
    --boot-disk-size=30GB \
    --boot-disk-type=pd-standard
  echo "-- SSH 疎通を待機"
  until "${SSH[@]}" --command 'true' >/dev/null 2>&1; do sleep 5; done
fi

# ── 2. コード同期(git 追跡ファイルのみを tar して転送。.git や _evidence は含めない) ──
echo "-- コードを VM へ同期"
TARBALL="$(mktemp /tmp/ryza-bot.XXXXXX.tar.gz)"
trap 'rm -f "${TARBALL}"' EXIT
git -C "${ROOT}" archive --format=tar.gz -o "${TARBALL}" HEAD
gcloud compute scp "${TARBALL}" "${VM}:/tmp/ryza-src.tar.gz" --zone "${ZONE}" --project "${PROJECT}"

# ── 3. リモート・プロビジョニング(全ステップ冪等) ───────────────────────────
# PostgreSQL 同居 → ロール/DB 作成 → uv で Python 環境 → .[bot] インストール →
# マイグレーション適用 → env ファイル → systemd ユニット → 有効化+再起動。
echo "-- VM 上でプロビジョニング(冪等)"
"${SSH[@]}" --command "sudo bash -s" <<REMOTE
set -euo pipefail

# 3.1 システムパッケージ(apt は冪等)。PostgreSQL 17 + 拡張(pg_partman/pgvector)は
#     Debian 標準 repo に無いため PGDG リポジトリから導入(docker/postgres と同構成)。
export DEBIAN_FRONTEND=noninteractive
# 過去実行の壊れた pgdg.list が残っていても最初の update が通るよう先に除去(後で書き直す)
rm -f /etc/apt/sources.list.d/pgdg.list
apt-get update -qq
apt-get install -y -qq curl ca-certificates gnupg >/dev/null
install -d /usr/share/postgresql-common/pgdg
[ -f /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc ] || \
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
# IMAGE_FAMILY=debian-12 固定のためコード名 bookworm を直書き(heredoc のローカル展開回避)
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
  > /etc/apt/sources.list.d/pgdg.list
apt-get update -qq
apt-get install -y -qq postgresql-17 postgresql-client-17 \
  postgresql-17-partman postgresql-17-pgvector >/dev/null

# 3.2 PostgreSQL: ロール ryza と DB ryza を idempotent に用意。
systemctl enable --now postgresql
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='ryza'" | grep -q 1 \
  || sudo -u postgres psql -c "CREATE ROLE ryza LOGIN PASSWORD 'ryza'"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='ryza'" | grep -q 1 \
  || sudo -u postgres createdb -O ryza ryza
# 拡張の CREATE は superuser 限定のため、migrations(role ryza)より先に superuser で用意
# (migrations 側は IF NOT EXISTS のためそのまま通る)
sudo -u postgres psql -d ryza -c "CREATE SCHEMA IF NOT EXISTS partman AUTHORIZATION ryza" >/dev/null
sudo -u postgres psql -d ryza -c "CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman" >/dev/null
sudo -u postgres psql -d ryza -c "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null

# 3.3 ソース展開(既存を消してから新しい tar を展開 = 冪等)。
#     uv.lock は git archive HEAD に含まれるため tar から復元される。旧世代の
#     `uv pip install -e '.[bot]'`(未固定)から `uv sync --locked` への移行後は、
#     lockfile が展開経路に残ることが CI と本番の依存解決を一致させる前提となる
#     (A-12 F-13・lockfile 項)。念のため wipe 対象にも uv.lock を含めて上書きを保証する。
install -d -o root -g root /opt/ryza
rm -rf /opt/ryza/src /opt/ryza/migrations /opt/ryza/pyproject.toml /opt/ryza/README.md /opt/ryza/uv.lock
tar -xzf /tmp/ryza-src.tar.gz -C /opt/ryza

# 3.4 uv(なければ導入)で lockfile 固定で .[bot] を同期(A-12 F-13)。
#     `uv pip install -e '.[bot]'` は毎回 pyproject.toml の `>=` を最新解決するため、
#     CI(`uv sync --locked`)との依存乖離を生む(Supply Chain の再現性劣化 —
#     pass4 所見 9 相当)。CI と同じ lockfile 固定機構(`uv sync --locked`)に統一する。
#     extras は用途別(CI は dev+dashboard、本 VM は bot)で、解決結果はいずれも
#     同一 uv.lock 由来。--python 3.12 は旧 `uv venv --python 3.12` のピンの継承
#     (CI も setup-uv で 3.12 固定 — 無指定だと新規 VM で uv が別系を拾い得る)。
#     lockfile が古ければ uv は即座に非ゼロ終了(set -e で SSH ごと落ちる)。
export HOME=/root
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="/root/.local/bin:\$PATH"
cd /opt/ryza
if [ ! -f uv.lock ]; then
  echo "ERROR: /opt/ryza/uv.lock が無い。tar に含まれていない可能性あり(デプロイ資材の欠落)。" >&2
  exit 1
fi
uv sync --locked --extra bot --python 3.12

# 3.5 マイグレーション適用(冪等: schema_migrations で未適用のみ実行)。
RYZA_DATABASE_URL='${DATABASE_URL}' .venv/bin/python -m ryza.db.migrate

# 3.6 実行時 env(トークンは含めない。Bot が Secret Manager から取得)。
install -d -m 0755 /etc/ryza
cat > /etc/ryza/bot.env <<ENV
RYZA_DISCORD_TOKEN_SECRET=${SECRET}
GCP_PROJECT=${PROJECT}
RYZA_OWNER_IDS=${OWNER_IDS}
RYZA_DISCORD_CATEGORY_ID=${CATEGORY_ID}
RYZA_GUILD_ID=${GUILD_ID}
RYZA_DATABASE_URL=${DATABASE_URL}
ENV
chmod 0640 /etc/ryza/bot.env

# 3.7 systemd ユニット(Restart=always で常駐。死活で自動再起動 → 起動時に #運営 通知)。
cat > /etc/systemd/system/ryza-bot.service <<UNIT
[Unit]
Description=Ryza Discord Bot (T-006)
After=network-online.target postgresql.service
Wants=network-online.target postgresql.service

[Service]
Type=simple
EnvironmentFile=/etc/ryza/bot.env
WorkingDirectory=/opt/ryza
ExecStart=/opt/ryza/.venv/bin/python -m ryza.bot.main
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT

# 3.8 反映(daemon-reload → enable → restart は冪等)。
systemctl daemon-reload
systemctl enable ryza-bot
systemctl restart ryza-bot
sleep 2
systemctl is-active ryza-bot
REMOTE

echo "== 完了 =="
echo "状態確認: gcloud compute ssh ${VM} --zone ${ZONE} --command 'systemctl status ryza-bot --no-pager'"
echo "ログ確認: gcloud compute ssh ${VM} --zone ${ZONE} --command 'journalctl -u ryza-bot -n 50 --no-pager'"
echo "疎通(テスト投稿)は設計リードが Discord 側で実施する。"
