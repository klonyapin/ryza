# T-017: FM エージェント第一陣(Ben・Jim)+決定論サイジング

- 起草: 2026-08-03 設計リード / 前提: **T-014・T-016 統合後に着手**(T-015 併走可)
- 前提知識: CLAUDE.md(不変原則1・モデル階層)、40-fund-managers.md(承認済みロースター。Ben/Jim から開始)、81-fm-mandates.md+config/mandates/{ben,jim}.yaml、05-governance.md(役職資産)、src/ryza/governance/personas.py(着任ローダ)、T-014 gate_and_record、research/llm.py(StructuredLLM)
- **データ前提**: J-Quants の現行日足(Light プラン加入は代表判断待ち)。加入までは「12週前 as_of の過去リプレイモード」で実装・検証する — **as_of を全経路で一貫させれば point-in-time 原則は満たされる**(現在ニュースと過去価格の混合は禁止。リプレイ時は文書・分析も as_of 以前のもののみ)

## 目的

FM を「シグナル生成→注文案」として実装し、ゲートに投入する。哲学は銘柄選択と保有期間に現れ、**サイジングは決定論**(LLM の確信度をサイズにしない — 不変原則1)。

## 実装

1. **FM 役職資産** `personas/fm-ben/`・`personas/fm-jim/`(charter.md + system.md)
   - 40-fund-managers.md の哲学要約・意思決定規範・禁じ手を charter に(本人の模倣を主張しない)。Ben: 割安×質×長期・安全域。Jim: 統計エッジ・物語を信じない
   - **反証条件の義務**: 全ての新規ポジション提案に「この論点が崩れたら降りる」invalidation を必須記載(Alpha Illusion 対策・40 §制約1)
2. **提案スキーマ+記録** `migrations/0016_fm_theses.sql` — `trading.fm_theses`(追記オンリー): fm / instrument_id / direction / thesis_md / evidence_refs jsonb(**必須・point-in-time**: as_of 以前の docs/indicators 参照のみ)/ invalidation_md / as_of / run_id。注文案(orders)から thesis_id を参照
3. **Ben(LLM・週次)** `src/ryza/fm/ben.py` — 前処理済み文書・財務(EDINET/EDGAR)・バーからユニバース内の候補を StructuredLLM(mid 階層・dept_tag=fm.ben)で選定。出力: 候補リスト(銘柄・方向・thesis・evidence_refs・invalidation)。**候補数上限・保有銘柄の見直し(invalidation 成立チェック)も同時に**。LLM 出力はすべて fm_theses に記録
4. **Jim(非 LLM・日次)** `src/ryza/fm/jim.py` — モデル階層原則どおり**まず非 LLM**: バーからの決定論シグナル(初版: 20日/60日モメンタムのクロス+出来高フィルタ。パラメータは config/fm_jim.yaml・根拠コメント)。thesis は自動生成テキスト+ルール ID(evidence=バー参照)
5. **決定論サイジング** `src/ryza/fm/sizing.py` — 共通: スロット制 MVP。ポッド仮想資本(mandates の ¥200万)を最大 N スロット等分(N は mandate 由来 config)。ポジション追加=空きスロット、invalidation 成立=クローズ注文案。**確信度・スコアはサイズに影響させない**(採否のみ)
6. **配線**: daily に fm ステージ(Jim 日次)+ weekly に Ben(実行曜日は config)。生成した注文案は `gate_and_record` へ。block された案は fm_theses に判定結果を残し FM の次回プロンプトに含める(学習材料)
7. **判断履歴の永続化**: FM の判断・反省は governance.stances と同じ思想で fm_theses が担う(FM 別・新しい順で次回セッションに注入)

## テスト(tests/fm/)

- Jim シグナルの数値検証(固定バー系列→期待シグナル)/ サイジングのスロット計算・確信度非依存 / Ben は FixtureProvider(スキーマ適合・evidence 必須違反の拒否・invalidation 欠落の拒否)/ point-in-time: as_of 超の evidence 参照を拒否 / gate 連携 E2E(pass→orders、block→記録)

## 受け入れ基準

全テスト+ruff 通過 / サイジング経路に LLM 値が入らないことをテストで固定 / リプレイモード(as_of 指定)で一巡動作 / コミット刻み(schema → jim → sizing → ben → 配線)。日本語+Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>、push しない
