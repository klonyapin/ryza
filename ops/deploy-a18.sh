#!/usr/bin/env bash
#
# deploy-a18.sh — A-18 監査(規則⇔実装トレーサビリティ)を GCE VM の週次 systemd timer で常駐設置する。
#
# 冪等: 何度再実行してもよい。git 導入・監査用 clone 作成・ランナー/unit 設置だけを行う。
#
# 構成判断(定款第5〜6条・独立役員審査 2 回の指摘を反映):
#   - A-18 は VM(ryza-bot)上の独立 timer で走らせ、結果 embed は press.outbox 経由で #運営 へ届く
#     (--always-report で所見ゼロでも毎週1通 = ハートビート)。
#     当初の理由は「Cloud Run 版 ops-weekly には checkout も VM 内 DB も無い」だったが、
#     2026-08-04 に ops-weekly 自体が VM の systemd timer へ移った後も**この分離は維持する**:
#     週次から稼働コード(/opt/ryza)を対象に走らせ直すと、下記の監査 clone 分離が無意味になる。
#     ops-weekly 側は A18_REPO_PATH を設定しない(= ダイジェストの A-18 行はスキップのまま)。
#   - 監査対象は /opt/ryza(rsync コピー・.git なし)ではなく**監査専用 clone /opt/ryza-audit**。
#     実行のたびに GitHub origin/main を fetch する — デプロイ経路(rsync)から独立させ、
#     「稼働コードの改竄が監査対象まで汚染する」経路を断つ。
#   - **監査コード自体も監査 clone 側から実行**(PYTHONPATH=/opt/ryza-audit/src。venv は依存のみ提供)。
#     rsync でデプロイされた a18 実装の改変が監査を無害化する経路を断つ(独立役員懸念2)。
#   - GitHub トークンは Secret Manager('github-token')から実行時取得。ディスク・remote URL に
#     残さず、argv にも載せない(GIT_ASKPASS 経由 — ps/cmdline 露出を防ぐ。独立役員懸念3)。
#   - インフラ層の失敗(clone/fetch/DB)は OnFailure で #運営 へ通知し、ハートビートの沈黙を
#     多義的にしない(独立役員懸念4)。通知自体が失敗する場合は journal のみ(限界として明記)。
#
# 使い方: ./ops/deploy-a18.sh   (PROJECT/ZONE/VM/ONCALENDAR は env で上書き可)
set -euo pipefail

PROJECT="${PROJECT:-ryza-main}"
ZONE="${ZONE:-us-west1-a}"
VM="${VM:-ryza-bot}"
REPO="${REPO:-klonyapin/ryza}"
# 週次ジョブ ops-weekly(VM の ryza-ops-weekly.timer・月曜 01:00 UTC)の後、独立に走る。
A18_ONCALENDAR="${A18_ONCALENDAR:-Mon *-*-* 01:40:00 UTC}"
DATABASE_URL="${RYZA_DATABASE_URL:-postgresql://ryza:ryza@localhost:5432/ryza}"

SSH=(gcloud compute ssh "${VM}" --zone "${ZONE}" --project "${PROJECT}")

echo "== ryza-a18 deploy =="
gcloud config set project "${PROJECT}" >/dev/null

# ── 1. SA に github-token の Secret Accessor を付与(冪等) ──────────────────────
SA="$(gcloud compute instances describe "${VM}" --zone "${ZONE}" \
  --format 'value(serviceAccounts[0].email)')"
echo "-- Secret Accessor を SA に付与(冪等): ${SA}"
gcloud secrets add-iam-policy-binding github-token \
  --member "serviceAccount:${SA}" --role roles/secretmanager.secretAccessor \
  --format none

# ── 2. VM 上でプロビジョニング(冪等) ─────────────────────────────────────────
"${SSH[@]}" --command "sudo bash -s" <<PROVISION
set -euo pipefail

# git 導入(冪等)
command -v git >/dev/null || (apt-get update -qq && apt-get install -y -qq git)

# 旧 A-13 系ユニットの撤去(Issue #48 の改番。冪等 — 未設置なら何もしない)。
# 監査 clone は origin/main を毎回 reset するため、改番がマージされた瞬間に旧ランナーの
# 'python -m ryza.audit.a13' は ModuleNotFoundError で毎週失敗する。二重起動と偽の失敗
# 通知を防ぐため、新ユニットの設置前に旧ユニットを止めて消す。
for u in ryza-a13.timer ryza-a13.service ryza-a13-fail.service; do
  systemctl disable --now "\$u" 2>/dev/null || true
  rm -f "/etc/systemd/system/\$u"
