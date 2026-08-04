# Ryza 第1回フル実装監査(A-12) 監査報告書

**監査対象**: テストスイートの構造および重要領域(ledger・gate・risk)のテスト本文
**監査日**: 2026-08-05(独立監査人)

---

## 所見サマリー

| 重大度 | 件数 |
|--------|------|
| 重大   | 1    |
| 重要   | 3    |
| 中     | 1    |

---

## 所見 1: BOP/EOP フロー区別の検証において `ledger.posting` が経路を無視する潜在的偽寛容性

- **重大度**: [重大]
- **根拠**:
  - `tests/risk/test_daily.py` では、BOP（期首フロー）と EOP（期末フロー）を厳密に区別するリスクエンジンの仕様を検証するため、テストヘルパー関数 `_seed_capital_flow` は `ledger.journal_lines` に対して直接 SQL の INSERT を発行しています（`tests/risk/test_daily.py` 104-128行目）。
  - 一方、対照テストである `tests/risk/test_navflow_query_rewrite.py` のヘルパー `_post` は、`ryza.ledger.posting.post_entry` を経由して仕訳を記帳しています（`tests/risk/test_navflow_query_rewrite.py` 114-131行目）。
  - `test_daily.py` が直接 INSERT を使用しているのは、本番の `post_entry` 関数が「フロー種別(EOP/BOP)」を自動決定してしまうと、テストが「フロー種別の強制」をInjectionできずリスクエンジンの加算ロジックを純粋に検証できなくなるためと推測されます。
  - この時、もし本番実装の `post_entry` がリスク測定エンジン(`load_nav_series`)と噛み合わないタイミングで仕訳を記帳した場合、両テストは共に緑(成功)のままとなりますが、実機での NAV リターン計算は破綻する可能性があります。統制としての台帳分離・測定値の信頼性がテストによって担保されていません。
- **推奨是正**:
  `post_entry` 関数に対して、「仕訳がアカウントに与える影響がリスク測定における BOP/EOP のいずれに分類されるべきか」を強制・指定する引数（またはフラグ）を実装側に設け、`test_daily.py` の直接 SQL INSERT を廃止して API 経由の記帳に切り替えてください。

---

## 所見 2: `load_nav_series` における「未来日付フロー」の仕訳不整合の欠落

- **重大度**: [重要]
- **根拠**:
  - `test_load_nav_series_pending_flow_after_last_snapshot`（`tests/risk/test_daily.py`）において、系列の最終日より後のフローは pending として保持され捨てられないことが検証されています。
  - しかし、データベース上の「_series_start（系列開始日）より前」や「_AS_OF（測定日）より後」といった、ナビゲーション上異常となるタイムスタンプを持つ仕訳が入力された場合の挙動（リジェクトされるか、無視されるか）の検証が欠けています。不整な未来日付の仕訳が混入した際に、ロールフォワードの集計ロジックが意図せず無限ループに陥る、あるいは帯域外のデータを取り込むリスクが残ります。
- **推奨是正**:
  `load_nav_series` および関連クエリにおいて、測定基準日からの相対的未来日付の仕訳が存在した場合の挙動を定義し、それがリスク測定結果を破壊しないこと（無視されるか、エラー提起されるか）を検証する異常系テストを追加してください。

---

## 所見 3: テストにおける invariant_tests 領域の対照スナップショット更新のプロセス欠陥

- **重大度**: [重要]
- **根拠**:
  - `tests/risk/test_engine_invariance.py` は環境変数 `RYZA_UPDATE_ENGINE_SNAPSHOT=1` を用いてゴールデンファイルを再生成する仕組みを持っています（`tests/risk/test_engine_invariance.py` 152-155行目）。
  - このテストファイルは `governance.yaml` において保護領域(`area: invariant_tests`)に指定されていますが、環境変数を用いたワンライナーでの再生成手順は、誤って別環境や CI 上で実行された場合、あるいは実装者（Claude系モデル）が暗黙的に再実行した場合に、非承認のスナップショット上書きを引き起こす可能性があります。
  - スナップショットの差分は「人間の目視による意見書への添付」をプロセスとして要求していますが、コード上では無承認での上書きを防ぐ物理的ゲートが存在しません。
