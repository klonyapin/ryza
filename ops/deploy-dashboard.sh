#!/usr/bin/env bash
#
# deploy-dashboard.sh — 運用ダッシュボードを Cloud Run + IAP で公開する(2026-08-03 代表指示)。
#
# 冪等: 何度再実行してもよい。全ステップが「無ければ作る・有れば流用/更新」で書かれている。
#
# 本スクリプトは保護領域 deploy_path(定款第5条)。独立役員審査(2026-08-03・
# docs/reviews/dashboard-deploy-independent-review.md)の差し戻しを受けて是正済み。
#
# 構成判断:
#   - **稼働コード = 承認済み main**(定款第5条)。作業ツリーが clean かつ
#     HEAD == origin/main でなければ**何もせず中断**する(重大-1)。イメージタグは
#     コミット SHA(:latest は使わない)で、Cloud Run にも code_version を
#     ラベルと env の両方で記録する(不変原則3)。
#     恒久策(GitHub main 連携の Cloud Build トリガー化)は Phase 5 に繰延し
#     ops/reminders.yaml `dashboard-cloudbuild-trigger` に登録済み。
#   - **認証は IAP に全面委譲**。アプリ内に認証コードは置かず、許可リスト
#     (roles/iap.httpsResourceAccessor)に載る Google アカウントだけがアクセスできる。
#     許可リストは**宣言的**に管理する(set-iam-policy で代表1名へ収束。追加専用の
#     add-iam-policy-binding は承認痕跡なしに閲覧者が増えるため使わない — 中-5)。
#   - **DB は VM(ryza-bot)内 PostgreSQL** に Cloud Run の Direct VPC egress で接続する
#     (VPC コネクタ不要。default VPC の ${REGION} サブネットに egress し、VM の内部 IP へ)。
#   - **DB は 2 ロール構成**(重大-2 / 反対意見書2の代替案):
#       ryza_dashboard … 読取専用。ryza のメンバーシップを持たず(IN ROLE 廃止)、
#                        全スキーマに SELECT のみ+default_transaction_read_only = on。
#       ryza_boardroom … 役員室の書込専用。governance.minutes / minute_resolutions /
#                        stances の INSERT と meta.runs の INSERT/UPDATE のみ。
#     それぞれ別 Secret の接続 URL を別 env で Cloud Run に注入する。
#   - **既存サービスを壊さない**: bot/daily は postgresql://ryza:ryza@localhost のまま。
#     role `ryza` のパスワードは変更しない(変更すると localhost の scram 認証で bot/daily が
#     壊れる)。pg_hba は「VPC サブネットからは上記2ロールのみ scram、localhost は現状維持」。
#   - **パスワードは VM に平文で渡さない**(中-6)。SCRAM-SHA-256 の検証子を
#     クライアント側(このスクリプトを実行する端末)で生成し、ALTER ROLE には検証子だけを
#     渡す。検証子から認証に必要な ClientKey は導出できない(RFC 5802)。
#   - **実行 SA は専用の最小権限 SA**(中-4)。Secret へのアクセスは対象 Secret 単位。
#   - コスト: min-instances=0 / max-instances=1(コールドスタート数十秒は許容 — README)。
#
# 実行タイミングの注意(低-8):
#   PostgreSQL の restart は **設定に実変更があった初回のみ**行う。それでも
#   **09:00 JST 前後(日次サイクル jobs.daily の実行帯)は避けること**。
#   このスクリプトは 08:45〜09:30 JST に再起動が必要になる場合、警告を出す。
#
# 前提(設計リードが事前に用意 — スクリプトは検証して中断する):
#   - deploy-bot.sh 実行済み(VM ryza-bot・PostgreSQL・DB ryza が存在)
#   - ローカルの作業ツリーが clean で HEAD == origin/main(= マージ済みの承認済みコード)
#   - gcloud 認証済み。専用 SA を実行 SA に指定するため、実行者に
#     iam.serviceAccounts.actAs(roles/iam.serviceAccountUser)が必要
#   - IAP の有効化はプロジェクトの OAuth 同意画面の構成が必要な場合がある
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
DB_NAME="${DB_NAME:-ryza}"
DB_OWNER="${DB_OWNER:-ryza}"                                       # 既存テーブルの所有ロール
DB_ROLE="${DB_ROLE:-ryza_dashboard}"                               # 読取専用
BOARDROOM_ROLE="${BOARDROOM_ROLE:-ryza_boardroom}"                 # 役員室の書込専用
DB_PASSWORD_SECRET="${DB_PASSWORD_SECRET:-ryza-db-password}"       # ${DB_ROLE} のパスワード
BR_PASSWORD_SECRET="${BR_PASSWORD_SECRET:-ryza-boardroom-db-password}"
DB_URL_SECRET="${DB_URL_SECRET:-ryza-dashboard-db-url}"            # 読取専用の接続 URL
BR_URL_SECRET="${BR_URL_SECRET:-ryza-boardroom-db-url}"            # 役員室の接続 URL
LLM_KEY_SECRET="${LLM_KEY_SECRET:-anthropic-api-key}"              # 役員室の LLM(あれば付与)
AR_REPO="${AR_REPO:-ryza}"
RUNTIME_SA_ID="${RUNTIME_SA_ID:-ryza-dashboard}"                   # 専用実行 SA(中-4)
PGVER="${PGVER:-17}"
# 承認済みコードの出所。origin がこれ以外を指していたらデプロイしない(再審査 条件4)。
EXPECTED_ORIGIN="${EXPECTED_ORIGIN:-https://github.com/klonyapin/ryza}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH=(gcloud compute ssh "${VM}" --zone "${ZONE}" --project "${PROJECT}")

