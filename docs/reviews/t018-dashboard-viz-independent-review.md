# T-018 ダッシュボード可視化再設計 — 独立役員審査

- 審査日: 2026-08-03 / 審査者: 独立役員(非執行・批判専任。起草者の選好は不知)
- 対象: `origin/main..HEAD` 3 コミット — `dashboard/viz.py`(新規)・`app.py` 再構成・`queries.py` 新規 6 本・`config/llm.yaml` budget・`ci.yml` extras・`docs/research/dashboard-visualization-guidelines.md`
- 判定: **条件付き承認**(重大 3 件の是正をマージ前提条件とする)

## 数式検証(議論規約4 — 手計算突合 2 件)

TWR のフロー符号(`credit−debit` で出資が正 → 減算)・複利合成・underwater のピーク定義と分母は、
`ryza.risk.engine.book_returns` / `drawdown` と完全一致し、独立の手計算とも一致した(¥100万→¥101万→
¥151万(出資+¥50万)→¥149.5万 で設定来 −3.311258278149154e−05、NAV[100,120,90,130,110] で
DD[0,0,−25,0,−15.3846]%)。**数式自体に誤りは無い。**

## 重大(マージ前必須)

- **重大-1** `viz.py:334-351` 期間の充足を検証せずラベルを付ける。設定 2 日目の帳簿で 1W/1M/設定来が全て「+10.00%」になる。`tests/dashboard/test_viz.py:192-196` が容認。
- **重大-2** `viz.py:344-346` 窓判定がリターンの終端日のみで基準 NAV の日付を見ない。系列に穴があると「1W」が 21 日分になる(実測 −18.00%)。誤仕様が `test_viz.py:180-183` で凍結されている。
- **重大-3** `app.py:308-325` エンジンの `sufficient` / ES `deferred` を無視。観測不足時に vol bullet が赤 breach を出す一方、直下のラッチは「未作動」— 同一画面で矛盾。

## 重要・中

重要-4 概況にラッチ状態が無く、DD 回復後も `dd_hard` 発注停止中に赤が一つも出ない(`app.py:160-171`)。
重要-5 snapshot 無し日の外部フローが落ち「前日比 +50%」を生む(`queries.py:194-224` / `risk/daily.py:64-79` 共通・engine 側は保護領域)。
重要-6 払戻が underwater で −30% の損失に化ける(同日 TWR は 0.0%)。
中-7 A12 違反が `app.py:419, 899-900` に残存し、本 PR が宣言した赤緑規約と自己矛盾。
中-8 生 `st.progress` 3 箇所がヘルパ経由規約を破る。中-9 30日ローリング 対 暦月予算の不整合。
中-10 DB クエリに `@st.cache_data` が皆無で無索引の `journal_lines` 全集計が毎再実行走る。
低-12 bullet の limit 負値が画面に出る/unknown 行が最下段に沈む(fail-closed の思想と逆)。
低-13 fmt_delta_md の反転オプション欠如・fmt_sig の桁上限・fmt_jpy 小数・「1M=30暦日」の未明示。

## 適合を確認した点

新規 6 クエリは全て SELECT かつ `ryza_dashboard` の GRANT 範囲内、帳簿混合なし。「未実装明示(等配分BH)・
エンジン測定値の使用(DD)・段別所要の非表示」の 3 判断はいずれも実装と一致。A13(生 Sharpe 単独)違反なし。
bullet は limit=0/None/負/NaN のいずれでもゼロ除算せず `unknown` に落ちる(実行確認済み)。
`ci.yml` の `--extra dashboard` は `uv.lock` 解決済みで `--locked` を壊さず、GitHub REST も stub 済み —
**反対すべき点を探して見つからなかった。**

## 設計リード裁定(2026-08-03 追記)

- 本 PR 内で是正: 重大-1〜3(必須)、重要-4(概況にラッチ要約併置)、重要-6(net_flow≠0 日の図上注記)、
  中-7(緑の残存2箇所を規約に合わせ修正)、中-8(viz ヘルパ追加+CI に禁止記法 grep)、
  中-9(暦月起点へ変更+概況⑥にコスト記録率を一言)、中-10 の cache_data(ttl=60)部分、
  低-12 の2点、低-13(good_when 引数・fmt_sig docstring 用途限定・fmt_jpy 小数抑制・「(30日)」表記)。
- リマインダー登録して別 PR(保護領域): 重要-5 の engine フロー・ロールフォワード修正
  (**リスク数値の実バグ — 優先度高・期限 2026-08-08**)、重大-3 恒久の state_metrics 拡張、
  journal_lines / meta.runs の索引追加(migrations)。
- CI テスト所要(低-11)は今回は据え置き(現状 CI は 2 分未満)。遅くなったら 1 インスタンス方式へ。
