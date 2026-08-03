#!/usr/bin/env bash
#
# deploy-ops-weekly.sh — 週次運用ジョブ ops-weekly(T-004)を GCE VM の systemd timer で常駐設置する。
#
# 冪等: 何度再実行してもよい。コード同期 + ランナー/unit の再設置 + 旧 Cloud Run 経路の撤去だけを行う。
#
# ── 構成判断(2026-08-04 変更。旧構成: Cloud Run Job + Cloud Scheduler)─────────────
#   週次ダイジェストが載せる統制のうち「決議の批判経由」(`BOARDROOM_AUDIT`)は
#   `governance.minute_resolutions` を読む。DB は VM 内 PostgreSQL(localhost)にあり
#   Cloud Run からは届かないため、Cloud Run 版では当該行が恒久的に
#   「スキップ(BOARDROOM_AUDIT 未配線)」になっていた(決議精緻化審査 2026-08-03 懸念4:
#   残る表出先はダッシュボード = 確認を外す当人であり、独立した検出点になっていない)。
#
#   採らなかった案と理由:
#     (a) Cloud Run Job の env に BOARDROOM_AUDIT=1 を足すだけ
#         → DB に届かないので毎週「失敗: 接続不能」に変わるだけ。鳴りっぱなしの行は
#           読まれなくなるため、統制としては未配線と同じかそれ以下になる。
#           この案には Cloud SQL 移行が前提になる(30-press §1 と同じ将来形)。
#     (b) 週次は Cloud Run のまま、監査だけ VM 側の別 timer で回す
#         → ダイジェスト本体の当該行は「スキップ」のまま残り、報告先が2か所に割れる。
#           懸念4 が問題にしたのは「週次ダイジェストに出ないこと」なので的を外す。
#     (c) 本スクリプトの採用案: **週次ジョブ本体を VM(ryza-bot)へ移す**
#         → DB もリマインダー発火先の GitHub も同じプロセスから届き、行が実値になる。
#           DB 依存ジョブを VM の systemd timer で回すのは deploy-daily.sh(T-013)で
#           既に採った構成判断であり、Cloud SQL 移行までの当面の姿として一貫する。
#
#   旧 Cloud Run 経路は**撤去する**(残置しない)。`post_digest` は当週マーカーで冪等なため、
#   DB を持たない Cloud Run 側が先に走ると「スキップ」行のダイジェストで当週分が確定し、
#   後続の VM 側は投稿をスキップする。二重経路は本変更を静かに無効化する。
#
#   `A18_REPO_PATH` は**意図的に設定しない**。A-18 監査は deploy-a18.sh が用意する
#   監査専用 clone(/opt/ryza-audit・origin/main を毎回 fetch)から独立に走る設計であり、
#   rsync でデプロイされた稼働コード(/opt/ryza)から実行すると「デプロイ経路の改変が
#   監査を無害化する」経路を再び開けてしまう(deploy-a18.sh の構成判断)。
#
# 前提(設計リードが事前に用意 — スクリプトはここを検証して中断する):
#   - deploy-bot.sh 実行済み(VM ryza-bot・PostgreSQL・/opt/ryza・venv が存在)
#   - deploy-daily.sh または deploy-bot.sh でマイグレーション適用済み
#     (本スクリプトは schema を触らない。governance スキーマの存在だけ検証する)
#   - gcloud 認証済み・API 有効化済み(compute, secretmanager, bigquery)
#   - Secret Manager に `github-token`(fine-grained PAT)を登録済み
#     └ 未登録ならこのスクリプトは中断する(値の投入はユーザー作業)。
#
# 使い方:
#   GITHUB_REPO=owner/name ./ops/deploy-ops-weekly.sh
#   DRY_RUN_JOB=1 GITHUB_REPO=owner/name ./ops/deploy-ops-weekly.sh   # ジョブ側も DRY_RUN で設置
#
# デプロイ後の確認(VM 上):
#   gcloud compute ssh ryza-bot --zone us-west1-a --command 'systemctl list-timers ryza-ops-weekly --no-pager'
#   gcloud compute ssh ryza-bot --zone us-west1-a --command 'sudo systemctl start ryza-ops-weekly.service'
#   gcloud compute ssh ryza-bot --zone us-west1-a --command 'journalctl -u ryza-ops-weekly -n 50 --no-pager'
#
set -euo pipefail

