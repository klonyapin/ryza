# T-012: 取込ソース一括拡張(EDGAR・e-Stat・海外中銀)

- 発行日: 2026-08-03 / 依存: T-009 完了(共通基盤 base.py を使用)
- 必読: docs/design/20-research.md §2(一括拡張バッチの項)、src/ryza/ingest/base.py、CLAUDE.md

## ゴール

既存パラダイム(API/RSS→documents/indicators)で処理可能な無料ソースを一括追加する。

## 実装

```
src/ryza/ingest/edgar.py       -- SEC EDGAR: submissions/companyfacts(米企業開示)+13F。10req/s 制限・User-Agent 連絡先必須
src/ryza/ingest/estat.py       -- e-Stat API(日本政府統計。appId は Secret 'estat-app-id')
src/ryza/ingest/intl_banks.py  -- ECB Data Portal API / BOE / IMF SDMX の主要系列
config/feeds.yaml              -- 主要国統計局・国際機関の RSS を追加
tests/ingest/                  -- 各ソースのモックテスト(T-009 と同水準)
```

- すべて base.py の契約(Run・as_of・証憑・冪等・リネージ)に準拠。indicators への系列は接頭辞で名前空間分離(EDGAR:/ESTAT:/ECB: 等)
- レート制限・User-Agent 等の各ソース規約をコードコメントに明記

## 受け入れ基準

- [ ] 各ソースのモックテスト(正常・重複・異常系)/ 冪等 / 証憑・リネージ
- [ ] `uv run pytest` 全通過・ruff パス
- 完了コミット: `feat(ingest): 取込ソース一括拡張 (T-012)`+Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
