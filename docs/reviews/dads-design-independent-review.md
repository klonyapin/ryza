# DADS デザイン改修 独立役員審査(2026-08-03)

対象: `.streamlit/config.toml` / `dashboard/dads.py`(新規)/ `st.navigation` 再構成 / `ops/fetch-fonts.sh`(新規)/ `viz.py` / `Dockerfile`。**判定: 条件付き承認**(重要-1〜3・中-5 をマージ前に是正)。

- **重要-1**: `dads.py` が自認する最大リスク(Streamlit 更新で CSS が無言で失効)の唯一の緩和策「メジャー更新後に目視確認」がコード内コメント止まりで `ops/reminders.yaml` に未登録。CLAUDE.md「将来アクションは機械可読で登録」違反。→ `streamlit-upgrade-dads-visual-check` を登録。
- **重要-2**: `config.toml` が「14px 未満は不許可」と宣言する一方、同 PR が書き換えた `app.py` の `_ORG_CSS` は font-size 10 箇所すべてが 10.4〜13.6px。同じ行の `line-height`・境界色は DADS へ寄せながら font-size のみ据え置き。併せて `.oc-fallback` の白文字は `#a78bfa` で **2.72:1**、`#059669` で 3.77:1、既定 `#888` で 3.54:1 と 4.5:1 を割る。テストは `config.toml` しか読まず未検出。→ 最小 0.875rem 化、アバター文字色の輝度選択、`app.py` への走査テスト追加。
- **重要-3**: `fetch-fonts.sh` の取得元が可変 ref `main` で SHA 固定・チェックサム検証なし、加工系も `fonttools>=4.53` を都度 PyPI 解決。代表のブラウザがパースするバイナリを再現性なく生成する。→ commit SHA 固定 + SHA-256 検証 + 依存 `==` 固定。失敗時に切れたライセンスが残らないよう `$WORK` 経由に。
- **中-4**: `dashboard/Dockerfile` と新規 `.streamlit/config.toml`(本番の `enableStaticServing=true`)が `protected_areas` 未登録で A-18-1 の検出外。CLAUDE.md の「デプロイ経路」に機械可読側が追いついていない。→ `area: deploy_path` へ登録(別 PR)。
- **中-5**: ブランチが `origin/main` より 4 コミット遅れ。rebase せずマージすると `app.py` の A-13 → A-18 改番(#48)を巻き戻し、規則ページが実在しない監査 ID を表示する。→ マージ前 rebase。
- **低**: `:focus-visible` の `border-radius:4px` がフォーカス時に要素の角丸を 8px→4px に変形(低-6)/ セレクタを緩めた戦略が偽陽性リスクのみ上げている(低-7)/ `_ORG_ACCENT` が単一出所の外でコントラスト検査対象外(低-8)/ `style` 属性へ HTML エスケープを適用しており `color` に CSS 注入余地(低-9・既存)。
- **反対を探して見つからなかった点**: CSS 注入に XSS 面は無い(展開値はすべて同ファイル定数、`icon_url` は SVG 拒否・SSRF 緩和済み)。コントラスト申告値は6色とも実測一致。`opacity:.6` は実効 `#767676` = 4.54:1 で下限を割らず、当初の疑義は取り下げた。ナビ到達テストは private API が壊れれば 14 中 13 が落ちる設計で「黙って通る」ではない(`_script_hash = calc_hash(_url_path)` を実装で確認)。色以外の冗長化(▲▼・語ラベル)は色剥がし後の可読性までテストで固定。DB 不要の 66 テストは 65 passed / 1 skipped。

## 設計リード裁定(2026-08-03 追記)

- 本 PR 内で是正: 重要-1(リマインダー登録)、重要-2(org CSS 最小 0.875rem・アバター文字色の輝度選択・app.py 走査テスト)、重要-3(SHA 固定+チェックサム+依存固定+$WORK 経由)、中-5(origin/main へ rebase・A-18 表記確認)、低-6(border-radius 削除)、低-7(display 強制の限定)、低-8(dads.ACCENT へ集約)、低-9(color の hex 検証)。
- 中-4(Dockerfile・.streamlit の保護領域登録)は次回の L1 バッチ PR で対応 — リマインダー `l1-protect-docker-streamlit` を登録(期限 2026-08-10)。
