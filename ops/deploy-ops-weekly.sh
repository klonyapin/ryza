#!/usr/bin/env bash
#
# deploy-ops-weekly.sh — 週次運用ジョブ ops-weekly(T-004)を GCP にデプロイする。
#
# 冪等: 何度再実行してもよい。既存リソースは describe で確認し、無ければ作成、
# あれば更新(deploy/update)する。
#
# 前提(設計リードが事前に用意):
#   - gcloud 認証済み・必要 API 有効化済み(run, cloudbuild, artifactregistry,
#     cloudscheduler, secretmanager, bigquery)
#   - Secret Manager に `github-token`(fine-grained PAT)を登録済み
#     └ 未登録ならこのスクリプトは中断する(値の投入はユーザー作業)。
#
# 使い方:
#   GITHUB_REPO=owner/name ./ops/deploy-ops-weekly.sh
#   DRY_RUN_JOB=1 GITHUB_REPO=owner/name ./ops/deploy-ops-weekly.sh   # ジョブ側も DRY_RUN で作成
#
set -euo pipefail

# ── 設定 ─────────────────────────────────────────────────────────────────────
PROJECT="${PROJECT:-ryza-main}"
REGION="${REGION:-us-west1}"
REPO="${REPO:-ryza}"                 # Artifact Registry リポジトリ
JOB="${JOB:-ops-weekly}"             # Cloud Run Job 名
SCHEDULER_JOB="${SCHEDULER_JOB:-ops-weekly-trigger}"
SCHEDULE="${SCHEDULE:-0 1 * * 1}"    # UTC 月曜01:00 = JST 月曜10:00
SECRET="${SECRET:-github-token}"
GITHUB_REPO="${GITHUB_REPO:?GITHUB_REPO=owner/name を指定してください}"
DRY_RUN_JOB="${DRY_RUN_JOB:-0}"      # 1 ならジョブ本体を DRY_RUN で起動する env にする

IMAGE_HOST="${REGION}-docker.pkg.dev"
IMAGE_BASE="${IMAGE_HOST}/${PROJECT}/${REPO}/${JOB}"
TAG="$(date -u +%Y%m%d%H%M%S)"
IMAGE="${IMAGE_BASE}:${TAG}"
IMAGE_LATEST="${IMAGE_BASE}:latest"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== ops-weekly deploy =="
echo "project=${PROJECT} region=${REGION} repo=${REPO} job=${JOB}"
echo "image=${IMAGE}"

gcloud config set project "${PROJECT}" >/dev/null

# ── 0. Secret の存在確認(未登録なら中断) ────────────────────────────────────
if ! gcloud secrets describe "${SECRET}" >/dev/null 2>&1; then
  echo "ERROR: Secret '${SECRET}' が未登録です。先に fine-grained PAT を登録してください:" >&2
  echo "  printf %s '<PAT>' | gcloud secrets create ${SECRET} --data-file=- --replication-policy=automatic" >&2
  exit 1
fi

# 実行/スケジューラ用サービスアカウント(デフォルトの Compute SA を使う)。
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
RUNTIME_SA="${RUNTIME_SA:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"
echo "runtime/scheduler SA=${RUNTIME_SA}"

# ── 1. Artifact Registry リポジトリ(無ければ作成) ───────────────────────────
if ! gcloud artifacts repositories describe "${REPO}" --location="${REGION}" >/dev/null 2>&1; then
  echo "-- Artifact Registry '${REPO}' を作成"
  gcloud artifacts repositories create "${REPO}" \
    --repository-format=docker --location="${REGION}" \
    --description="Ryza container images"
else
  echo "-- Artifact Registry '${REPO}' は既存"
fi

# ── 2. イメージのビルドと push(Cloud Build) ─────────────────────────────────
echo "-- イメージをビルド: ${IMAGE}"
# gcloud builds submit は --config に stdin(-)を受け付けないため一時ファイルを使う
CB_CONFIG=$(mktemp /tmp/cloudbuild.XXXXXX.yaml)
trap 'rm -f "${CB_CONFIG}"' EXIT
cat > "${CB_CONFIG}" <<EOF
steps:
  - name: gcr.io/cloud-builders/docker
    args: ["build", "-f", "docker/ops/Dockerfile", "-t", "${IMAGE}", "-t", "${IMAGE_LATEST}", "."]
images:
  - "${IMAGE}"
  - "${IMAGE_LATEST}"
EOF
gcloud builds submit "${ROOT}" --config="${CB_CONFIG}"

# ── 3. Secret へのアクセス権(SA に Secret Accessor) ─────────────────────────
echo "-- Secret Accessor を SA に付与(冪等)"
gcloud secrets add-iam-policy-binding "${SECRET}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor" >/dev/null

# ── 4. BigQuery データ閲覧ロール(bq_table_missing 用) ───────────────────────
echo "-- BigQuery Data Viewer を SA に付与(冪等)"
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/bigquery.dataViewer" \
  --condition=None >/dev/null

# ── 5. Cloud Run Job(deploy は create-or-update で冪等) ─────────────────────
JOB_ENV="GITHUB_REPO=${GITHUB_REPO}"
if [[ "${DRY_RUN_JOB}" == "1" ]]; then
  JOB_ENV="${JOB_ENV},DRY_RUN=1"
fi
echo "-- Cloud Run Job '${JOB}' を deploy"
gcloud run jobs deploy "${JOB}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --service-account="${RUNTIME_SA}" \
  --set-secrets="GITHUB_TOKEN=${SECRET}:latest" \
  --set-env-vars="${JOB_ENV}" \
  --max-retries=1 \
  --task-timeout=600

# ── 6. Cloud Scheduler(OAuth で Cloud Run Job を起動) ───────────────────────
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/run.invoker" \
  --condition=None >/dev/null

RUN_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/${JOB}:run"
if gcloud scheduler jobs describe "${SCHEDULER_JOB}" --location="${REGION}" >/dev/null 2>&1; then
  echo "-- Cloud Scheduler '${SCHEDULER_JOB}' を更新"
  SCHED_CMD=update
else
  echo "-- Cloud Scheduler '${SCHEDULER_JOB}' を作成"
  SCHED_CMD=create
fi
gcloud scheduler jobs "${SCHED_CMD}" http "${SCHEDULER_JOB}" \
  --location="${REGION}" \
  --schedule="${SCHEDULE}" \
  --time-zone="Etc/UTC" \
  --uri="${RUN_URI}" \
  --http-method=POST \
  --oauth-service-account-email="${RUNTIME_SA}"

echo "== 完了 =="
echo "手動実行で検証: gcloud run jobs execute ${JOB} --region ${REGION}"