echo "== ryza-dashboard deploy (Cloud Run + IAP) =="
echo "project=${PROJECT} region=${REGION} vm=${VM} service=${SERVICE} user=${DASHBOARD_USER}"

# ── 0. 承認済みコード一致の検証(重大-1・定款第5条)────────────────────────────
# デプロイできるのは「レビュー・CI・マージを通った main の実物」だけ。ローカルの
# 未コミット変更や未 push のコミットが本番へ入る経路をここで塞ぐ。
# 抜け道(--force 等)は意図的に用意しない — 用意すると統制目的が消えるため。
echo "-- 稼働コードの検証: 作業ツリー clean かつ HEAD == origin/main"
if ! git -C "${ROOT}" rev-parse --git-dir >/dev/null 2>&1; then
  echo "ERROR: ${ROOT} は git リポジトリではない。デプロイは承認済み main の checkout から行う。" >&2
  exit 1
fi
# origin が本物のリポジトリを指すことを確認する。HEAD == origin/main だけでは、
# origin を攻撃者のリモートに差し替えれば任意コードが「承認済み」を騙れる(再審査 条件4)。
ORIGIN_URL="$(git -C "${ROOT}" remote get-url origin 2>/dev/null || true)"
if [ "${ORIGIN_URL%.git}" != "${EXPECTED_ORIGIN%.git}" ]; then
  echo "ERROR: origin が想定と違う(取得='${ORIGIN_URL}' 期待='${EXPECTED_ORIGIN}')。" >&2
  echo "       origin を差し替えれば HEAD==origin/main は容易に満たせるため、ここで中断する。" >&2
  echo "       SSH リモート(git@github.com:...)を使っている場合は EXPECTED_ORIGIN で明示すること。" >&2
  exit 1
fi
DIRTY="$(git -C "${ROOT}" status --porcelain)"
if [ -n "${DIRTY}" ]; then
  echo "ERROR: 作業ツリーに未コミット/未追跡の変更がある。デプロイ対象は承認済み main のみ(定款第5条)。" >&2
  printf '%s\n' "${DIRTY}" >&2
  exit 1
fi
git -C "${ROOT}" fetch origin main --quiet
CODE_VERSION="$(git -C "${ROOT}" rev-parse HEAD)"
ORIGIN_MAIN="$(git -C "${ROOT}" rev-parse origin/main)"
if [ "${CODE_VERSION}" != "${ORIGIN_MAIN}" ]; then
  echo "ERROR: HEAD(${CODE_VERSION}) が origin/main(${ORIGIN_MAIN})と一致しない。" >&2
  echo "       PR をマージし、main を checkout してから再実行すること(全変更 PR 経由)。" >&2
  exit 1
fi
echo "-- code_version=${CODE_VERSION}(origin/main と一致)"

# イメージタグはコミット SHA(:latest は使わない — どのコードが動いているかを不変にする)。
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/dashboard:${CODE_VERSION}"

# 日次サイクル(09:00 JST)帯の警告(低-8)。
JST_HHMM="$(TZ=Asia/Tokyo date +%H%M)"
if [ "${JST_HHMM}" -ge 845 ] && [ "${JST_HHMM}" -le 930 ]; then
  echo "WARNING: 現在 ${JST_HHMM} JST — 日次サイクル(09:00 JST)の実行帯。" >&2
  echo "         PostgreSQL の設定変更が必要な場合は再起動が入る。時間をずらすことを強く推奨。" >&2
fi

# ── 1. API 有効化(冪等)と VM の存在確認 ───────────────────────────────────────
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  secretmanager.googleapis.com iap.googleapis.com compute.googleapis.com \
  artifactregistry.googleapis.com iam.googleapis.com \
  --project "${PROJECT}" >/dev/null