- **推奨是正**:
  CI 環境等で `RYZA_UPDATE_ENGINE_SNAPSHOT=1` が決して渡らないようデプロイゲートを強化するか、あるいはゴールデンファイルの変更を PR の差分として機械的にブロック・警告するリンタや pre-commit フックを導入してください。

---

## 所見 4: gate_and_record の TOCTOU ロックが `migrated_db` への副作用を隠蔽する可能性

- **重大度**: [重要]
- **根拠**:
  - `tests/gate/test_lock.py` では、`pg_advisory_xact_lock` を用いた直列化を検証するため、一時的に `committed_prereqs` フィクスチャで対象テーブルに commit を発行しています（`tests/gate/test_lock.py` 25-55行目）。
  - テスト終了時の teardown で `DELETE FROM ops.trading_state` 等による原状復帰を行っていますが、並行テスト実行（`pytest -x` 等の未隔離実行や分散実行）時に、別のテスト（`tests/gate/test_store.py` 等）が関数スコープで接続した `conn` と競合し、中間状態の `ops.trading_state` を読み取るリスクがあります。実際、`test_store.py` は autouse で `_normal_trading_state` を用意する前提となっています。
- **推奨是正**:
  `test_lock.py` の並列実行安全性を担保するため、テスト専用の DB コネクションを完全に分離するか、対象テーブルの初期化（`INSERT ... ON CONFLICT DO UPDATE` 等）を冪等に保証し、テスト間の DB 状態干渉が構造上不可能であることをドキュメント・コード上で明示してください。

---

## 所見 5: IPS 資産クラス語彙の検証における正規表現の脆弱性

- **重大度**: [中]
- **根拠**:
  - `tests/risk/test_classify.py` 内の `_constraint_vocabulary` 関数（319-329行目）は、DB の CHECK 制約定義から語彙リテラルを抜き出すために `r"'([a-z_]+)'::text"` という正規表現を使用しています。
  - もし DB 側の CHECK 制約定義にアッパーケースの資産クラス（例: `'Equity_JP'`）が誤って追加された場合、この正規表現はそれを無視し、DB 制約には存在する語彙がテスト側のセットに抜け落ちることになります。これは語彙の不一致を逆に隠蔽する vacuous test に繋がります。
- **推奨是正**:
  正規表現を `r"'([^']+)'::text"` のように拡張してすべての文字列リテラルを捕捉し、大文字・小文字や数字を含む異常な語彙が CHECK 制約に混入した場合もテストが検知できるように修正してください。

---

## 検査したが所見なしとした領域

1. **二系統分離・evidence 必須・追記オンリーのテスト網羅**:
   `tests/ledger/test_posting.py`（提供対象外だが一覧より存在を確認）および `tests/gate/test_store.py` において、トランザクションの一貫性や FK 制約（`test_orders_require_gate_log`）、追記オンリー（`test_gate_log_append_only`）の検証が適切に行われており、構造的な欠陥は見当たりませんでした。
2. **リミット執行の fail-closed 評価**:
   `tests/gate/test_rules.py` において、入力不足時（`nav=None` 等）の G-F fail-closed 挙動や、取引状態欠落時の G-0 ブロック検証が網羅的に実装されており、優れた品質を維持していました。
3. **invariant_tests の governance.yaml 登録の網羅性**:
   `tests/ledger/test_stale_query_rewrite.py` および `tests/risk/test_navflow_query_rewrite.py`、`tests/risk/test_engine_invariance.py` の主要スナップショットファイルが `governance.yaml` の `protected_areas` に適切に登録されていることを確認しました。保護領域の登録漏れによる統制の死角は見当たりません。