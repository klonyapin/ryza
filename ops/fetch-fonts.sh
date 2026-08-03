#!/usr/bin/env bash
# ops/fetch-fonts.sh — Noto Sans JP を取得・サブセットして dashboard/static/fonts/ に置く。
#
# 代表指示 2026-08-03(DADS 準拠のデザイン改修)。DADS のタイポグラフィ規定は
# Noto Sans JP(SIL Open Font License 1.1)で、Ryza ダッシュボードはこれを
# **self-host** する。Google Fonts の CDN から配信しないのは、閲覧のたびに代表の
# IP・User-Agent が第三者へ送られるためで、完全非公開ツールとして避ける
# (docs/research/dads-streamlit-application.md §5)。
#
# **このスクリプトはネットワークからバイナリを取得する。** 生成物(WOFF2 と OFL.txt)は
# OFL 1.1 の下で再配布可能なのでリポジトリにコミットしてよい(ライセンス全文を同梱する
# ことが OFL の条件で、本スクリプトが OFL.txt も一緒に取得する)。
#
# 実行(リポジトリルートから):
#     ./ops/fetch-fonts.sh
#     git add dashboard/static/fonts && git commit
#
# 依存: curl と uv だけ。fonttools / brotli は uv run --with で一時環境に入れるため、
# リポジトリの依存(pyproject.toml)は増やさない。
#
# **取得物は再現性を持たせて固定する**(独立役員審査 重要-3)。代表のブラウザが
# パースするバイナリを、実行のたびに違いうる入力から作らない。3段で固定する:
#
#   1. **commit SHA 固定**: 取得元を可変 ref(main)ではなく特定コミットにする。
#      git は内容アドレスなので、同じコミットの同じパスは未来も同一バイトである。
#   2. **git blob SHA-1 検証**: 上に加えて、落ちてきたファイル自体を検証する(経路上の
#      改竄・切断・プロキシの取り違えを検出)。期待値は GitHub API が返す blob SHA で、
#      `git hash-object` と同じ `sha1("blob <size>\0" + content)`。SHA-256 ではなく
#      これを使うのは、**バイナリを一度も取得せずに権威ある期待値を得られる唯一の
#      ダイジェスト**だからである(API がツリーと一緒に返す)。生成物側の SHA-256 は
#      下の SHA256SUMS に記録し、コミット後のバイナリはそちらで検証できる。
#   3. **加工系の固定**: fonttools / brotli を `==` で固定。サブセッタが変われば
#      出力バイトも変わる。
#
# 取得元(2026-08-03 時点・google/fonts):
#   commit: 2796410152d4f9524b68ed46e69c1b60f8e0f7c3(2026-07-31)
#   フォント: ofl/notosansjp/NotoSansJP[wght].ttf(可変フォント・9,589,900 バイト)
#   ライセンス: ofl/notosansjp/OFL.txt(4,388 バイト)
# 更新するときは3つ(commit・blob SHA・サイズ)を同時に取り直すこと:
#   gh api repos/google/fonts/commits/main --jq .sha
#   gh api repos/google/fonts/contents/ofl/notosansjp --jq \
#     '.[] | select(.name|test("ttf$|OFL.txt$")) | .name + " " + .sha + " " + (.size|tostring)'
#
# 処理: 可変フォント → wght=400 / 700 の静的インスタンス化 → JIS X 0208 サブセット →
# WOFF2 圧縮。DADS はウェイトを 400/700 の2段しか使わないため他のウェイトは作らない。
#
# **サブセットの範囲**: JIS X 0208(第1・第2水準の 6,879 漢字 + 記号・かな 524 字)+
# ASCII + Latin-1 + 全角英数記号 + 罫線・矢印。全 CJK 統合漢字(U+4E00-9FFF・約2万字)を
# 入れると 1 ウェイトで 4 MB を超え、DADS 準拠のために表示が遅くなる本末転倒になる。
# サブセット外の稀用漢字はフォールバック(Hiragino Sans 等・.streamlit/config.toml の
# theme.font)で描画される — 字形が混ざるが読めなくなることはない。
# RYZA_FONT_JIS_LEVEL=1 を渡すと第1水準までに絞る(さらに約 40% 小さくなる)。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 配信できるのは **エントリポイント(dashboard/app.py)と同じ階層の static/** 配下だけ
# (Streamlit の server.enableStaticServing の仕様)。dashboard/fonts/ に置いても
# app/static/... の URL では配信されない。
OUT="$ROOT/dashboard/static/fonts"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ── 固定した入力(更新時は上のコメントの手順で3つ同時に取り直す)─────────────
FONTS_COMMIT="2796410152d4f9524b68ed46e69c1b60f8e0f7c3"
BASE="https://raw.githubusercontent.com/google/fonts/${FONTS_COMMIT}/ofl/notosansjp"
TTF_BLOB_SHA1="cdd8f083c1f5928ff3361f8cda4d3fc9462cbe89"
OFL_BLOB_SHA1="1c9f43281b8f216c5461fe9ac729afbade7724e4"
# 加工系の固定。バージョンが動くと同じ入力から違う WOFF2 が出る。
FONTTOOLS_VERSION="4.63.0"
BROTLI_VERSION="1.2.0"
#: 合計がこれを超えたら警告する(初回描画のダウンロード量。設計リード指示の目安)。
MAX_TOTAL_KB=3072

