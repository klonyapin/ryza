#!/usr/bin/env bash
#
# deploy-dashboard.sh — 運用ダッシュボードを Cloud Run + IAP で公開する(2026-08-03 代表指示)。
#
# 冪等: 何度再実行してもよい。全ステップが「無ければ作る・有れば流用/更新」で書かれている。
#
# 構成判断:
#   - **認証は IAP に全面委譲**。アプリ内に認証コードは置かず、許可リスト
#     (roles/iap.httpsResourceAccessor)に載る Google アカウントだけがアクセスできる。
#   - **DB は VM(ryza-bot)内 PostgreSQL** に Cloud Run の Direct VPC egress で接続する
#     (VPC コネクタ不要。default VPC の ${REGION} サブネットに egress し、VM の内部 IP へ)。
#   - **既存サービスを壊さない**: bot/daily は postgresql://ryza:ryza@localhost のまま。
#     role `ryza` のパスワードは変更しない(変更すると localhost の scram 認証で bot/daily が
#     壊れる)。代わりに専用ロール `ryza_dashboard`(IN ROLE ryza・INHERIT=ryza の全権限を
#     継承。役員室の追記も可)を強パスワードで新設し、pg_hba は「VPC サブネットからは
#     ryza_dashboard のみ scram、localhost は現状維持」とする。
#   - コスト: min-instances=0 / max-instances=1(コールドスタート数十秒は許容 — README)。
#
# 前提(設計リードが事前に用意 — スクリプトは検証して中断する):
#   - deploy-bot.sh 実行済み(VM ryza-bot・PostgreSQL・DB ryza が存在)
#   - gcloud 認証済み。IAP の有効化はプロジェクトの OAuth 同意画面の構成が必要な場合がある
#     (エラーになったら console で 同意画面(Internal 相当)を1回だけ設定 — 手順は実行ログに表示)
#
# 使い方:
#   ./ops/deploy-dashboard.sh
#   PROJECT=... REGION=... VM=... DASHBOARD_USER=... ./ops/deploy-dashboard.sh
#
set -euo pipefail

# ── 設定(env で上書き可・ハードコードしない) ──────────────────────────────────
PROJECT="${PROJECT:-ryza-main}"
REGION="${REGION:-us-west1}"
ZONE="${ZONE:-us-west1-a}"
VM="${VM:-ryza-bot}"
SERVICE="${SERVICE:-ryza-dashboard}"
NETWORK="${NETWORK:-default}"
SUBNET="${SUBNET:-default}"
DASHBOARD_USER="${DASHBOARD_USER:-mileage_embassy_9x@icloud.com}" # Google アカウントであること
DB_PASSWORD_SECRET="${DB_PASSWORD_SECRET:-ryza-db-password}"       # ryza_dashboard ロールのパスワード
DB_URL_SECRET="${DB_URL_SECRET:-ryza-dashboard-db-url}"            # 完全な接続 URL(env 注入用)
AR_REPO="${AR_REPO:-ryza}"
DB_ROLE="ryza_dashboard"
PGVER="${PGVER:-17}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH=(gcloud compute ssh "${VM}" --zone "${ZONE}" --project "${PROJECT}")
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/dashboard:latest"

echo "== ryza-dashboard deploy (Cloud Run + IAP) =="
echo "project=${PROJECT} region=${REGION} vm=${VM} service=${SERVICE} user=${DASHBOARD_USER}"

gcloud config set project "${PROJECT}" >/dev/null

# ── 0. API 有効化(冪等)と VM の存在確認 ───────────────────────────────────────
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  secretmanager.googleapis.com iap.googleapis.com compute.googleapis.com \
  artifactregistry.googleapis.com >/dev/null

if ! gcloud compute instances describe "${VM}" --zone "${ZONE}" >/dev/null 2>&1; then
  echo "ERROR: VM '${VM}' が存在しません。先に ops/deploy-bot.sh を実行してください。" >&2
  exit 1
fi