if ! gcloud compute instances describe "${VM}" --zone "${ZONE}" --project "${PROJECT}" >/dev/null 2>&1; then
  echo "ERROR: VM '${VM}' が存在しません。先に ops/deploy-bot.sh を実行してください。" >&2
  exit 1
fi

# ── 2. DB パスワード Secret(無ければ生成して作成。hex なので URL セーフ) ─────────
# stdout はパスワードそのもの(呼び出し側が $() で受ける)。ログは必ず stderr へ。
ensure_password_secret() {  # $1=secret 名
  if ! gcloud secrets describe "$1" --project "${PROJECT}" >/dev/null 2>&1; then
    echo "-- Secret '$1' を新規作成(openssl rand -hex 24)" >&2
    openssl rand -hex 24 | tr -d '\n' | gcloud secrets create "$1" \
      --project "${PROJECT}" --data-file=- --replication-policy=automatic >/dev/null
  fi
  gcloud secrets versions access latest --secret "$1" --project "${PROJECT}"
}
DASH_PW="$(ensure_password_secret "${DB_PASSWORD_SECRET}")"
BR_PW="$(ensure_password_secret "${BR_PASSWORD_SECRET}")"
if [ -z "${DASH_PW}" ] || [ -z "${BR_PW}" ]; then
  echo "ERROR: パスワード Secret を取得できなかった(空)。空パスワードのロールは作らない。" >&2
  exit 1
fi

# ── 3. ネットワーク情報(VM 内部 IP・サブネット CIDR) ───────────────────────────
INTERNAL_IP="$(gcloud compute instances describe "${VM}" --zone "${ZONE}" --project "${PROJECT}" \
  --format='value(networkInterfaces[0].networkIP)')"
SUBNET_CIDR="$(gcloud compute networks subnets describe "${SUBNET}" --region "${REGION}" \
  --project "${PROJECT}" --format='value(ipCidrRange)')"
echo "-- VM 内部 IP=${INTERNAL_IP} / サブネット CIDR=${SUBNET_CIDR}"

# ── 4. ロール定義 SQL をクライアント側で生成(中-6・重大-2) ─────────────────────
# 平文パスワードは VM に渡さない。SCRAM-SHA-256 検証子(RFC 5802)をここで作り、
# ALTER ROLE には検証子だけを渡す。検証子には StoredKey(= SHA256(ClientKey))しか
# 含まれず、そこから ClientKey を復元できないため、検証子の漏洩では認証できない。
# 生成物は base64 で転送する(検証子は '$' を含み、シェル展開で壊れるため)。
echo "-- ロール定義 SQL を生成(SCRAM 検証子はクライアント側生成)"
ROLE_SQL_B64="$(
  RYZA_DASH_PW="${DASH_PW}" RYZA_BR_PW="${BR_PW}" \
  RYZA_DASH_ROLE="${DB_ROLE}" RYZA_BR_ROLE="${BOARDROOM_ROLE}" \
  RYZA_DB="${DB_NAME}" RYZA_OWNER="${DB_OWNER}" \
  python3 - <<'PY'
import base64
import hashlib
import hmac
import os
import secrets


def scram_verifier(password: str, iterations: int = 4096) -> str:
    """PostgreSQL の SCRAM-SHA-256 検証子文字列を作る(RFC 5802 / PG scram-common.c)。

    形式: SCRAM-SHA-256$<iters>:<b64 salt>$<b64 StoredKey>:<b64 ServerKey>
    パスワードは openssl rand -hex(ASCII)なので SASLprep は恒等変換。
    """
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda raw: base64.b64encode(raw).decode("ascii")  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


