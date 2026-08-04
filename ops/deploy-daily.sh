#!/usr/bin/env bash
#
# deploy-daily.sh — 日次サイクル(T-013)を GCE VM 上の systemd timer で常駐設置する。
#
# 冪等: 何度再実行してもよい。コード同期 + マイグレーション適用 + timer/service の再設置だけを行う。
#
# 構成判断(T-013 / 00-system-design §10):
#   - 日次ジョブは **Cloud Run Jobs ではなく** 既存 GCE VM(ryza-bot)上の systemd timer で動かす。
#     理由: DB が VM 内 PostgreSQL(localhost)にあり、Cloud Run からは届かない。30-press §1 の
#     「Cloud Run Jobs」は Cloud SQL 移行後の姿とし、当面はこの構成(コードコメントにも明記)。
#   - Bot(deploy-bot.sh)と同じ VM・同じ /opt/ryza・同じ venv を共有する。**deploy-bot.sh を先に
#     実行して VM を用意しておくこと**(本スクリプトは VM を作成しない)。
#   - Anthropic API キーは Secret Manager('anthropic-api-key')から実行時取得(VM メタデータ SA +
#     REST。src/ryza/research/providers.py の load_api_key)。env には鍵を置かない。
#
# 前提(設計リードが事前に用意 — スクリプトはここを検証して中断する):
#   - deploy-bot.sh 実行済み(VM ryza-bot・PostgreSQL・/opt/ryza・venv が存在)
#   - gcloud 認証済み・API 有効化済み(compute, secretmanager)
#   - Secret Manager に `anthropic-api-key` を登録済み(値の投入はユーザー作業):
#       printf %s '<ANTHROPIC_API_KEY>' | gcloud secrets create anthropic-api-key \
#         --data-file=- --replication-policy=automatic
#
# 使い方(実デプロイ・実スモークの実行は設計リード担当。本スクリプトは手順の自動化まで):
#   ./ops/deploy-daily.sh
#   # プロジェクト・ゾーン・VM・タイマー時刻は env で上書き可(ハードコードしない):
#   PROJECT=... ZONE=... VM=... DAILY_ONCALENDAR='*-*-* 09:00:00 Asia/Tokyo' ./ops/deploy-daily.sh
#
# デプロイ後の確認(VM 上):
#   gcloud compute ssh ryza-bot --zone us-west1-a --command 'systemctl list-timers ryza-daily --no-pager'
#   gcloud compute ssh ryza-bot --zone us-west1-a --command 'journalctl -u ryza-daily -n 50 --no-pager'
#   # 手動スモーク(ドライラン。実 API を呼ばない):
#   gcloud compute ssh ryza-bot --zone us-west1-a --command \
#     'cd /opt/ryza && sudo RYZA_DATABASE_URL=postgresql://ryza:ryza@localhost:5432/ryza \
#        .venv/bin/python -m ryza.jobs.daily --dry-run'
#
set -euo pipefail

# ── 設定(env で上書き可・ハードコードしない) ──────────────────────────────────
PROJECT="${PROJECT:-ryza-main}"
ZONE="${ZONE:-us-west1-a}"
VM="${VM:-ryza-bot}"
SECRET="${SECRET:-anthropic-api-key}"

# タイマー起動時刻(systemd OnCalendar・JST)。朝刊は 09:40 までに outbox 投入されるよう 09:00 起動。
# systemd 252(bookworm)は OnCalendar にタイムゾーンを直接書ける。
DAILY_ONCALENDAR="${DAILY_ONCALENDAR:-*-*-* 09:00:00 Asia/Tokyo}"

# 実行時 env(トークンは含めない。provider が Secret Manager から取得)。
DATABASE_URL="${RYZA_DATABASE_URL:-postgresql://ryza:ryza@localhost:5432/ryza}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH=(gcloud compute ssh "${VM}" --zone "${ZONE}" --project "${PROJECT}")

