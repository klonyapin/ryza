#!/usr/bin/env bash
#
# deploy-dashboard.sh — 運用ダッシュボードを Cloud Run + IAP で公開する(2026-08-03 代表指示)。
#
# 冪等: 何度再実行してもよい。全ステップが「無ければ作る・有れば流用/更新」で書かれている。
#
# 本スクリプトは保護領域 deploy_path(定款第5条)。独立役員審査(2026-08-03・
# docs/reviews/dashboard-deploy-independent-review.md)の差し戻しを受けて是正済み。
#
# 統制ゲートの所在(再審査「次回 PR 対応」で切り出し。保護領域の一部):
#   ops/lib/deploy-guards.sh  … git ゲート・公開バインディング検査(gcloud を使う)
#   ops/lib/pg_hba_check.sh   … pg_hba のアドレス列検査(base64 で VM へ運ぶ)
#   ロール権限ゲートは本ファイル内の生成 SQL 末尾 `DO $ryza_gate$`(値が想定外なら中断)
#   いずれも tests/ops/test_deploy_guards.py / test_deploy_role_gate.py が
#   「実際に中断すること」を CI で検証している(統制コードは壊れても静かに通るため)。
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
#       ryza_boardroom … 役員室・開発室の書込専用。governance.minutes /
#                        minute_resolutions / stances の INSERT、meta.runs の
#                        INSERT/UPDATE、ops.org_icon_overrides(0020)と
#                        ops.dev_chat(0024・SELECT と列レベル INSERT (sender, body) のみ)。
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
# **env で上書きできない**(第3回審査 C-5)。上書きできる限り、攻撃者は origin を
# 自分のリモートに差し替え EXPECTED_ORIGIN を同じ値にするだけでゲートを満たせてしまい、
# 「origin URL の照合」という統制そのものが成立しない。許可するのは本リポジトリの
# 3表記(https の .git 有無 + SSH)だけで、比較時に末尾 .git を落として突き合わせる。
ALLOWED_ORIGINS=(
  "https://github.com/klonyapin/ryza"
  "git@github.com:klonyapin/ryza.git"
)

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH=(gcloud compute ssh "${VM}" --zone "${ZONE}" --project "${PROJECT}")

# 統制ゲートは ops/lib/deploy-guards.sh の関数に切り出してある(CI でテストするため —
# tests/ops/test_deploy_guards.py)。ここでの呼び出しに `|| exit 1` を付け忘れると
# ゲートが無効化されるので注意。
# shellcheck source=lib/deploy-guards.sh
. "${ROOT}/ops/lib/deploy-guards.sh"
# SQL 識別子検証(A-12 F-8・pass4-1)。ロール名 env の入口検査。
# shellcheck source=lib/sql_ident_check.sh
. "${ROOT}/ops/lib/sql_ident_check.sh"

echo "== ryza-dashboard deploy (Cloud Run + IAP) =="
echo "project=${PROJECT} region=${REGION} vm=${VM} service=${SERVICE} user=${DASHBOARD_USER}"

# ── 0.0 ロール名 env の SQL 識別子検証(A-12 F-8・pass4-1)────────────────────────
# 後段(§4)で SQL テンプレートに `.replace()` で埋め込む env を、入口で必ず検査する。
# operator 制御の env であり悪用経路は限定的だが、タイポや `"` / 空白 / セミコロン
# 混入で意図しない SQL に化ける経路を、ここで止める。
# DB_NAME(→RYZA_DB)も同じ SQL テンプレートに識別子として埋め込まれる。指示書の対象
# は「ロール名 env」だが、同じ入口に片方だけの検査を付けるとゲートの整合性が崩れる
# (`"; DROP …` のような値がロール名では止まるが DB 名では素通りする)ため、同一関数で
# 併せて検査する。判断根拠は本コメントに明記(実装指示書との逸脱として完了報告に記載)。
assert_sql_ident RYZA_DASH_ROLE "${DB_ROLE}" || exit 1
assert_sql_ident RYZA_BR_ROLE   "${BOARDROOM_ROLE}" || exit 1
assert_sql_ident RYZA_OWNER     "${DB_OWNER}" || exit 1
assert_sql_ident RYZA_DB        "${DB_NAME}" || exit 1

