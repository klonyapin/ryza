# T-005: 会計エンジンと証憑ストアの統合(小タスク)

- 発行日: 2026-08-02 / 依存: T-002・T-003 完了(済)
- 必読: src/ryza/provenance/evidence.py の docstring(安定 API)、src/ryza/ledger/_util.py の証憑作成部、docs/design/10-data-accounting.md §5 補足

## ゴール

T-002 の暫定実装(証憑 JSON を payload_ref にインライン格納)を、T-003 の証憑ストア(`ryza.provenance.evidence`)経由に置き換える。

## 仕様

1. `ledger._util` の証憑作成を `provenance.evidence.store(conn, kind, payload, source)` 呼び出しに変更。ストレージは環境変数 `RYZA_EVIDENCE_DIR` があれば `LocalStorage(そのパス)`、無ければ従来どおりインライン格納にフォールバック(kind='decision' 等の小型内部記録は設計上インライン許容のため、フォールバックは仕様違反ではない)
2. posting の公開シグネチャは不変(evidence_id または dict を受ける)
3. 既存テストを全て緑に保ち、統合経路のテストを追加: RYZA_EVIDENCE_DIR 設定時に store 経由で保存され、verify が通り、仕訳の evidence_id から get で原文が取れること

## 受け入れ基準

- [ ] 全スイート pytest 通過(既存 55 + 追加分)
- [ ] ruff パス
- [ ] コミット: `feat(ledger): 証憑ストア統合 (T-005)`