# ── 設定(env で上書き可・ハードコードしない) ──────────────────────────────────
PROJECT="${PROJECT:-ryza-main}"
ZONE="${ZONE:-us-west1-a}"
REGION="${REGION:-us-west1}"
VM="${VM:-ryza-bot}"
SECRET="${SECRET:-github-token}"
GITHUB_REPO="${GITHUB_REPO:?GITHUB_REPO=owner/name を指定してください}"
DRY_RUN_JOB="${DRY_RUN_JOB:-0}"      # 1 ならジョブ本体を DRY_RUN で起動する env にする

# タイマー起動時刻。旧 Cloud Scheduler と同一(UTC 月曜 01:00 = JST 月曜 10:00)。
# A-18(deploy-a18.sh・Mon 01:40 UTC)より前に走り、両者は独立に報告する。
WEEKLY_ONCALENDAR="${WEEKLY_ONCALENDAR:-Mon *-*-* 01:00:00 UTC}"

DATABASE_URL="${RYZA_DATABASE_URL:-postgresql://ryza:ryza@localhost:5432/ryza}"

# 撤去対象(旧構成)。既に無ければ何もしない。
OLD_JOB="${OLD_JOB:-ops-weekly}"
OLD_SCHEDULER_JOB="${OLD_SCHEDULER_JOB:-ops-weekly-trigger}"

# guard_git_state に渡す許可 origin(env で差し替えない — 差し替えられる時点で照合が
# 統制として成立しないため。deploy-dashboard.sh と同じ流儀)。
ALLOWED_ORIGINS=(
  "https://github.com/klonyapin/ryza.git"
  "git@github.com:klonyapin/ryza.git"
)

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH=(gcloud compute ssh "${VM}" --zone "${ZONE}" --project "${PROJECT}")

# 統制ゲートは ops/lib/deploy-guards.sh の関数(CI でテスト — tests/ops/test_deploy_guards.py)。
# 呼び出しに `|| exit 1` を付け忘れるとゲートが無効化されるので注意。
# shellcheck source=lib/deploy-guards.sh
. "${ROOT}/ops/lib/deploy-guards.sh"

echo "== ryza-ops-weekly deploy (GCE VM + systemd timer) =="
echo "project=${PROJECT} zone=${ZONE} vm=${VM} oncalendar='${WEEKLY_ONCALENDAR}'"

gcloud config set project "${PROJECT}" >/dev/null

# ── 0. 承認済みコード一致の検証(定款第5条)────────────────────────────────────
# 本スクリプトは VM へ**ソースを配る**(旧構成の Cloud Build 相当)。未コミット変更や
# 未 push のコミットが週次統制の実行コードになる経路をここで塞ぐ。
echo "-- 稼働コードの検証: 作業ツリー clean かつ HEAD == origin/main"
CODE_VERSION="$(guard_git_state "${ROOT}" "${ALLOWED_ORIGINS[@]}")" || exit 1
echo "-- code_version=${CODE_VERSION}(origin/main と一致)"

# ── 1. VM の存在確認(無ければ中断 — deploy-bot.sh を先に) ─────────────────────
if ! gcloud compute instances describe "${VM}" --zone "${ZONE}" >/dev/null 2>&1; then
  echo "ERROR: VM '${VM}' が存在しません。先に ops/deploy-bot.sh を実行してください。" >&2
  exit 1
fi

# ── 2. Secret の存在確認 + IAM(冪等) ─────────────────────────────────────────
if ! gcloud secrets describe "${SECRET}" >/dev/null 2>&1; then
  echo "ERROR: Secret '${SECRET}' が未登録です。先に fine-grained PAT を登録してください:" >&2
  echo "  printf %s '<PAT>' | gcloud secrets create ${SECRET} --data-file=- --replication-policy=automatic" >&2
  exit 1
fi
RUNTIME_SA="$(gcloud compute instances describe "${VM}" --zone "${ZONE}" \
  --format 'value(serviceAccounts[0].email)')"
echo "-- VM の SA=${RUNTIME_SA}"

echo "-- Secret Accessor を SA に付与(冪等)"
gcloud secrets add-iam-policy-binding "${SECRET}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor" >/dev/null

# bq_table_missing 条件(billing-export-verify 等)が BigQuery を読む。
echo "-- BigQuery Data Viewer を SA に付与(冪等)"
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/bigquery.dataViewer" \
  --condition=None >/dev/null