# ── 0. 承認済みコード一致の検証(重大-1・定款第5条)────────────────────────────
echo "-- 稼働コードの検証: 作業ツリー clean かつ HEAD == origin/main"
CODE_VERSION="$(guard_git_state "${ROOT}" "${ALLOWED_ORIGINS[@]}")" || exit 1
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
-- 開発室(0024・代表指示 2026-08-03)。ダッシュボードの開発室ページが代表の連絡を
-- **追記**し、スレッドを読み返すための権限。
--
-- **INSERT は列レベル (sender, body) に限定する**(独立役員審査 重大-1)。表レベルの
-- INSERT では created_at・relayed_at・inserted_by を任意指定でき、
--   * created_at の遡及(存在しなかった時点の指示を捏造する)
--   * relayed_at の事前設定(Discord に出ないのに「中継済み」の行を作り、中継ループに
--     永久に拾わせない)
--   * inserted_by の詐称(書込主体の証跡を偽る)
-- が可能になる。0024 のガードトリガは BEFORE UPDATE OR DELETE で、**INSERT には
-- 発火しない**ため、この入口を塞げるのは権限だけである。
-- 残余: sender は付与列に含まれるので、このロールでも sender='design_lead' の行は
-- 作れる(代表が UI から設計リードを騙る操作)。これは inserted_by との矛盾として
-- 検出する設計で、権限では止めない — 止めると代表自身の投稿も書けなくなる。
--
-- UPDATE を与えないのは意図的で、relayed_at は Bot だけが立てる状態だからである。
-- DELETE も与えない(表は追記オンリー)。
SELECT format('GRANT SELECT, INSERT (sender, body) ON ops.dev_chat TO %I', '__BR_ROLE__')
 WHERE to_regclass('ops.dev_chat') IS NOT NULL;
\gexec
-- meta.runs は開始 INSERT → 終了時に status/finished_at/cost を UPDATE する。UPDATE は
-- **列レベル**に限定し、job_name / code_version / started_at / params の事後改竄を防ぐ
-- (リネージの証跡性。再審査 条件3)。列名は migrations/0001_meta.sql に一致させること。
GRANT INSERT, UPDATE (finished_at, status, cost) ON meta.runs TO "__BR_ROLE__";

-- ── 3) 権限の全経路を束ねた一時ビュー(第3回審査 C-3)────────────────────────
-- information_schema.role_table_grants だけを見ると**列レベル GRANT が見えない**。
-- `GRANT UPDATE (state) ON ops.trading_state TO ryza_boardroom` は role_table_grants に
-- 1行も現れないため、Kill Switch を書き換えられる権限が証跡にもゲートにも映らず
-- 素通りする(実測で確認済み — tests/ops/test_deploy_role_gate.py)。
-- column_privileges は列レベル GRANT を見せ、かつ表レベル GRANT も列へ展開する。
-- ただし DELETE / TRUNCATE は列権限に存在しないため、両者の UNION が必要。
CREATE OR REPLACE TEMP VIEW ryza_gate_grants AS
  SELECT grantee::text AS grantee, table_schema::text AS table_schema,
         table_name::text AS table_name, privilege_type::text AS privilege_type
    FROM information_schema.role_table_grants
  UNION
  SELECT grantee::text, table_schema::text, table_name::text, privilege_type::text
    FROM information_schema.column_privileges;

-- ── 3.1) 証跡(デプロイログに残す)───────────────────────────────────────────
\echo '-- ロール属性(rolsuper/rolinherit は false、memberships は 0 であること)'
SELECT r.rolname, r.rolcanlogin, r.rolsuper, r.rolinherit,
       (SELECT count(*) FROM pg_auth_members m WHERE m.member = r.oid) AS memberships
  FROM pg_roles r WHERE r.rolname IN ('__DASH_ROLE__', '__BR_ROLE__') ORDER BY 1;
\echo '-- 読取専用ロールの非 SELECT 権限(0 であること。列レベル GRANT を含む)'
SELECT count(*) AS dashboard_write_grants
  FROM ryza_gate_grants
 WHERE grantee = '__DASH_ROLE__' AND privilege_type <> 'SELECT';
\echo '-- 読取専用ロールの秘密テーブルへの権限(0 であること)'
SELECT count(*) AS dashboard_secret_grants
  FROM ryza_gate_grants
 WHERE grantee = '__DASH_ROLE__'
   AND table_schema || '.' || table_name IN ('ops.discord_webhooks');

-- 役員室ロールの ops スキーマ権限(独立役員審査 0020 C-5)。上の GRANT は
-- to_regclass ガード付きで、0020 未適用の DB では**黙ってスキップ**される。GRANT が
-- 効いたか/余計な表に広がっていないかを、デプロイのたびにログへ残して検証する。
\echo '-- 役員室ロールが ops で権限を持つ表(org_icon_overrides / org_icon_override_log / dev_chat の3表のみであること)'
SELECT table_name, string_agg(privilege_type, ',' ORDER BY privilege_type) AS privileges
  FROM ryza_gate_grants
 WHERE grantee = '__BR_ROLE__' AND table_schema = 'ops'
 GROUP BY table_name ORDER BY table_name;