done
# 失敗状態のまま残るとユニット削除後も systemctl --failed に出続けるため明示的に消す。
systemctl reset-failed ryza-a13.service 2>/dev/null || true
rm -f /opt/ryza-a13-run.sh /opt/ryza-a13-fail.sh

# 監査用ランナー(トークンは実行時に Secret Manager から取得し env + askpass で渡す)
cat > /opt/ryza-a18-run.sh <<'RUNNER'
#!/usr/bin/env bash
set -euo pipefail
AUDIT=/opt/ryza-audit
GIT_TOKEN="\$(/opt/ryza/.venv/bin/python -c 'import os; from ryza.secrets import access_secret; print(access_secret("github-token", project=os.environ["GCP_PROJECT"]))')"
export GIT_TOKEN
ASKPASS="\$(mktemp)"
trap 'rm -f "\${ASKPASS}"' EXIT
printf '#!/bin/sh\necho "\$GIT_TOKEN"\n' > "\${ASKPASS}"
chmod 700 "\${ASKPASS}"
export GIT_ASKPASS="\${ASKPASS}"
URL="https://x-access-token@github.com/${REPO}.git"
if [ ! -d "\${AUDIT}/.git" ]; then
  git clone --quiet "\${URL}" "\${AUDIT}"
  git -C "\${AUDIT}" remote set-url origin "https://github.com/${REPO}.git"
fi
# origin/main を毎回更新して監査対象を最新化
git -C "\${AUDIT}" fetch --quiet "\${URL}" "main:refs/remotes/origin/main"
git -C "\${AUDIT}" reset --hard --quiet origin/main
# 監査コードは監査 clone 側を使う(venv は依存のみ)
export PYTHONPATH="\${AUDIT}/src"
exec /opt/ryza/.venv/bin/python -m ryza.audit.a18 --repo "\${AUDIT}" --always-report
RUNNER
chmod 700 /opt/ryza-a18-run.sh

# 失敗通知(インフラ層の失敗も #運営 へ。これ自体の失敗は journal のみが限界)
cat > /opt/ryza-a18-fail.sh <<'FAILSH'
#!/usr/bin/env bash
exec /opt/ryza/.venv/bin/python - <<'PY'
from ryza.bot import outbox
from ryza.db.conn import connect
from ryza.provenance import start_run

with connect() as conn:
    r = start_run("audit.a18.failure", conn=conn)
    outbox.enqueue(
        conn, "ops",
        {"title": "A-18 実行失敗(インフラ層)",
         "description": "systemd ryza-a18.service が失敗しました。clone/fetch/DB/コード層の障害の可能性。"
                        "journalctl -u ryza-a18 を確認してください。",
         "color": 15158332},
        r.run_id, urgent=True,
    )
    r.finish("success")
    conn.commit()
PY
FAILSH
chmod 700 /opt/ryza-a18-fail.sh

# systemd units + timer
cat > /etc/systemd/system/ryza-a18-fail.service <<'FAILUNIT'
[Unit]
Description=Ryza A-18 失敗通知

[Service]
Type=oneshot
Environment=RYZA_DATABASE_URL=${DATABASE_URL}
ExecStart=/opt/ryza-a18-fail.sh
FAILUNIT

cat > /etc/systemd/system/ryza-a18.service <<'UNIT'
[Unit]
Description=Ryza A-18 監査(規則⇔実装トレーサビリティ・週次)
After=network-online.target postgresql.service
OnFailure=ryza-a18-fail.service

[Service]
Type=oneshot
Environment=RYZA_DATABASE_URL=${DATABASE_URL}
Environment=GCP_PROJECT=${PROJECT}
ExecStart=/opt/ryza-a18-run.sh
UNIT

cat > /etc/systemd/system/ryza-a18.timer <<'TIMER'
[Unit]
Description=Ryza A-18 監査タイマー(週次)

[Timer]
OnCalendar=${A18_ONCALENDAR}
Persistent=true

[Install]
WantedBy=timers.target
TIMER

systemctl daemon-reload
systemctl enable --now ryza-a18.timer
systemctl list-timers ryza-a18 --no-pager
PROVISION

echo "== 完了 =="
echo "手動実行:   gcloud compute ssh ${VM} --zone ${ZONE} --command 'sudo systemctl start ryza-a18.service'"
echo "ログ確認:   gcloud compute ssh ${VM} --zone ${ZONE} --command 'sudo journalctl -u ryza-a18 -n 30 --no-pager'"
