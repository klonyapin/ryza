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

## 再現性(独立役員審査 重要-3)

代表のブラウザがパースするバイナリを、実行のたびに変わりうる入力から作らない。3段で固定する:

1. **取得元を commit SHA 固定**。`google/fonts` の可変 ref `main` ではなく特定コミットを指す。git は内容アドレスなので、同じコミットの同じパスは未来も同一バイトである
2. **git blob SHA-1 で検証**。落ちてきたファイル自体を検証し、経路上の改竄・切断・プロキシの取り違えを検出する。期待値は GitHub API がツリーと一緒に返す blob SHA(`git hash-object` と同じ `sha1("blob <size>\0" + content)`)。SHA-256 ではなくこれを使うのは、**バイナリを一度も取得せずに権威ある期待値を得られる唯一のダイジェスト**だから
3. **加工系を `==` 固定**。`fonttools` / `brotli` はサブセッタなので、バージョンが動けば同じ入力から違う WOFF2 が出る

生成後は `SHA256SUMS` が置かれ、コミット済みのバイナリをネットワーク無しで検証できる:

```sh
cd dashboard/static/fonts && shasum -c SHA256SUMS
```

検証に失敗した取得物は破棄して中断する。ライセンスと WOFF2 は作業ディレクトリ経由で
配置するため、**転送が途中で切れても切り詰められたファイルがここに残ることはない**
(欠けた OFL 全文は「無い」より悪い — あるように見えて再配布の条件を満たさないため)。

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
