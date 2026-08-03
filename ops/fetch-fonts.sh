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
# 取得元(2026-08-03 時点):
#   フォント: https://github.com/google/fonts/raw/main/ofl/notosansjp/NotoSansJP%5Bwght%5D.ttf
#             (可変フォント・約 5.6 MB)
#   ライセンス: https://github.com/google/fonts/raw/main/ofl/notosansjp/OFL.txt(約 4 KB)
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

BASE="https://github.com/google/fonts/raw/main/ofl/notosansjp"
#: 合計がこれを超えたら警告する(初回描画のダウンロード量。設計リード指示の目安)。
MAX_TOTAL_KB=3072

mkdir -p "$OUT"

echo "== 取得: NotoSansJP[wght].ttf(可変フォント・約 5.6 MB)"
curl -fsSL "$BASE/NotoSansJP%5Bwght%5D.ttf" -o "$WORK/NotoSansJP.ttf"

echo "== 取得: OFL.txt(ライセンス全文 — 再配布の条件)"
curl -fsSL "$BASE/OFL.txt" -o "$OUT/LICENSE-OFL.txt"

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
    uv run --quiet --no-project --with "fonttools>=4.53" --with brotli python - \
        "$WORK/NotoSansJP.ttf" "$wght" "$WORK/charset.txt" "$OUT/NotoSansJP-$name.woff2" <<'PY'
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
done

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