echo "== ryza-daily deploy =="
echo "project=${PROJECT} zone=${ZONE} vm=${VM} oncalendar='${DAILY_ONCALENDAR}'"

gcloud config set project "${PROJECT}" >/dev/null

# ── 0. VM の存在確認(無ければ中断 — deploy-bot.sh を先に) ─────────────────────
if ! gcloud compute instances describe "${VM}" --zone "${ZONE}" >/dev/null 2>&1; then
  echo "ERROR: VM '${VM}' が存在しません。先に ops/deploy-bot.sh を実行してください。" >&2
  exit 1
fi

# ── 1. Secret の存在確認 + Accessor 付与(冪等) ────────────────────────────────
if ! gcloud secrets describe "${SECRET}" >/dev/null 2>&1; then
  echo "ERROR: Secret '${SECRET}' が未登録です。先に Anthropic API キーを登録してください:" >&2
  echo "  printf %s '<ANTHROPIC_API_KEY>' | gcloud secrets create ${SECRET} --data-file=- --replication-policy=automatic" >&2
  exit 1
fi
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
RUNTIME_SA="${RUNTIME_SA:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"
echo "-- Secret Accessor を SA に付与(冪等): ${RUNTIME_SA}"
gcloud secrets add-iam-policy-binding "${SECRET}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor" >/dev/null

# ── 1b. データ取込 Secret の Accessor 付与(冪等・R-4 / docs/ops/gcp-iam-inventory.md D-3)
#
# 日次サイクルの実取込(jquants / fred / estat)が VM 上で読む Secret。これらは 2026-08-03 に
# **手動で** accessor を付与されており、どの deploy スクリプトにも宣言が無かった。そのため
# VM を作り直すと再現せず、取込だけが静かに落ちる(取込側は理由付きで skip するため、
# 気づくのは朝刊の欠落を見たときになる)。ここで宣言に取り込み、冪等に収束させる。
#
# `jquants-refresh-token` は**意図的に含めない**。J-Quants V2 は API キー認証のみで、
# refresh token は V1 の遺物である(src/ryza/ingest/jquants.py の api_key() 参照。
# コード検索でも参照は当該コメント1件のみ)。使われていない Secret に accessor を
# 宣言すると、宣言そのものが「使っている」という誤った証拠になるため足さない。
# 既存の手動付与の扱いは docs/ops/gcp-iam-inventory.md §8 の要調査項目とする。
#
# 未登録の Secret はエラーにせず警告に留める(取込は鍵が無ければ理由付きで skip する
# 縮退設計であり、e-Stat 等が未登録の段階でも日次サイクル自体は設置できるべきである)。
DATA_SECRETS="${DATA_SECRETS:-jquants-api-key fred-api-key estat-app-id}"
for ds in ${DATA_SECRETS}; do
  if gcloud secrets describe "${ds}" >/dev/null 2>&1; then
    echo "   - ${ds}: accessor 付与(冪等)"
    gcloud secrets add-iam-policy-binding "${ds}" \
      --member="serviceAccount:${RUNTIME_SA}" \
      --role="roles/secretmanager.secretAccessor" >/dev/null
  else
    echo "   ! WARN: Secret '${ds}' が未登録のため accessor を付与しませんでした。" >&2
    echo "     該当データソースの取込は理由付きで skip されます(鍵登録後に本スクリプトを再実行)。" >&2
  fi
done

# ── 2. コード同期(git 追跡ファイルのみを tar して転送) ────────────────────────
echo "-- コードを VM へ同期"
TARBALL="$(mktemp /tmp/ryza-daily.XXXXXX.tar.gz)"
trap 'rm -f "${TARBALL}"' EXIT
git -C "${ROOT}" archive --format=tar.gz -o "${TARBALL}" HEAD
gcloud compute scp "${TARBALL}" "${VM}:/tmp/ryza-src.tar.gz" --zone "${ZONE}" --project "${PROJECT}"

