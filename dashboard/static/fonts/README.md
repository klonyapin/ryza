# dashboard/static/fonts — self-host する Noto Sans JP

DADS(デジタル庁デザインシステム)のタイポグラフィ規定フォント **Noto Sans JP** の
WOFF2 サブセットを置く場所。`.streamlit/config.toml` の `[[theme.fontFaces]]` が
`app/static/fonts/NotoSansJP-{Regular,Bold}.woff2` として参照する。

## なぜここか(`dashboard/fonts/` ではない)

Streamlit の静的配信(`server.enableStaticServing = true`)が公開するのは
**エントリポイントと同じ階層の `static/` ディレクトリ配下だけ**である。
エントリポイントは `dashboard/app.py` なので、配信できるのは `dashboard/static/` のみ。
`dashboard/fonts/` に置いても `app/static/...` の URL では 404 になる。

## なぜ self-host か

Google Fonts の CDN から配信すると、ダッシュボードを開くたびに代表の IP アドレスと
User-Agent が第三者(Google)へ送られる。本ダッシュボードは IAP 許可リストが代表1名の
完全非公開ツールであり、この情報漏出を受け入れる理由がない
(`docs/research/dads-streamlit-application.md` §5)。

## 取得方法

```sh
./ops/fetch-fonts.sh          # curl + uv だけで完結(リポジトリ依存は増えない)
git add dashboard/static/fonts && git commit
```

スクリプトの処理は「可変フォント取得 → wght=400/700 でインスタンス化 →
JIS X 0208(第1・第2水準)サブセット → WOFF2 圧縮 → OFL.txt 同梱」。
サイズが 3 MB を超える場合は `RYZA_FONT_JIS_LEVEL=1` で第1水準に絞る。

## ライセンス

**SIL Open Font License 1.1**。再配布・改変(サブセット化を含む)・埋め込みが許可され、
リポジトリへのコミットも許される。条件は **ライセンス全文の同梱**で、
`ops/fetch-fonts.sh` が `LICENSE-OFL.txt` を一緒に取得する。
`LICENSE-OFL.txt` を消して WOFF2 だけを残すことは OFL 違反になる。

なお OFL は「予約フォント名(Reserved Font Name)」の付いたフォントの改変版に元の名前を
使うことを禁じるが、Noto Sans JP は予約フォント名を持たないため、サブセット後も
`Noto Sans JP` の名前で参照してよい。

## 未取得のときの挙動

WOFF2 が無くても**アプリは壊れない**。`app/static/fonts/...` が 404 になり、
`.streamlit/config.toml` の `theme.font` に並べたフォールバック
(Hiragino Sans → Hiragino Kaku Gothic ProN → Meiryo → sans-serif)で描画される。
字形は DADS 規定と異なるが、サイズ・行間・色のトークンはすべて有効なままである。