# ── 3. コード同期(git 追跡ファイルのみを tar して転送) ────────────────────────
echo "-- コードを VM へ同期"
TARBALL="$(mktemp /tmp/ryza-ops-weekly.XXXXXX.tar.gz)"
trap 'rm -f "${TARBALL}"' EXIT
git -C "${ROOT}" archive --format=tar.gz -o "${TARBALL}" HEAD
gcloud compute scp "${TARBALL}" "${VM}:/tmp/ryza-src.tar.gz" --zone "${ZONE}" --project "${PROJECT}"

# ── 4. リモート・プロビジョニング(全ステップ冪等) ───────────────────────────
echo "-- VM 上でプロビジョニング(冪等)"
"${SSH[@]}" --command "sudo bash -s" <<REMOTE
set -euo pipefail
export HOME=/root
export PATH="/root/.local/bin:\$PATH"

# 4.1 ソース展開(deploy-bot.sh / deploy-daily.sh と同じ /opt/ryza を共有。冪等)。
install -d -o root -g root /opt/ryza
rm -rf /opt/ryza/src /opt/ryza/migrations /opt/ryza/pyproject.toml /opt/ryza/README.md /opt/ryza/config
tar -xzf /tmp/ryza-src.tar.gz -C /opt/ryza

# 4.2 venv(無ければ作成)+ 依存同期。
# .[ops] は bq_table_missing の google-cloud-bigquery(遅延インポート)。
# .[bot] を併せて入れるのは同じ venv を Bot / daily と共有しているため(取り外さない)。
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
cd /opt/ryza
[ -d .venv ] || uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[bot,ops]'

# 4.3 前提スキーマの存在検証(本スクリプトは schema を触らない)。
# 形骸化監査は governance.minute_resolutions を読む。未適用のままデプロイすると
# 週次ダイジェストの当該行が毎週「失敗: UndefinedTable」になり、鳴りっぱなしの行は
# 読まれなくなる = 未配線と同じになる。デプロイ時に落とす。
RYZA_DATABASE_URL='${DATABASE_URL}' .venv/bin/python - <<'PYCHECK'
import sys

from ryza.db.conn import connect

with connect() as conn, conn.cursor() as cur:
    cur.execute("SELECT to_regclass('governance.minute_resolutions')")
    if cur.fetchone()[0] is None:
        sys.exit(
            "ERROR: governance.minute_resolutions が無い。"
            "先に ops/deploy-daily.sh(または deploy-bot.sh)でマイグレーションを適用すること。"
        )
print("OK: governance.minute_resolutions を確認")
PYCHECK

# 4.4 実行時 env(トークンは含めない。ランナーが Secret Manager から取得)。
install -d -m 0755 /etc/ryza
# BOARDROOM_AUDIT=1: 決議の形骸化監査(05-governance §6-5 の趣旨に連なる新設統制)。
#   DB へ届く実行環境でのみ設定する — その条件を満たすのがこの VM 経路である。
# A18_REPO_PATH は**設定しない**(理由は本ファイル冒頭の構成判断)。A-18 は
#   ryza-a18.timer が監査専用 clone から独立に実行し、結果は press.outbox → #運営 へ出る。
cat > /etc/ryza/ops-weekly.env <<ENV
GCP_PROJECT=${PROJECT}
RYZA_DATABASE_URL=${DATABASE_URL}
GITHUB_REPO=${GITHUB_REPO}
BOARDROOM_AUDIT=1
ENV
if [ "${DRY_RUN_JOB}" = "1" ]; then
  echo "DRY_RUN=1" >> /etc/ryza/ops-weekly.env
fi
chmod 0640 /etc/ryza/ops-weekly.env

# 4.5 ランナー(GitHub トークンを実行時に Secret Manager から取得する)。
# トークンはディスクにも argv にも置かない — export した env のみで渡す
# (deploy-a18.sh と同じ流儀。ps/cmdline 露出を防ぐ)。
cat > /opt/ryza-ops-weekly-run.sh <<'RUNNER'
#!/usr/bin/env bash
set -euo pipefail
GITHUB_TOKEN="\$(/opt/ryza/.venv/bin/python -c 'import os; from ryza.secrets import access_secret; print(access_secret("${SECRET}", project=os.environ["GCP_PROJECT"]))')"
export GITHUB_TOKEN
cd /opt/ryza
exec /opt/ryza/.venv/bin/python -m ryza.ops.weekly
RUNNER
chmod 700 /opt/ryza-ops-weekly-run.sh