# git blob SHA-1 = sha1("blob <バイト数>\0" + 内容)。git が無くても計算できるよう
# shasum で組み立てる(git があれば `git hash-object` と同じ値になる)。
verify_blob() {
    local path="$1" expected="$2" label="$3" actual size
    size=$(wc -c <"$path" | tr -d ' ')
    actual=$( { printf 'blob %s\0' "$size"; cat "$path"; } | shasum -a 1 | cut -d' ' -f1)
    if [ "$actual" != "$expected" ]; then
        echo "中断: $label のダイジェストが一致しない(取得物を破棄した)" >&2
        echo "  期待 (git blob sha1): $expected" >&2
        echo "  実際 (git blob sha1): $actual   size=$size" >&2
        echo "  取得元: $BASE (commit ${FONTS_COMMIT})" >&2
        exit 1
    fi
    echo "   検証 OK: $label($size バイト)"
}

mkdir -p "$OUT"

echo "== 取得: NotoSansJP[wght].ttf(可変フォント・9,589,900 バイト)"
curl -fsSL "$BASE/NotoSansJP%5Bwght%5D.ttf" -o "$WORK/NotoSansJP.ttf"
verify_blob "$WORK/NotoSansJP.ttf" "$TTF_BLOB_SHA1" "NotoSansJP[wght].ttf"

echo "== 取得: OFL.txt(ライセンス全文 — 再配布の条件)"
# **$WORK へ落としてから配置する**(独立役員審査 重要-3)。$OUT へ直接書くと、
# 転送が途中で切れた場合に**切り詰められたライセンス**が残る。OFL 全文の同梱は
# 再配布の条件なので、欠けたライセンスが残る状態は「無い」より悪い(あるように
# 見えて条件を満たさない)。検証を通ったものだけを mv で原子的に置く。
curl -fsSL "$BASE/OFL.txt" -o "$WORK/OFL.txt"
verify_blob "$WORK/OFL.txt" "$OFL_BLOB_SHA1" "OFL.txt"
mv "$WORK/OFL.txt" "$OUT/LICENSE-OFL.txt"

echo "== サブセット文字集合を生成(JIS X 0208。オフラインで euc_jp コーデックから列挙)"
uv run --quiet --no-project python - "$WORK/charset.txt" <<'PY'
import sys

# JIS X 0208 の区点を euc_jp で往復デコードして Unicode 文字を得る(外部データ不要)。
#   1-15 区: 記号・数字・英字・ひらがな・カタカナ・ギリシャ・キリル・罫線
#   16-47 区: 第1水準漢字 / 48-84 区: 第2水準漢字
import os