SQL = r"""
-- ============================================================================
-- Ryza ダッシュボードの DB ロール(2ロール構成)
--   生成: ops/deploy-dashboard.sh(独立役員審査 2026-08-03 重大-2 の是正)
--   平文パスワードはこのファイルに含まれない(SCRAM 検証子のみ)
-- ============================================================================

-- ── 1) __DASH_ROLE__: 読取専用 ───────────────────────────────────────────────
SELECT 'CREATE ROLE "__DASH_ROLE__" LOGIN'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '__DASH_ROLE__');
\gexec

-- 旧版(CREATE ROLE ... IN ROLE __OWNER__)で付いた全権限の継承を剥がす。
SELECT 'REVOKE "__OWNER__" FROM "__DASH_ROLE__"'
  FROM pg_auth_members m
  JOIN pg_roles g ON g.oid = m.roleid
  JOIN pg_roles u ON u.oid = m.member
 WHERE g.rolname = '__OWNER__' AND u.rolname = '__DASH_ROLE__';
\gexec

ALTER ROLE "__DASH_ROLE__"
  LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD '__DASH_VERIFIER__';

-- 既定を読取専用トランザクションに固定(アプリ側の SET に依存しない)。
ALTER ROLE "__DASH_ROLE__" SET default_transaction_read_only = on;

GRANT CONNECT ON DATABASE "__DB__" TO "__DASH_ROLE__";

-- SELECT だけを全ユーザースキーマに付与する。個別列挙にしないのは、スキーマ進化の
-- たびに保守が必要になり付け漏れが起きるため(反対意見書2の代替案)。書込特権は
-- 一切付与しないので、列挙漏れがあっても過剰権限にはならない。
SELECT format('GRANT USAGE ON SCHEMA %I TO %I', nspname, '__DASH_ROLE__')
  FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema';
\gexec
SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO %I', nspname, '__DASH_ROLE__')
  FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema';
\gexec
SELECT format(
         'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT SELECT ON TABLES TO %I',
         '__OWNER__', nspname, '__DASH_ROLE__')
  FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema';
\gexec

-- 秘密を保持するテーブルは一括 GRANT の対象から**明示的に外す**。
--   ops.discord_webhooks.webhook_url は「URL を知る者が誰でも当該チャンネルへ投稿できる」
--   秘密(migrations/0017 冒頭)。ダッシュボードは表示に一切使わないので読ませない。
-- 注意: ALTER DEFAULT PRIVILEGES により**将来作られるテーブルにも SELECT が付く**。
--   秘密を持つテーブルを新設したら、この除外リストに追加すること
--   (恒久策は ops/reminders.yaml db-role-separation-webhook-url のロール分離)。
--   ops.org_icon_overrides / ops.org_icon_override_log(0020)は公開画像 URL のみで
--   秘密を持たず、組織ページの表示に読取が必要なため**除外しない**(SELECT のまま)。
SELECT format('REVOKE ALL ON %s FROM %I', t.rel, '__DASH_ROLE__')
  FROM (VALUES ('ops.discord_webhooks')) AS t(rel)
 WHERE to_regclass(t.rel) IS NOT NULL;
\gexec

-- ── 2) __BR_ROLE__: 役員室の書込専用(最小権限)─────────────────────────────
-- 書込先は追記オンリーの3テーブル+実行記録のみ。帳簿(ledger)・取引状態(ops)・
-- 監査対象への経路を持たない。
SELECT 'CREATE ROLE "__BR_ROLE__" LOGIN'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '__BR_ROLE__');
\gexec

ALTER ROLE "__BR_ROLE__"
  LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD '__BR_VERIFIER__';
ALTER ROLE "__BR_ROLE__" SET default_transaction_read_only = off;

GRANT CONNECT ON DATABASE "__DB__" TO "__BR_ROLE__";
GRANT USAGE ON SCHEMA governance, meta, ops TO "__BR_ROLE__";
-- 着任プロンプト(stances)・決議一覧・Run の読み出しに必要な SELECT。
GRANT SELECT ON governance.minutes, governance.minute_resolutions, governance.stances,
                meta.runs TO "__BR_ROLE__";
GRANT INSERT ON governance.minutes, governance.minute_resolutions, governance.stances
  TO "__BR_ROLE__";
-- キャラクターアイコンの上書き(0020・代表指示 2026-08-03)。組織ページの編集 UI が
-- このロールで書く。書けるのはこの 2 表だけで、ops の他の表(trading_state・flags・
-- discord_webhooks 等)への権限は与えない — Kill Switch や webhook 秘密への経路を作らない。
-- 現在値表は上書きが本義のため UPDATE と、初期値へ戻すための DELETE が要る。
-- 履歴表は**追記オンリー**(INSERT のみ。UPDATE/DELETE を与えず履歴を消せなくする)。
SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE ON ops.org_icon_overrides TO %I',
              '__BR_ROLE__')
 WHERE to_regclass('ops.org_icon_overrides') IS NOT NULL;
\gexec
SELECT format('GRANT INSERT ON ops.org_icon_override_log TO %I', '__BR_ROLE__')
 WHERE to_regclass('ops.org_icon_override_log') IS NOT NULL;
\gexec
-- meta.runs は開始 INSERT → 終了時に status/finished_at/cost を UPDATE する。UPDATE は
-- **列レベル**に限定し、job_name / code_version / started_at / params の事後改竄を防ぐ
-- (リネージの証跡性。再審査 条件3)。列名は migrations/0001_meta.sql に一致させること。
GRANT INSERT, UPDATE (finished_at, status, cost) ON meta.runs TO "__BR_ROLE__";

-- ── 3) 証跡(デプロイログに残す)─────────────────────────────────────────────
\echo '-- ロール属性(rolsuper/rolinherit は false、memberships は 0 であること)'
SELECT r.rolname, r.rolcanlogin, r.rolsuper, r.rolinherit,
       (SELECT count(*) FROM pg_auth_members m WHERE m.member = r.oid) AS memberships
  FROM pg_roles r WHERE r.rolname IN ('__DASH_ROLE__', '__BR_ROLE__') ORDER BY 1;
\echo '-- 読取専用ロールの非 SELECT 権限(0 であること)'
SELECT count(*) AS dashboard_write_grants
  FROM information_schema.role_table_grants
 WHERE grantee = '__DASH_ROLE__' AND privilege_type <> 'SELECT';
\echo '-- 読取専用ロールの秘密テーブルへの権限(0 であること)'
SELECT count(*) AS dashboard_secret_grants
  FROM information_schema.role_table_grants
 WHERE grantee = '__DASH_ROLE__'
   AND table_schema || '.' || table_name IN ('ops.discord_webhooks');
"""

