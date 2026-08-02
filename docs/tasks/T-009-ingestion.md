# T-009: データ取込パイプライン

- 発行日: 2026-08-03 / 依存: T-001/T-003(完了済)。T-006 と並行(ファイル領域が異なる)
- 必読: docs/design/20-research.md §2・§6(仕様の正)、docs/design/10-data-accounting.md §2〜3、CLAUDE.md

## ゴール

初期スコープ5ソース(J-Quants 日足・財務 / TDnet RSS / EDINET / 汎用ニュース RSS / 経済カレンダー)の取込ジョブ群と鮮度 SLA 監視を実装する。

## 実装

```
migrations/0008_research.sql     -- market.calendar_events / market.watchlist(20-research §6)
src/ryza/ingest/base.py          -- 共通: Run 経由・as_of・証憑保存(provenance.evidence)・content_hash 冪等・リネージ記録
src/ryza/ingest/jquants.py       -- 日足・財務・銘柄マスタ(Free プラン API。認証: Secret 'jquants-refresh-token'、環境変数フォールバック)
src/ryza/ingest/tdnet.py         -- 適時開示 RSS(5分ポーリング用。開示種別の辞書分類は T-010 なのでここでは生取込のみ)
src/ryza/ingest/edinet.py        -- 書類一覧+type=5 CSV 取得
src/ryza/ingest/news_rss.py      -- 汎用 RSS(feed URL リストは config/feeds.yaml。初期セットはコメントで提案し設計リード確認待ちと明記)
src/ryza/ingest/calendar.py      -- 経済カレンダー(初期は日銀・FRB・主要指標の静的定義+決算予定は J-Quants から)
src/ryza/ingest/freshness.py     -- ソース別鮮度 SLA 検査 → press.outbox(ops チャンネル)へ警告投入
tests/ingest/                    -- 全ソース: HTTP モック・冪等性・as_of/リネージ・証憑保存の検証
```

- 実 API 疎通(J-Quants)は資格情報登録後に設計リードが行う。テストは全てモックで完結させること
- bars への書込は instrument_id 解決(market.instruments に無い銘柄は SCD2 で自動登録)を含む
- ジョブは Cloud Run Jobs 想定のエントリポイント(`python -m ryza.ingest.<source>`)+ローカル実行可能

## git 規約

パス指定 add のみ(-A 禁止。並行ワーカーが src/ryza/bot と migrations/0007 を使用中 — **migrations は 0008 を使い 0007 に触れない**)/ 30分ごと wip / 15分詰まったら T-009-questions.md で停止 / push しない。完了コミット: `feat(ingest): データ取込パイプライン (T-009)`+Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

## 受け入れ基準

- [ ] 各ソースのモックテスト(正常・重複・異常系)+鮮度 SLA の発火テスト
- [ ] 同一データ再取込で行が増えない(冪等)
- [ ] 全書込行に run_id・as_of、原文が証憑ストアへ、lineage_edges 登録
- [ ] `uv run pytest` 全通過・ruff パス