\echo '-- 役員室ロールの ops 権限の表数(3 であること。2 なら 0024 未適用で dev_chat の GRANT がスキップされた)'
SELECT count(DISTINCT table_name) AS boardroom_ops_tables
  FROM ryza_gate_grants
 WHERE grantee = '__BR_ROLE__' AND table_schema = 'ops';
\echo '-- 役員室ロールが ops の想定外テーブルに持つ権限(0 であること — trading_state/flags/discord_webhooks 等)'
SELECT count(*) AS boardroom_unexpected_ops_grants
  FROM ryza_gate_grants
 WHERE grantee = '__BR_ROLE__' AND table_schema = 'ops'
   AND table_name NOT IN ('org_icon_overrides', 'org_icon_override_log', 'dev_chat');
\echo '-- 履歴表への非 INSERT 権限(0 であること — 追記オンリー。UPDATE/DELETE/TRUNCATE を持たない)'
SELECT count(*) AS boardroom_log_mutation_grants
  FROM ryza_gate_grants
 WHERE grantee = '__BR_ROLE__' AND table_schema = 'ops'
   AND table_name = 'org_icon_override_log' AND privilege_type <> 'INSERT';
-- 開発室は**列レベル**で INSERT を絞っているため、表レベルの検査だけでは足りない
-- (列だけの GRANT は role_table_grants に現れない)。両方を見る(独立役員審査 重大-1)。
\echo '-- 開発室の表レベル権限(SELECT のみであること — INSERT/UPDATE/DELETE が表レベルで付いていない)'
SELECT count(*) AS boardroom_dev_chat_table_grants
  FROM information_schema.role_table_grants
 WHERE grantee = '__BR_ROLE__' AND table_schema = 'ops'
   AND table_name = 'dev_chat' AND privilege_type <> 'SELECT';
\echo '-- 開発室の列レベル権限(INSERT は sender/body の 2 列だけであること)'
SELECT column_name, privilege_type
  FROM information_schema.role_column_grants
 WHERE grantee = '__BR_ROLE__' AND table_schema = 'ops'
   AND table_name = 'dev_chat' AND privilege_type <> 'SELECT'
 ORDER BY privilege_type, column_name;
\echo '-- 開発室で書込可能な列数(2 であること。0 なら 0024 未適用で GRANT がスキップされた)'
SELECT count(*) AS boardroom_dev_chat_writable_columns
  FROM information_schema.role_column_grants
 WHERE grantee = '__BR_ROLE__' AND table_schema = 'ops'
   AND table_name = 'dev_chat' AND privilege_type <> 'SELECT';
\echo '-- 開発室の想定外の列権限(0 であること — created_at/relayed_at/inserted_by/id への書込)'
SELECT count(*) AS boardroom_dev_chat_unexpected_column_grants
  FROM information_schema.role_column_grants
 WHERE grantee = '__BR_ROLE__' AND table_schema = 'ops'
   AND table_name = 'dev_chat' AND privilege_type <> 'SELECT'
   AND (privilege_type <> 'INSERT' OR column_name NOT IN ('sender', 'body'));

-- ── 4) ゲート(想定外ならデプロイを中断する)──────────────────────────────────
-- 上の SELECT はログに残す**証跡**にすぎず、値が想定外でもデプロイは進んでいた
-- (独立役員 再審査「次回 PR 対応」第1項)。同じ条件をここで**強制**する。
-- psql は -v ON_ERROR_STOP=1 で起動しており、RAISE EXCEPTION は非ゼロ終了となって
-- 呼び出し側(set -e のリモートシェル)ごとデプロイを止める。
DO $ryza_gate$
DECLARE
  n bigint;
  bad text;
  expected_ops_tables int;