sql = (
    SQL.replace("__DASH_VERIFIER__", scram_verifier(os.environ["RYZA_DASH_PW"]))
    .replace("__BR_VERIFIER__", scram_verifier(os.environ["RYZA_BR_PW"]))
    .replace("__DASH_ROLE__", os.environ["RYZA_DASH_ROLE"])
    .replace("__BR_ROLE__", os.environ["RYZA_BR_ROLE"])
    .replace("__OWNER__", os.environ["RYZA_OWNER"])
    .replace("__DB__", os.environ["RYZA_DB"])
)
print(base64.b64encode(sql.encode("utf-8")).decode("ascii"))
PY
)"

# ── 5. VM 側 PostgreSQL 設定(冪等。localhost 接続の bot/daily は変更しない) ─────
echo "-- VM 上の PostgreSQL を VPC サブネットからの接続に対応させる(冪等)"
"${SSH[@]}" --command "sudo bash -s" <<REMOTE
set -euo pipefail
CONF_DIR="/etc/postgresql/${PGVER}/main"
CONF_D="\${CONF_DIR}/conf.d"
HBA="\${CONF_DIR}/pg_hba.conf"
NEED_RESTART=0
NEED_RELOAD=0

# 5.1 listen_addresses に内部 IP を追加(conf.d 経由・postgresql.conf 本体は触らない)。
install -d "\${CONF_D}"
DESIRED="listen_addresses = 'localhost,${INTERNAL_IP}'  # ryza-dashboard (deploy-dashboard.sh)"
CUR="\$(cat "\${CONF_D}/20-ryza-dashboard.conf" 2>/dev/null || true)"
if [ "\${CUR}" != "\${DESIRED}" ]; then
  printf '%s\n' "\${DESIRED}" > "\${CONF_D}/20-ryza-dashboard.conf"
  NEED_RESTART=1   # listen_addresses は reload では反映されない
else
  echo "listen_addresses は設定済み(変更なし)"
fi

# 5.2 pg_hba: 既存の non-localhost host 行を検査してから追記する。
#     pg_hba は**先勝ち**のため、既存の広い host 行があると本行は無効化される。
#     想定外の行を見つけたら中断し、人間に判断させる(自動で消さない)。
DESIRED_HBA="host    ${DB_NAME}    ${DB_ROLE},${BOARDROOM_ROLE}    ${SUBNET_CIDR}    scram-sha-256"
HOST_LINES="\$(grep -E '^[[:space:]]*host(ssl|nossl)?[[:space:]]' "\${HBA}" || true)"
UNEXPECTED="\$(printf '%s\n' "\${HOST_LINES}" \
  | grep -vE '127\.0\.0\.1/32|::1/128|samehost|localhost' \
  | grep -vF "\${DESIRED_HBA}" || true)"
if [ -n "\${UNEXPECTED}" ]; then
  echo "ERROR: pg_hba.conf に想定外の non-localhost host 行がある(先勝ち規則で本設定が無効化される)。" >&2
  printf '%s\n' "\${UNEXPECTED}" >&2
  echo "       内容を確認し、不要な行を削除してから再実行すること(自動削除はしない)。" >&2
  exit 1
fi
if ! grep -qF "\${DESIRED_HBA}" "\${HBA}"; then
  printf '%s\n' "\${DESIRED_HBA}" >> "\${HBA}"
  NEED_RELOAD=1
  echo "pg_hba に追記: \${DESIRED_HBA}"
else
  echo "pg_hba は設定済み(変更なし): \${DESIRED_HBA}"
fi

# 5.3 ロール(読取専用+役員室書込)。平文パスワードは渡らない — SCRAM 検証子のみ。
#     psql には -f - (stdin) で渡し、argv にもシェル履歴にも SQL を残さない。
printf '%s' '${ROLE_SQL_B64}' | base64 -d \
  | sudo -u postgres psql -q -v ON_ERROR_STOP=1 -d "${DB_NAME}" -f -

# 5.4 再起動/再読込は**実変更があったときだけ**(低-8)。
if [ "\${NEED_RESTART}" = 1 ]; then
  echo "listen_addresses が変わったため PostgreSQL を再起動する(bot/daily は再接続で復帰)"
  systemctl restart postgresql
