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
# include 系の指令は**検査不能**として想定外扱いにする(参照先の内容をここでは
# 読めず、「見えないから安全」とみなすと検査が沈黙するため — 再審査 条件1 と同じ思想)。
#
# 使い方:
#   . ops/lib/pg_hba_check.sh
#   UNEXPECTED="$(pg_hba_unexpected_lines /etc/postgresql/17/main/pg_hba.conf "${DESIRED}" || true)"
#   [ -n "${UNEXPECTED}" ] && exit 1
#
#   第2引数 DESIRED は本スクリプトが追記する1行。空白の詰め方が違っても一致させる
#   (正規化して比較)。DESIRED と完全一致する行だけが「広いアドレスでも想定内」。
#   アドレスが同じでも database/user 列が違う行(例: `all all`)は想定外として報告する。

# 想定外の行を stdout に出力する。1行でも見つかれば戻り値 1、無ければ 0。
pg_hba_unexpected_lines() {  # $1=pg_hba.conf のパス $2=許可する1行(desired)
  awk -v desired="$2" '
    function norm(s) {
      gsub(/[ \t]+/, " ", s); sub(/^ /, "", s); sub(/ $/, "", s); return s
    }
    # ループバック限定と言えるアドレスだけを安全とみなす。samenet(サブネット全体)や
    # ホスト名・0.0.0.0/0 は安全ではない。
    function is_loopback(a, m,    al) {
      al = tolower(a)
      if (al == "samehost" || al == "localhost") return 1
      if (al == "127.0.0.1/32" || al == "::1/128") return 1
      if (al == "127.0.0.1" && m == "255.255.255.255") return 1
      if (al == "::1" && tolower(m) == "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff") return 1
      return 0
    }
    BEGIN { dn = norm(desired); bad = 0 }
    {
      raw = $0
      sub(/#.*/, "", raw)            # コメント除去(行全体コメントはここで空になる)
      line = norm(raw)
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