# ── 1. DB パスワード Secret(無ければ生成して作成。hex なので URL セーフ) ─────────
if ! gcloud secrets describe "${DB_PASSWORD_SECRET}" >/dev/null 2>&1; then
  echo "-- Secret '${DB_PASSWORD_SECRET}' を新規作成(openssl rand -hex 24)"
  openssl rand -hex 24 | tr -d '\n' | gcloud secrets create "${DB_PASSWORD_SECRET}" \
    --data-file=- --replication-policy=automatic >/dev/null
fi
DBPW="$(gcloud secrets versions access latest --secret "${DB_PASSWORD_SECRET}")"

# ── 2. ネットワーク情報(VM 内部 IP・サブネット CIDR) ───────────────────────────
INTERNAL_IP="$(gcloud compute instances describe "${VM}" --zone "${ZONE}" \
  --format='value(networkInterfaces[0].networkIP)')"
SUBNET_CIDR="$(gcloud compute networks subnets describe "${SUBNET}" --region "${REGION}" \
  --format='value(ipCidrRange)')"
echo "-- VM 内部 IP=${INTERNAL_IP} / サブネット CIDR=${SUBNET_CIDR}"

# ── 3. VM 側 PostgreSQL 設定(冪等。localhost 接続の bot/daily は変更しない) ─────
echo "-- VM 上の PostgreSQL を VPC サブネットからの接続に対応させる(冪等)"
"${SSH[@]}" --command "sudo bash -s" <<REMOTE
set -euo pipefail
CONF_DIR="/etc/postgresql/${PGVER}/main"
CONF_D="\${CONF_DIR}/conf.d"
HBA="\${CONF_DIR}/pg_hba.conf"

# 3.1 listen_addresses に内部 IP を追加(conf.d 経由・postgresql.conf 本体は触らない)。
#     変更時のみ restart(listen_addresses は reload では反映されないため)。
install -d "\${CONF_D}"
DESIRED="listen_addresses = 'localhost,${INTERNAL_IP}'  # ryza-dashboard (deploy-dashboard.sh)"
CUR="\$(cat "\${CONF_D}/20-ryza-dashboard.conf" 2>/dev/null || true)"
if [ "\${CUR}" != "\${DESIRED}" ]; then
  printf '%s\n' "\${DESIRED}" > "\${CONF_D}/20-ryza-dashboard.conf"
  systemctl restart postgresql
  echo "listen_addresses を更新して PostgreSQL を再起動した(bot/daily は再接続で復帰)"
else
  echo "listen_addresses は設定済み"
fi

# 3.2 専用ロール(IN ROLE ryza・INHERIT — ryza の全権限を継承。役員室の追記も可)。
#     role 'ryza' 自体のパスワードは変更しない(localhost の bot/daily を壊さない)。
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_ROLE}'" | grep -q 1 \
  || sudo -u postgres psql -c "CREATE ROLE ${DB_ROLE} LOGIN INHERIT IN ROLE ryza"
sudo -u postgres psql -c "ALTER ROLE ${DB_ROLE} LOGIN PASSWORD '${DBPW}'" >/dev/null

# 3.3 pg_hba: VPC サブネットからは ${DB_ROLE} のみ scram。localhost 行は現状維持。
LINE="host ryza ${DB_ROLE} ${SUBNET_CIDR} scram-sha-256"
grep -qF "\${LINE}" "\${HBA}" || { printf '%s\n' "\${LINE}" >> "\${HBA}"; systemctl reload postgresql; }
echo "pg_hba 設定済み: \${LINE}"
REMOTE

# ── 4. ファイアウォール(サブネット内→VM:5432 のみ。0.0.0.0/0 は開けない) ────────
if ! gcloud compute firewall-rules describe ryza-allow-dashboard-db >/dev/null 2>&1; then
  gcloud compute firewall-rules create ryza-allow-dashboard-db \
    --network "${NETWORK}" --direction INGRESS --allow "tcp:5432" \
    --source-ranges "${SUBNET_CIDR}" --target-tags ryza-db \
    --description "Cloud Run (Direct VPC egress) -> VM PostgreSQL (deploy-dashboard.sh)" >/dev/null