elif [ "\${NEED_RELOAD}" = 1 ]; then
  echo "pg_hba が変わったため PostgreSQL を再読込する(接続は切れない)"
  systemctl reload postgresql
else
  echo "PostgreSQL の設定に変更なし — 再起動・再読込は行わない"
fi
REMOTE

# ── 6. ファイアウォール(サブネット内→VM:5432 のみ。0.0.0.0/0 は開けない) ────────
# source は Direct VPC egress が使う ${REGION} サブネットに限定(Cloud Run の送信元 IP は
# このサブネット内で動的に割り当てられるため、これ以上は絞れない)。
if ! gcloud compute firewall-rules describe ryza-allow-dashboard-db --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud compute firewall-rules create ryza-allow-dashboard-db \
    --project "${PROJECT}" \
    --network "${NETWORK}" --direction INGRESS --allow "tcp:5432" \
    --source-ranges "${SUBNET_CIDR}" --target-tags ryza-db \
    --description "Cloud Run (Direct VPC egress) -> VM PostgreSQL (deploy-dashboard.sh)" >/dev/null
fi
gcloud compute instances add-tags "${VM}" --zone "${ZONE}" --project "${PROJECT}" \
  --tags ryza-db >/dev/null

# ── 7. 接続 URL Secret(値が変わったときだけ新版を積む) ─────────────────────────
put_url_secret() {  # $1=secret 名 $2=URL
  local current
  if ! gcloud secrets describe "$1" --project "${PROJECT}" >/dev/null 2>&1; then
    printf %s "$2" | gcloud secrets create "$1" --project "${PROJECT}" \
      --data-file=- --replication-policy=automatic >/dev/null
    return
  fi
  current="$(gcloud secrets versions access latest --secret "$1" --project "${PROJECT}" 2>/dev/null || true)"
  if [ "${current}" != "$2" ]; then
    printf %s "$2" | gcloud secrets versions add "$1" --project "${PROJECT}" --data-file=- >/dev/null
  fi
}
put_url_secret "${DB_URL_SECRET}" "postgresql://${DB_ROLE}:${DASH_PW}@${INTERNAL_IP}:5432/${DB_NAME}"
put_url_secret "${BR_URL_SECRET}" "postgresql://${BOARDROOM_ROLE}:${BR_PW}@${INTERNAL_IP}:5432/${DB_NAME}"

# ── 8. 専用実行 SA(中-4)。既定 compute SA(プロジェクト全体に強い)は使わない ────
RUNTIME_SA="${RUNTIME_SA_ID}@${PROJECT}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "${RUNTIME_SA}" --project "${PROJECT}" >/dev/null 2>&1; then
  echo "-- 専用実行 SA を作成: ${RUNTIME_SA}"
  gcloud iam service-accounts create "${RUNTIME_SA_ID}" --project "${PROJECT}" \
    --display-name "Ryza dashboard (Cloud Run runtime)" \
    --description "ダッシュボードの Cloud Run 実行 SA。付与は対象 Secret の accessor のみ" >/dev/null
fi
# Secret へのアクセスは**対象 Secret 単位**で付与(プロジェクトレベルでは付けない)。
for s in "${DB_URL_SECRET}" "${BR_URL_SECRET}"; do
  gcloud secrets add-iam-policy-binding "${s}" --project "${PROJECT}" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/secretmanager.secretAccessor" >/dev/null
done
# 役員室の LLM キー(存在するときだけ。無ければ env 経由の運用)。
if gcloud secrets describe "${LLM_KEY_SECRET}" --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud secrets add-iam-policy-binding "${LLM_KEY_SECRET}" --project "${PROJECT}" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/secretmanager.secretAccessor" >/dev/null
fi

# ── 9. イメージビルド(Cloud Build。コンテキストはリポジトリルート) ──────────────
gcloud artifacts repositories describe "${AR_REPO}" --location "${REGION}" --project "${PROJECT}" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "${AR_REPO}" --location "${REGION}" --project "${PROJECT}" \
       --repository-format docker --description "Ryza images" >/dev/null
echo "-- Cloud Build でイメージをビルド: ${IMAGE}"
CB="$(mktemp /tmp/ryza-dashboard-cb.XXXXXX.yaml)"
IAP_POLICY="$(mktemp /tmp/ryza-dashboard-iap.XXXXXX.yaml)"
trap 'rm -f "${CB}" "${IAP_POLICY}"' EXIT
cat > "${CB}" <<YAML
steps:
  - name: gcr.io/cloud-builders/docker
    args: [build, -f, dashboard/Dockerfile, -t, "${IMAGE}", .]