level = os.environ.get("RYZA_FONT_JIS_LEVEL", "2")
rows = list(range(1, 48)) + (list(range(48, 85)) if level != "1" else [])
chars = set()
for ku in rows:
    for ten in range(1, 95):
        try:
            chars.add(bytes([0xA0 + ku, 0xA0 + ten]).decode("euc_jp"))
        except UnicodeDecodeError:
            pass
# ASCII + Latin-1 補助(欧文・記号)、一般句読点、矢印、幾何学模様(▲▼ など符号の代替表示)、
# 全角形。ダッシュボードは ▲▼⛔✅⚠ を「色だけに頼らない」表示に使う(絵文字は OS 側で描画)。
for start, end in ((0x20, 0x7E), (0xA0, 0xFF), (0x2000, 0x206F), (0x2190, 0x21FF),
                   (0x25A0, 0x25FF), (0x3000, 0x30FF), (0xFF00, 0xFFEF)):
    chars.update(chr(cp) for cp in range(start, end + 1))
with open(sys.argv[1], "w", encoding="utf-8") as f:
    f.write("".join(sorted(chars)))
print(f"   文字数: {len(chars)}(JIS 水準={level})")
PY

for spec in "Regular:400" "Bold:700"; do
    name="${spec%%:*}"
    wght="${spec##*:}"
    echo "== インスタンス化 wght=$wght → サブセット → WOFF2($name)"
    # 生成も $WORK 経由。失敗したときに壊れた/古い WOFF2 が $OUT に残ると、
    # ブラウザは 404 ではなくパースエラーになりフォールバックが効かない。
    uv run --quiet --no-project \
        --with "fonttools==${FONTTOOLS_VERSION}" --with "brotli==${BROTLI_VERSION}" python - \
        "$WORK/NotoSansJP.ttf" "$wght" "$WORK/charset.txt" "$WORK/NotoSansJP-$name.woff2" <<'PY'
import sys

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

src, wght, charset_path, dst = sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4]
font = instancer.instantiateVariableFont(TTFont(src), {"wght": wght}, inplace=False)
text = open(charset_path, encoding="utf-8").read()

options = subset.Options()
options.flavor = "woff2"
options.desubroutinize = True
options.layout_features = ["*"]        # 縦組み・合字などの機能は落とさない
options.notdef_outline = True
options.drop_tables += ["DSIG"]
subsetter = subset.Subsetter(options=options)
subsetter.populate(text=text)
subsetter.subset(font)
font.flavor = "woff2"
font.save(dst)
PY
    mv "$WORK/NotoSansJP-$name.woff2" "$OUT/NotoSansJP-$name.woff2"
done

# 生成物の SHA-256 を残す。コミット後は「リポジトリにある WOFF2 が、この手順で
# 作られたものと同一か」をネットワーク無しで検証できる(shasum -c SHA256SUMS)。
( cd "$OUT" && shasum -a 256 NotoSansJP-*.woff2 LICENSE-OFL.txt > "$WORK/SHA256SUMS" )
mv "$WORK/SHA256SUMS" "$OUT/SHA256SUMS"

echo
total_kb=0
for f in "$OUT"/NotoSansJP-*.woff2; do
    kb=$(( ( $(wc -c <"$f") + 1023 ) / 1024 ))
    total_kb=$(( total_kb + kb ))
    printf '  %-40s %6s KB\n' "$(basename "$f")" "$kb"
done
printf '  %-40s %6s KB\n' "合計" "$total_kb"

if [ "$total_kb" -gt "$MAX_TOTAL_KB" ]; then
    echo
    echo "警告: 合計が ${MAX_TOTAL_KB} KB を超えた。RYZA_FONT_JIS_LEVEL=1 で第1水準に絞るか、" >&2
    echo "      Bold を落として太字を合成に任せることを検討する。" >&2
fi

echo
echo "完了。生成物は $OUT"
echo "OFL 1.1 の条件によりライセンス全文(LICENSE-OFL.txt)を必ず一緒にコミットすること。"