fi
gcloud compute instances add-tags "${VM}" --zone "${ZONE}" --tags ryza-db >/dev/null

# ── 5. 接続 URL Secret(値が変わったときだけ新版を積む) ─────────────────────────
DB_URL="postgresql://${DB_ROLE}:${DBPW}@${INTERNAL_IP}:5432/ryza"
CURRENT_URL="$(gcloud secrets versions access latest --secret "${DB_URL_SECRET}" 2>/dev/null || true)"
if ! gcloud secrets describe "${DB_URL_SECRET}" >/dev/null 2>&1; then
  printf %s "${DB_URL}" | gcloud secrets create "${DB_URL_SECRET}" \
    --data-file=- --replication-policy=automatic >/dev/null
elif [ "${CURRENT_URL}" != "${DB_URL}" ]; then
  printf %s "${DB_URL}" | gcloud secrets versions add "${DB_URL_SECRET}" --data-file=- >/dev/null
fi

# Cloud Run 実行 SA(既定 compute SA)に Secret の読み取りを付与(冪等)。
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
RUNTIME_SA="${RUNTIME_SA:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"
gcloud secrets add-iam-policy-binding "${DB_URL_SECRET}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor" >/dev/null

# ── 6. イメージビルド(Cloud Build。コンテキストはリポジトリルート) ──────────────
gcloud artifacts repositories describe "${AR_REPO}" --location "${REGION}" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "${AR_REPO}" --location "${REGION}" \
       --repository-format docker --description "Ryza images" >/dev/null
echo "-- Cloud Build でイメージをビルド: ${IMAGE}"
CB="$(mktemp /tmp/ryza-dashboard-cb.XXXXXX.yaml)"
trap 'rm -f "${CB}"' EXIT
cat > "${CB}" <<YAML
steps:
  - name: gcr.io/cloud-builders/docker
    args: [build, -f, dashboard/Dockerfile, -t, "${IMAGE}", .]
images: ["${IMAGE}"]
YAML
gcloud builds submit --config "${CB}" "${ROOT}" >/dev/null

# ── 7. Cloud Run デプロイ(Direct VPC egress・非公開・min 0/max 1) ───────────────
echo "-- Cloud Run へデプロイ: ${SERVICE}"
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --no-allow-unauthenticated \
  --min-instances 0 --max-instances 1 \
  --memory 1Gi --cpu 1 \
  --port 8080 \
  --network "${NETWORK}" --subnet "${SUBNET}" \
  --vpc-egress private-ranges-only \
  --set-env-vars "GCP_PROJECT=${PROJECT}" \
  --set-secrets "RYZA_DATABASE_URL=${DB_URL_SECRET}:latest" \
  --quiet >/dev/null

# ── 8. IAP 有効化+許可リスト(冪等) ──────────────────────────────────────────
echo "-- IAP を有効化(失敗したら console で OAuth 同意画面を1回設定してから再実行)"
if ! gcloud beta run services update "${SERVICE}" --region "${REGION}" --iap --quiet; then
  cat >&2 <<'NOTE'
ERROR: IAP の有効化に失敗。多くの場合 OAuth 同意画面が未構成のため。console で
  APIs & Services → OAuth consent screen を User Type=External(公開せず・自分のみ)
  または組織があれば Internal で1回だけ構成し、本スクリプトを再実行すること。
NOTE
  exit 1
fi
gcloud beta iap web add-iam-policy-binding \
  --project "${PROJECT}" \
  --resource-type=cloud-run --service="${SERVICE}" --region="${REGION}" \
  --member="user:${DASHBOARD_USER}" \
  --role="roles/iap.httpsResourceAccessor" >/dev/null

URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" --format='value(status.url)')"
echo "== 完了 =="
echo "アクセス URL: ${URL}(${DASHBOARD_USER} の Google アカウントでのみ閲覧可)"
echo "初回はコールドスタートで数十秒かかる(min-instances=0 のコスト最適化 — README 参照)"
echo "DB 接続: Direct VPC egress → ${INTERNAL_IP}:5432(role=${DB_ROLE}。bot/daily の localhost 接続は不変)"