images: ["${IMAGE}"]
YAML
gcloud builds submit --config "${CB}" --project "${PROJECT}" "${ROOT}" >/dev/null

# ── 10. Cloud Run デプロイ(Direct VPC egress・非公開・min 0/max 1) ──────────────
echo "-- Cloud Run へデプロイ: ${SERVICE}"
gcloud run deploy "${SERVICE}" \
  --project "${PROJECT}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --service-account "${RUNTIME_SA}" \
  --no-allow-unauthenticated \
  --min-instances 0 --max-instances 1 \
  --memory 1Gi --cpu 1 \
  --port 8080 \
  --network "${NETWORK}" --subnet "${SUBNET}" \
  --vpc-egress private-ranges-only \
  --labels "code-version=${CODE_VERSION}" \
  --set-env-vars "GCP_PROJECT=${PROJECT},RYZA_CODE_VERSION=${CODE_VERSION}" \
  --set-secrets "RYZA_DATABASE_URL=${DB_URL_SECRET}:latest,RYZA_BOARDROOM_DATABASE_URL=${BR_URL_SECRET}:latest" \
  --quiet >/dev/null

# ── 11. IAP 有効化+許可リスト(宣言的に代表1名へ収束 — 中-5) ────────────────────
echo "-- IAP を有効化(失敗したら console で OAuth 同意画面を1回設定してから再実行)"
if ! gcloud beta run services update "${SERVICE}" --project "${PROJECT}" --region "${REGION}" --iap --quiet; then
  cat >&2 <<'NOTE'
ERROR: IAP の有効化に失敗。多くの場合 OAuth 同意画面が未構成のため。console で
  APIs & Services → OAuth consent screen を User Type=External(公開せず・自分のみ)
  または組織があれば Internal で1回だけ構成し、本スクリプトを再実行すること。
NOTE
  exit 1
fi
# add-iam-policy-binding(追加専用)は使わない。追加専用だと承認痕跡なしに閲覧者が
# 増え、増えた人物が役員室で決議を代表名義でマークできてしまう(中-5)。
cat > "${IAP_POLICY}" <<YAML
bindings:
- role: roles/iap.httpsResourceAccessor
  members:
  - user:${DASHBOARD_USER}
YAML
gcloud beta iap web set-iam-policy "${IAP_POLICY}" \
  --project "${PROJECT}" --resource-type=cloud-run \
  --service="${SERVICE}" --region="${REGION}" --quiet >/dev/null
echo "-- IAP 許可リスト(宣言的に収束済み):"
gcloud beta iap web get-iam-policy --project "${PROJECT}" --resource-type=cloud-run \
  --service="${SERVICE}" --region="${REGION}" \
  --flatten="bindings[].members" --format="value(bindings.role,bindings.members)" || true

# ── 12. 公開バインディングの検査と除去(重大-3)──────────────────────────────────
# 2026-08-02 の無認証 Cloud Run 公開版と同名サービスの場合、allUsers の run.invoker が
# 残っていると IAP を有効化しても直接 URL で全世界に公開されたままになる。
#
# **失敗は「公開なし」ではない**(再審査 条件1)。get-iam-policy が権限不足・API 断で
# 落ちたときに「検出ゼロ」と同じ扱いにすると、検査自体が沈黙して公開を見逃す。
# 取得の終了コードを見て、失敗なら中断する。
echo "-- 公開バインディング(allUsers / allAuthenticatedUsers)を検査"
service_iam_policy() {  # 失敗時は非ゼロで返る(呼び出し側が中断する)
  gcloud run services get-iam-policy "${SERVICE}" --project "${PROJECT}" --region "${REGION}" \
    --flatten="bindings[].members" --format="value(bindings.role,bindings.members)"
}
public_members_in() {  # $1=ポリシーのテキスト
  printf '%s\n' "$1" | grep -E '[[:space:]](allUsers|allAuthenticatedUsers)$' || true
}

if ! POLICY="$(service_iam_policy)"; then
  echo "ERROR: ${SERVICE} の IAM ポリシーを取得できなかった。公開状態を確認できないため中断する。" >&2
  echo "       (取得失敗を『公開なし』とみなすと検査が沈黙する — 再審査 条件1)" >&2
  exit 1
