#!/usr/bin/env bash
#
# sql_ident_check.sh — SQL 識別子として使う env の入口検証(A-12 F-8・pass4-1)。
#
# 本ファイルは保護領域 deploy_path の一部(ops/deploy-dashboard.sh から使われる)。
# ここを緩めることは SQL 埋め込みの入口統制を緩めることと同義。
#
# 対象は「ロール名を SQL 識別子として `"…"` で囲んで埋め込む env」。deploy-dashboard.sh は
# Python ヒアドキュメント内で env(RYZA_DASH_ROLE / RYZA_BR_ROLE / RYZA_OWNER)を
# `.replace()` で SQL テンプレートに差し込む。operator 制御の env であり悪用経路は
# 限定的(裁定 §3 F-8)だが、タイポや `"` / 空白 / セミコロン等の混入で `CREATE ROLE
# "foo"; DROP ROLE ryza --"` のような SQL に育つ経路を、入口で止める。
#
# 検証規則:
#   - 正規表現 `^[a-z_][a-z0-9_]*$` に一致(PostgreSQL の unquoted 識別子文法)
#   - 長さ 1..63 バイト(NAMEDATALEN 上限)
#   両方満たさなければ非ゼロ終了。stderr に「どの env が」「何の理由で」不合格かを出す。
#
# ASCII 小文字+数字+アンダースコアに絞る判断:
#   - PostgreSQL の識別子は `"…"` で囲めば任意文字を含められるが、ここで受けるのは
#     「Ryza の運用ロール名(ryza_dashboard / ryza_boardroom / ryza)」だけであり、
#     大文字・非 ASCII・記号を許す理由が無い。狭くしても運用者が困らない範囲で、
#     クォート漏れ・見た目衝突(例: `Ryza` と `ryza`)・非 ASCII の見え方の差を全て
#     入口で除外できる。逆に unquoted 識別子として PostgreSQL が受ける全ての文字列
#     (`_$0` など)を許すと、正しさの検査が SQL 側の folding 規則に依存し、シェルで
#     一貫して検査できなくなる。
#
# stdout に受け取った値は出さない(制御文字混入時の端末破壊を避ける)。診断は stderr の
# サニタイズ済み表現(printf '%q')で出す。呼び出し側が受け取るのは終了コードのみ。
#
# 使い方:
#   . ops/lib/sql_ident_check.sh
#   assert_sql_ident RYZA_DASH_ROLE "${DB_ROLE}" || exit 1
#   assert_sql_ident RYZA_BR_ROLE   "${BOARDROOM_ROLE}" || exit 1
#   assert_sql_ident RYZA_OWNER     "${DB_OWNER}" || exit 1
#
# 失敗は `return`(exit しない)。中断は呼び出し側が `|| exit 1` で明示する。
# tests/ops/test_sql_ident_check.py が「実際に中断すること」を検証する。

# 単一の env を検査する。
#   $1 = env 変数名(診断メッセージ用のラベル)
#   $2 = 値
# 戻り値 0 … 合格 / 1 … 不合格(理由は stderr)
assert_sql_ident() {
  local name="$1" value="${2-}"

  if [ "$#" -lt 2 ]; then
    echo "ERROR: assert_sql_ident 呼び出しに値が渡されていない(name=${name}・呼び出し側のバグ)" >&2
    return 1
  fi

  # 空文字は即失敗。`"${VAR-}"` で unset にもここに到達する経路を作っている。
  if [ -z "${value}" ]; then
    echo "ERROR: SQL 識別子として使う env '${name}' が空(未設定または空文字)。" >&2
    return 1
  fi

  # 長さ(バイト単位。ASCII のみを許すので byte==char)。PostgreSQL の識別子上限は
  # 63 バイト(NAMEDATALEN=64 の末尾 NUL を除く)。超過を SQL 側で切り詰めさせない。
  local byte_len
  byte_len="$(LC_ALL=C printf '%s' "${value}" | wc -c | tr -d ' ')"
  if [ "${byte_len}" -gt 63 ]; then
    printf 'ERROR: SQL 識別子 %s が長すぎる(%d バイト・上限 63)。値=%q\n' \
      "${name}" "${byte_len}" "${value}" >&2
    return 1
  fi

  # 文法。`[[ =~ ]]` は locale 依存を避けるため LC_ALL=C の下で判定する。
  # ここが唯一の合否条件で、これを緩める場合は必ず tests/ops/test_sql_ident_check.py の
  # 前提を見直すこと(現状は「ASCII 小英字+数字+アンダースコアで先頭が小英字/_」)。
  if ! LC_ALL=C awk -v v="${value}" 'BEGIN { exit (v ~ /^[a-z_][a-z0-9_]*$/) ? 0 : 1 }'; then
    printf 'ERROR: SQL 識別子 %s の形式が不正(要件: ^[a-z_][a-z0-9_]*$)。値=%q\n' \
      "${name}" "${value}" >&2
    return 1
  fi

  return 0
}
