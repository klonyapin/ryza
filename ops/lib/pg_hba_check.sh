#!/usr/bin/env bash
#
# pg_hba_check.sh — pg_hba.conf の host 行を**フィールド単位**で検査する。
#
# 本ファイルは保護領域 deploy_path の一部(ops/deploy-dashboard.sh から使われる)。
# ここを緩めることは PostgreSQL の接続統制を緩めることと同義。
#
# 独立役員 再審査(docs/reviews/dashboard-deploy-independent-review.md「次回 PR 対応」)の
# 「pg_hba 検査の CIDR 列限定」への対応。行全体の文字列マッチをやめた理由は2つある。
#   (a) 偽陰性: `host all all 0.0.0.0/0 md5 # localhost 用` のように**コメントや
#       データベース名**に 'localhost' を含むだけで「安全」と誤判定し、全世界から
#       接続できる行を見逃す。pg_hba は**先勝ち**なので、広い行が1本でも上にあれば
#       deploy-dashboard.sh が追記する限定行は無効化される。
#   (b) 偽陽性: `# host all all 0.0.0.0/0 trust` のように**コメントアウト済み**の行を
#       有効な行と誤判定してデプロイを止める。
#
# 検査対象は host / hostssl / hostnossl / hostgssenc / hostnogssenc 行のアドレス列
# (第4フィールド。必要なら第5フィールドの netmask も見る)だけ。local 行は
# Unix ドメインソケット接続でネットワーク到達性を持たないため対象外。
#
# ループバックとみなすのは 127.0.0.1/32・::1/128・localhost・および address+netmask の
# 2列表記でこれらに等しいものだけ。**samehost は含めない**(第3回審査 C-6): samehost は
# 「サーバ自身の**全 IP アドレス**」に一致し、VM の VPC 内部 IP を含む。ループバック
# 限定ではないため、`samehost` の広い行を安全と扱うと検査の意味が薄れる。samenet
# (サブネット全体)も同様に想定外。
#
# include 系の指令は**検査不能**として想定外扱いにする(参照先の内容をここでは
# 読めず、「見えないから安全」とみなすと検査が沈黙するため — 再審査 条件1 と同じ思想)。
#
# 使い方(呼び出し側は pg_hba_guard を使うこと。3分岐の中断判定込み):
#   . ops/lib/pg_hba_check.sh
#   pg_hba_guard /etc/postgresql/17/main/pg_hba.conf "${DESIRED}" || exit 1
#   pg_hba_has_line /etc/postgresql/17/main/pg_hba.conf "${DESIRED}" || 追記する
#
#   DESIRED は本スクリプトが追記する1行。空白の詰め方が違っても一致させる(正規化して
#   比較)。DESIRED と正規化一致する行だけが「広いアドレスでも想定内」。アドレスが
#   同じでも database/user 列が違う行(例: `all all`)は想定外として報告する。

# 行を正規化する awk 断片(コメント除去+空白圧縮)。検査と追記判定で**同一の**
# 正規化を使う(第3回審査 C-7)。ここがずれると、空白ゆらぎのある既存行を
# 「想定外ではないが未追記」と判定して同義の行を重複追記してしまう。
_PG_HBA_AWK_COMMON='
    function norm(s) {
      sub(/#.*/, "", s)
      gsub(/[ \t]+/, " ", s); sub(/^ /, "", s); sub(/ $/, "", s); return s
    }
'

# 想定外の行を stdout に出力する。
#   戻り値 0 … 想定外なし
#   戻り値 1 … 想定外あり(内容は stdout)
#   その他   … 検査自体の失敗(ファイルが読めない・awk の異常終了等)
pg_hba_unexpected_lines() {  # $1=pg_hba.conf のパス $2=許可する1行(desired)
  if [ ! -r "$1" ]; then
    echo "pg_hba_unexpected_lines: pg_hba.conf を読めない: $1" >&2
    return 2
  fi
  awk -v desired="$2" "${_PG_HBA_AWK_COMMON}"'
    # ループバック限定と言えるアドレスだけを安全とみなす。samehost(サーバの全 IP)・
    # samenet(サブネット全体)・ホスト名・0.0.0.0/0 は安全ではない。
    function is_loopback(a, m,    al) {
      al = tolower(a)
      if (al == "localhost") return 1
      if (al == "127.0.0.1/32" || al == "::1/128") return 1
      if (al == "127.0.0.1" && m == "255.255.255.255") return 1
      if (al == "::1" && tolower(m) == "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff") return 1
      return 0
    }
    BEGIN { dn = norm(desired); bad = 0 }
    {
      line = norm($0)
      if (line == "") next
      n = split(line, f, " ")
      type = tolower(f[1])
      if (type ~ /^include/) {       # include / include_if_exists / include_dir
        print "[検査不能: include 指令のため参照先を確認できない] " $0
        bad = 1
        next
      }
      if (type !~ /^host(ssl|nossl|gssenc|nogssenc)?$/) next   # local 行等は対象外
      if (line == dn) next                                     # 本スクリプトが追記する行
      addr = (n >= 4) ? f[4] : ""
      mask = (n >= 5) ? f[5] : ""
      if (is_loopback(addr, mask)) next
      print $0
      bad = 1
    }
    END { exit (bad ? 1 : 0) }
  ' "$1"
}

# desired と**正規化一致**する有効行が既に存在するか(追記の要否判定 — C-7)。
#   戻り値 0 … 存在する(追記不要) / 1 … 存在しない / その他 … 検査自体の失敗
pg_hba_has_line() {  # $1=pg_hba.conf のパス $2=desired
  if [ ! -r "$1" ]; then
    echo "pg_hba_has_line: pg_hba.conf を読めない: $1" >&2
    return 2
  fi
  awk -v desired="$2" "${_PG_HBA_AWK_COMMON}"'
    BEGIN { dn = norm(desired); found = 0 }
    { if (norm($0) == dn) found = 1 }
    END { exit (found ? 0 : 1) }
  ' "$1"
}

# 検査を実行し、終了コードを**3分岐**して中断可否を返す(第3回審査 C-2)。
#   0 … 想定外なし(続行してよい)
#   1 … 想定外あり、または**検査自体が失敗**(いずれも呼び出し側は中断すること)
# 要点は「検査自体の失敗」を 0 と混同しないこと。awk が落ちた・ファイルが読めない
# といった状況を「想定外なし」と同じ扱いにすると、検査が沈黙して広い行を見逃す。
pg_hba_guard() {  # $1=pg_hba.conf のパス $2=desired
  local out rc=0
  out="$(pg_hba_unexpected_lines "$1" "$2")" || rc=$?
  case "${rc}" in
    0)
      echo "-- pg_hba: 想定外の non-localhost host 行なし"
      return 0
      ;;
    1)
      echo "ERROR: pg_hba.conf に想定外の non-localhost host 行がある(先勝ち規則で本設定が無効化される)。" >&2
      printf '%s\n' "${out}" >&2
      echo "       内容を確認し、不要な行を削除してから再実行すること(自動削除はしない)。" >&2
      return 1
      ;;
    *)
      echo "ERROR: pg_hba.conf の検査自体が失敗した(終了コード ${rc})。" >&2
      printf '%s\n' "${out}" >&2
      echo "       検査できない状態を『想定外なし』とみなすと統制が沈黙するため中断する。" >&2
      return 1
      ;;
  esac
}