fi
FOUND="$(public_members_in "${POLICY}")"
if [ -n "${FOUND}" ]; then
  echo "WARNING: 公開バインディングを検出したため除去する:" >&2
  printf '%s\n' "${FOUND}" >&2
  while IFS=$'\t ' read -r role member; do
    [ -n "${role:-}" ] && [ -n "${member:-}" ] || continue
    # </dev/null: gcloud に while の入力(残りの行)を食わせない
    gcloud run services remove-iam-policy-binding "${SERVICE}" \
      --project "${PROJECT}" --region "${REGION}" \
      --member="${member}" --role="${role}" --quiet >/dev/null </dev/null
  done <<< "${FOUND}"
  if ! POLICY="$(service_iam_policy)"; then
    echo "ERROR: 除去後の IAM ポリシーを再取得できなかった。公開のままの可能性がある。" >&2
    exit 1
  fi
  STILL="$(public_members_in "${POLICY}")"
  if [ -n "${STILL}" ]; then
    echo "ERROR: 公開バインディングを除去できなかった。サービスは全世界公開のままの可能性がある。" >&2
    printf '%s\n' "${STILL}" >&2
    exit 1
  fi
  echo "-- 公開バインディングを除去した(現在は無し)"
else
  echo "-- サービスレベルの公開バインディングは無し"
fi

# ── 12.5 プロジェクトレベル IAM の公開バインディング(再審査 条件1)────────────────
# roles/run.invoker がプロジェクト全体で allUsers に付いていると、サービス側の
# ポリシーが清潔でも全 Cloud Run サービスが無認証で叩ける。ここは**自動で消さない** —
# 他サービスへの影響が読めないため、検出したら中断して人間に判断させる。
echo "-- プロジェクトレベル IAM の公開バインディングを検査"
if ! PROJ_POLICY="$(gcloud projects get-iam-policy "${PROJECT}" \
    --flatten="bindings[].members" --format="value(bindings.role,bindings.members)")"; then
  echo "ERROR: プロジェクトの IAM ポリシーを取得できなかった。公開状態を確認できないため中断する。" >&2
  exit 1
fi
PROJ_PUBLIC="$(printf '%s\n' "${PROJ_POLICY}" \
  | grep -E '^roles/run\.[a-zA-Z]+[[:space:]]+(allUsers|allAuthenticatedUsers)$' || true)"
if [ -n "${PROJ_PUBLIC}" ]; then
  echo "ERROR: プロジェクトレベルで Cloud Run の公開バインディングがある(全サービスが無認証で到達可能):" >&2
  printf '%s\n' "${PROJ_PUBLIC}" >&2
  echo "       他サービスへの影響があるため自動では消さない。手動で除去してから再実行すること。" >&2
  exit 1
fi
echo "-- プロジェクトレベルの公開バインディングは無し"

URL="$(gcloud run services describe "${SERVICE}" --project "${PROJECT}" --region "${REGION}" \
  --format='value(status.url)')"

# ── 13. 陽性テスト: 未認証アクセスが実際に拒否されることを確認(再審査 条件1)────────
# IAM ポリシーの読みは「設定がそうなっている」ことしか示さない。実際に外から叩いて
# 拒否されることを確認して初めて統制が効いていると言える(議論規約4)。
# -L は付けない — IAP のサインイン画面への 302 は「拒否」であり、追跡すると 200 に見える。
if ! command -v curl >/dev/null 2>&1; then
  echo "ERROR: curl が無く未認証アクセスの陽性テストを実行できない。検証せずに完了扱いにしない。" >&2
  exit 1
fi
echo "-- 陽性テスト: 未認証で ${URL}/ を叩く(401/403/302 なら拒否されている)"
if ! HTTP_CODE="$(curl -sS -o /dev/null -w '%{http_code}' -m 30 "${URL}/")"; then
  HTTP_CODE="000"   # 接続失敗(DNS・タイムアウト等)。拒否の証拠にはならない
fi
case "${HTTP_CODE}" in
  401|403|302|307)
    echo "-- 未認証アクセスは HTTP ${HTTP_CODE}(拒否)— OK"
    ;;
  2??)
    echo "ERROR: 未認証アクセスが HTTP ${HTTP_CODE} を返した。サービスが公開されている。" >&2
    echo "       IAP の有効化と invoker バインディングを確認すること。" >&2
    exit 1
    ;;
  *)
    echo "WARNING: 未認証アクセスが想定外の HTTP ${HTTP_CODE}(000=接続失敗)。" >&2
    echo "         拒否と断定できないため、ブラウザで直接 URL を開いて確認すること。" >&2
    ;;
esac

echo "== 完了 =="
echo "アクセス URL: ${URL}(${DASHBOARD_USER} の Google アカウントでのみ閲覧可)"
echo "code_version: ${CODE_VERSION}(= origin/main。イメージタグ・Cloud Run ラベル・env に記録)"
echo "初回はコールドスタートで数十秒かかる(min-instances=0 のコスト最適化 — README 参照)"
echo "DB 接続: Direct VPC egress → ${INTERNAL_IP}:5432"
echo "  読取 = ${DB_ROLE}(SELECT のみ・default_transaction_read_only=on)"
echo "  役員室 = ${BOARDROOM_ROLE}(governance 3テーブル INSERT + meta.runs のみ)"
echo "  bot/daily の localhost 接続(role ryza)は不変"