# ── 3. リモート・プロビジョニング(全ステップ冪等) ───────────────────────────
# ソース展開 → venv 同期(.[bot] を共有)→ マイグレーション適用 → env → systemd service+timer。
echo "-- VM 上でプロビジョニング(冪等)"
"${SSH[@]}" --command "sudo bash -s" <<REMOTE
set -euo pipefail
export HOME=/root
export PATH="/root/.local/bin:\$PATH"

# 3.1 ソース展開(deploy-bot.sh と同じ /opt/ryza を共有。冪等)。
#     uv.lock は git archive HEAD に含まれるため tar で更新される(A-12 F-13)。
install -d -o root -g root /opt/ryza
rm -rf /opt/ryza/src /opt/ryza/migrations /opt/ryza/pyproject.toml /opt/ryza/README.md /opt/ryza/config /opt/ryza/uv.lock
tar -xzf /tmp/ryza-src.tar.gz -C /opt/ryza

# 3.2 依存同期(A-12 F-13: `uv sync --locked` で CI と同じ解決に固定)。
#     旧世代 `uv pip install -e '.[bot]'` は pyproject.toml の `>=` を毎回最新解決し
#     CI と本番の依存が乖離する(Supply Chain の再現性劣化)。CI(.github/workflows/
#     ci.yml)と同じ `uv sync --locked --extra bot` に統一する。daily は torch を
#     積まない縮退モードで動くため extra は bot のみ(preprocess は入れない)。
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
cd /opt/ryza
if [ ! -f uv.lock ]; then
  echo "ERROR: /opt/ryza/uv.lock が無い。tar に含まれていない可能性あり(デプロイ資材の欠落)。" >&2
  exit 1
fi
uv sync --locked --extra bot

# 3.3 マイグレーション適用(冪等: schema_migrations で未適用のみ)。
RYZA_DATABASE_URL='${DATABASE_URL}' .venv/bin/python -m ryza.db.migrate

# 3.4 実行時 env(トークンは含めない。provider が Secret Manager から取得)。
install -d -m 0755 /etc/ryza
# provider(load_api_key)は Secret 'anthropic-api-key' を GCP_PROJECT のメタデータ SA + REST で取得。
cat > /etc/ryza/daily.env <<ENV
GCP_PROJECT=${PROJECT}
RYZA_DATABASE_URL=${DATABASE_URL}
ENV
chmod 0640 /etc/ryza/daily.env

# 3.5 systemd service(oneshot: 日次サイクルを1回実行して終了)。
cat > /etc/systemd/system/ryza-daily.service <<UNIT
[Unit]
Description=Ryza 日次サイクル (T-013)
After=network-online.target postgresql.service
Wants=network-online.target postgresql.service

[Service]
Type=oneshot
EnvironmentFile=/etc/ryza/daily.env
WorkingDirectory=/opt/ryza
ExecStart=/opt/ryza/.venv/bin/python -m ryza.jobs.daily
UNIT

# 3.6 systemd timer(JST 09:00 起動。取りこぼしは Persistent で次回起動時に補完)。
cat > /etc/systemd/system/ryza-daily.timer <<UNIT
[Unit]
Description=Ryza 日次サイクルを毎朝起動 (T-013)

[Timer]
OnCalendar=${DAILY_ONCALENDAR}
Persistent=true
Unit=ryza-daily.service

[Install]
WantedBy=timers.target
UNIT

# 3.7 反映(daemon-reload → timer を有効化・開始は冪等)。service 自体は timer が起動する。
systemctl daemon-reload
systemctl enable --now ryza-daily.timer
systemctl list-timers ryza-daily.timer --no-pager || true
REMOTE

echo "== 完了 =="
echo "タイマー確認: gcloud compute ssh ${VM} --zone ${ZONE} --command 'systemctl list-timers ryza-daily --no-pager'"
echo "ログ確認:     gcloud compute ssh ${VM} --zone ${ZONE} --command 'journalctl -u ryza-daily -n 50 --no-pager'"
echo "実スモーク(--dry-run / 実 API は Secret 登録後に設計リードが1回だけ手動実行)。"