BEGIN
  -- 4.1 両ロールが揃っていること
  SELECT count(*) INTO n FROM pg_roles WHERE rolname IN ('__DASH_ROLE__', '__BR_ROLE__');
  IF n <> 2 THEN
    RAISE EXCEPTION 'ロールが揃っていない(__DASH_ROLE__ / __BR_ROLE__ の期待 2 に対し実際 %)', n;
  END IF;

  -- 4.2 ロール属性が最小権限であること(superuser 不可・INHERIT 不可・他ロールの
  --     メンバーシップ 0・LOGIN 必須)。旧版の IN ROLE 継承が残っていればここで落ちる。
  SELECT string_agg(
           format('%s(super=%s inherit=%s login=%s memberships=%s)',
                  r.rolname, r.rolsuper, r.rolinherit, r.rolcanlogin,
                  (SELECT count(*) FROM pg_auth_members m WHERE m.member = r.oid)),
           ', ' ORDER BY r.rolname)
    INTO bad
    FROM pg_roles r
   WHERE r.rolname IN ('__DASH_ROLE__', '__BR_ROLE__')
     AND (r.rolsuper OR r.rolinherit OR NOT r.rolcanlogin
          OR EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid));
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'ロール属性が想定外(super/inherit/継承メンバーシップは不可・LOGIN 必須): %', bad;
  END IF;

  -- 4.3 読取専用ロールに非 SELECT 権限が無いこと(列レベル GRANT を含む — C-3)
  SELECT count(*) INTO n FROM ryza_gate_grants
   WHERE grantee = '__DASH_ROLE__' AND privilege_type <> 'SELECT';
  IF n <> 0 THEN
    RAISE EXCEPTION '読取専用ロール __DASH_ROLE__ に非 SELECT 権限が % 件ある(列レベル GRANT を含む)', n;
  END IF;

  -- 4.4 秘密テーブルへの権限が無いこと(ops.discord_webhooks — 0017 冒頭)
  SELECT count(*) INTO n FROM ryza_gate_grants
   WHERE grantee = '__DASH_ROLE__'
     AND table_schema || '.' || table_name IN ('ops.discord_webhooks');
  IF n <> 0 THEN
    RAISE EXCEPTION '読取専用ロール __DASH_ROLE__ が秘密テーブル ops.discord_webhooks に権限を持つ(% 件)', n;
  END IF;

  -- 4.5 役員室ロールの ops 権限が対象3表ちょうどであること。
  --     上の GRANT は to_regclass ガード付きで 0020 / 0024 未適用の DB では黙って
  --     スキップされるため、まず**対象表が実在すること自体**を要求する(C-4)。実在数を
  --     そのまま期待値にすると、未適用の DB(実在 0・GRANT 0)が「一致」と判定され、
  --     役員室が何も書けないダッシュボードを正常として本番化してしまう。
  expected_ops_tables := (to_regclass('ops.org_icon_overrides') IS NOT NULL)::int
                       + (to_regclass('ops.org_icon_override_log') IS NOT NULL)::int
                       + (to_regclass('ops.dev_chat') IS NOT NULL)::int;
  IF expected_ops_tables <> 3 THEN
    RAISE EXCEPTION
      '役員室の書込先(ops.org_icon_overrides / ops.org_icon_override_log / ops.dev_chat)が揃っていない(実在 % / 期待 3)。migrations 0020 または 0024 が未適用の DB へデプロイしようとしている',
      expected_ops_tables;
  END IF;
  SELECT count(DISTINCT table_name) INTO n FROM ryza_gate_grants
   WHERE grantee = '__BR_ROLE__' AND table_schema = 'ops';
  IF n <> 3 THEN
    RAISE EXCEPTION
      '役員室ロールの ops 権限表数が想定外(期待 3, 実際 %)。GRANT が効いていないか余計な表に広がっている', n;
  END IF;

  -- 4.6 想定外の ops 表への権限が無いこと(trading_state / flags / discord_webhooks 等)
  SELECT count(*) INTO n FROM ryza_gate_grants
   WHERE grantee = '__BR_ROLE__' AND table_schema = 'ops'
     AND table_name NOT IN ('org_icon_overrides', 'org_icon_override_log', 'dev_chat');
  IF n <> 0 THEN
    RAISE EXCEPTION '役員室ロールが ops の想定外テーブルに権限を持つ(% 件・列レベル GRANT を含む)', n;
  END IF;

  -- 4.7 履歴表は追記オンリー(非 INSERT 権限を持たない)
  SELECT count(*) INTO n FROM ryza_gate_grants
   WHERE grantee = '__BR_ROLE__' AND table_schema = 'ops'
     AND table_name = 'org_icon_override_log' AND privilege_type <> 'INSERT';
  IF n <> 0 THEN
    RAISE EXCEPTION 'ops.org_icon_override_log への非 INSERT 権限がある(% 件・追記オンリー違反)', n;
  END IF;

  -- 4.8 開発室 ops.dev_chat に**表レベル**の書込権限が無いこと(独立役員審査 重大-1)。
  --     表レベル INSERT があると created_at の遡及・relayed_at の事前設定・inserted_by の
  --     詐称ができる。0024 のガードトリガは INSERT に発火しないため、止められるのは権限だけ。
  SELECT count(*) INTO n FROM information_schema.role_table_grants
   WHERE grantee = '__BR_ROLE__' AND table_schema = 'ops'
     AND table_name = 'dev_chat' AND privilege_type <> 'SELECT';
  IF n <> 0 THEN
    RAISE EXCEPTION 'ops.dev_chat に表レベルの非 SELECT 権限がある(% 件)。INSERT は列レベル (sender, body) に限ること', n;
  END IF;

  -- 4.9 開発室の書込可能列が sender / body の INSERT ちょうど2件であること。
  --     0 なら 0024 未適用で GRANT がスキップされ、多ければ証跡列へ書ける。
  SELECT count(*) INTO n FROM information_schema.role_column_grants
   WHERE grantee = '__BR_ROLE__' AND table_schema = 'ops'
     AND table_name = 'dev_chat' AND privilege_type <> 'SELECT';
  IF n <> 2 THEN
    RAISE EXCEPTION 'ops.dev_chat の書込可能列数が想定外(期待 2, 実際 %)', n;
  END IF;
  SELECT count(*) INTO n FROM information_schema.role_column_grants
   WHERE grantee = '__BR_ROLE__' AND table_schema = 'ops'
     AND table_name = 'dev_chat' AND privilege_type <> 'SELECT'
     AND (privilege_type <> 'INSERT' OR column_name NOT IN ('sender', 'body'));
  IF n <> 0 THEN
    RAISE EXCEPTION 'ops.dev_chat に想定外の列権限がある(% 件 — created_at/relayed_at/inserted_by/id への書込)', n;
  END IF;

  RAISE NOTICE 'ロール権限ゲート: 全項目 OK(4.1〜4.9)';