# 4.6 失敗通知(インフラ層の失敗も #運営 へ。ダイジェストの沈黙を多義的にしない)。
# 週次ジョブが落ちるとダイジェスト自体が投稿されず、「今週は何も無かった」と
# 区別できなくなる。これ自体の失敗は journal のみが限界(deploy-a18.sh と同じ)。
cat > /opt/ryza-ops-weekly-fail.sh <<'FAILSH'
#!/usr/bin/env bash
exec /opt/ryza/.venv/bin/python - <<'PY'
from ryza.bot import outbox
from ryza.db.conn import connect
from ryza.provenance import start_run

with connect() as conn:
    r = start_run("ops.weekly.failure", conn=conn)
    outbox.enqueue(
        conn, "ops",
        {"title": "週次ジョブ ops-weekly 実行失敗",
         "description": "systemd ryza-ops-weekly.service が失敗しました。"
                        "週次ダイジェスト(リマインダー発火・決議の批判経由)は今週投稿されていません。"
                        "journalctl -u ryza-ops-weekly を確認してください。",
         "color": 15158332},
        r.run_id, urgent=True,
    )
    r.finish("success")
    conn.commit()
PY
FAILSH
chmod 700 /opt/ryza-ops-weekly-fail.sh

# 4.7 systemd units + timer。
cat > /etc/systemd/system/ryza-ops-weekly-fail.service <<'FAILUNIT'
[Unit]
Description=Ryza 週次ジョブ 失敗通知

[Service]
Type=oneshot
EnvironmentFile=/etc/ryza/ops-weekly.env
WorkingDirectory=/opt/ryza
ExecStart=/opt/ryza-ops-weekly-fail.sh
FAILUNIT

cat > /etc/systemd/system/ryza-ops-weekly.service <<'UNIT'
[Unit]
Description=Ryza 週次運用ジョブ ops-weekly (T-004)
After=network-online.target postgresql.service
Wants=network-online.target postgresql.service
OnFailure=ryza-ops-weekly-fail.service

[Service]
Type=oneshot
EnvironmentFile=/etc/ryza/ops-weekly.env
WorkingDirectory=/opt/ryza
ExecStart=/opt/ryza-ops-weekly-run.sh
UNIT

cat > /etc/systemd/system/ryza-ops-weekly.timer <<'TIMER'
[Unit]
Description=Ryza 週次運用ジョブを毎週起動 (T-004)

[Timer]
OnCalendar=${WEEKLY_ONCALENDAR}
Persistent=true
Unit=ryza-ops-weekly.service

[Install]
WantedBy=timers.target
TIMER

systemctl daemon-reload
systemctl enable --now ryza-ops-weekly.timer
systemctl list-timers ryza-ops-weekly --no-pager || true
REMOTE

# ── 5. 旧 Cloud Run 経路の撤去(冪等 — 既に無ければ何もしない) ─────────────────
# 残置すると DB を持たない Cloud Run 側が先に走り、「スキップ」行のダイジェストで
# 当週分が確定して VM 側の投稿がスキップされる(post_digest は当週マーカーで冪等)。
if gcloud scheduler jobs describe "${OLD_SCHEDULER_JOB}" --location="${REGION}" >/dev/null 2>&1; then
  echo "-- 旧 Cloud Scheduler '${OLD_SCHEDULER_JOB}' を削除(VM の timer に移行済み)"
  gcloud scheduler jobs delete "${OLD_SCHEDULER_JOB}" --location="${REGION}" --quiet
else
  echo "-- 旧 Cloud Scheduler '${OLD_SCHEDULER_JOB}' は無し"
fi
if gcloud run jobs describe "${OLD_JOB}" --region="${REGION}" >/dev/null 2>&1; then
  echo "-- 旧 Cloud Run Job '${OLD_JOB}' を削除(手動実行で DB 無し版が走る余地を残さない)"
  gcloud run jobs delete "${OLD_JOB}" --region="${REGION}" --quiet
else
  echo "-- 旧 Cloud Run Job '${OLD_JOB}' は無し"
fi

echo "== 完了 =="
echo "タイマー確認: gcloud compute ssh ${VM} --zone ${ZONE} --command 'systemctl list-timers ryza-ops-weekly --no-pager'"
echo "手動実行:     gcloud compute ssh ${VM} --zone ${ZONE} --command 'sudo systemctl start ryza-ops-weekly.service'"
echo "ログ確認:     gcloud compute ssh ${VM} --zone ${ZONE} --command 'sudo journalctl -u ryza-ops-weekly -n 50 --no-pager'"