END
$ryza_gate$;
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
# pg_hba の検査ロジックは ops/lib/pg_hba_check.sh に切り出し、base64 で VM へ運んで
# eval で読み込む(ヒアドキュメントに直書きするとローカルシェルが $1 等を展開して壊れる)。
# 同じ関数を CI が tests/ops/test_deploy_guards.py から直接叩いて検証している。
PG_HBA_LIB_B64="$(base64 < "${ROOT}/ops/lib/pg_hba_check.sh" | tr -d '\n')"

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
#     判定は**アドレス列のみ**(ops/lib/pg_hba_check.sh)。行全体の文字列マッチは
#     コメントや DB 名の 'localhost' で誤判定するため使わない(再審査 次回 PR 対応)。
eval "\$(printf '%s' '${PG_HBA_LIB_B64}' | base64 -d)"
# eval が失敗しても set -e は素通りしうる(base64 の出力が空でも eval は成功する)。
# 関数が定義されていないまま進むと以降の検査呼び出しが「コマンドなし」で終わり、
# 統制が沈黙する。読み込めたことをここで明示的に確かめる(第3回審査 C-2)。
declare -F pg_hba_unexpected_lines >/dev/null || exit 1
declare -F pg_hba_has_line >/dev/null || exit 1
declare -F pg_hba_guard >/dev/null || exit 1
DESIRED_HBA="host    ${DB_NAME}    ${DB_ROLE},${BOARDROOM_ROLE}    ${SUBNET_CIDR}    scram-sha-256"
# pg_hba_guard は 0=想定外なし / 1=想定外あり / その他=検査自体の失敗 を3分岐し、
# 後ろ2つを中断に倒す(検査できない状態を「安全」と混同しない)。
pg_hba_guard "\${HBA}" "\${DESIRED_HBA}" || exit 1
# 追記の要否は検査と**同一の正規化**で判定する(空白ゆらぎでの重複追記を防ぐ — C-7)。
if ! pg_hba_has_line "\${HBA}" "\${DESIRED_HBA}"; then
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

# ── 12. 公開バインディングの検査と除去(重大-3・再審査 条件1)──────────────────────
# 実装は ops/lib/deploy-guards.sh(取得失敗を「公開なし」と混同しない・除去後に再取得して
# 確認する)。tests/ops/test_deploy_guards.py が中断挙動を検証している。
echo "-- 公開バインディング(allUsers / allAuthenticatedUsers)を検査"
guard_service_public_bindings "${SERVICE}" "${PROJECT}" "${REGION}" || exit 1

# ── 12.5 プロジェクトレベル IAM の公開バインディング(再審査 条件1)────────────────
echo "-- プロジェクトレベル IAM の公開バインディングを検査"
guard_project_public_bindings "${PROJECT}" || exit 1

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
