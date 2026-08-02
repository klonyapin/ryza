# T-003: 証憑ストアとリネージ記録

- 発行日: 2026-08-02 / 発行者: 設計リード(Fable)/ 依存: T-001 完了(T-002 と並行可)
- 必読: `CLAUDE.md`、`docs/design/10-data-accounting.md` §6〜7

## ゴール

全部門が使う横断基盤2つ: ①証憑ストア(不変保存+改竄検知)②リネージ記録(run 管理+成果物→入力の辺)を `ryza/provenance/` に実装する。

## モジュール構成

```
src/ryza/provenance/
  evidence.py    -- 証憑の保存・取得・検証
  runs.py        -- ジョブ実行(run)のライフサイクル
  lineage.py     -- リネージ辺の記録・遡及クエリ
tests/provenance/
```

## 仕様

### evidence.py
- `store(kind, payload: bytes | dict, source) -> evidence_id`
  - sha256 を計算し、ストレージに `evidence/{yyyy}/{mm}/{kind}/{sha256}.{ext}` で保存、`ledger.evidence` に行を作成
  - ストレージはインターフェース `EvidenceStorage` で抽象化: `LocalStorage`(開発用・ディレクトリ)と `GcsStorage`(本番用。バケットはバージョニング+削除保護前提)。同一 sha256 は再保存せず既存 evidence_id を返す(重複排除)
- `verify(evidence_id) -> bool` — ストレージ上の実体の sha256 と DB 記録を突合(監査 A-1 の部品)
- `get(evidence_id) -> payload`

### runs.py
- `start_run(job_name, params) -> Run`(code_version は git describe で自動取得)/ `Run.finish(status)` / `Run.add_cost(model_tier, tokens, cost_estimate)`
- コンテキストマネージャ `with run("ingest.jquants.daily") as r:` で例外時に failed 記録
- **規約**: DB に書き込む全ジョブは Run 経由で run_id を取得すること(CLAUDE.md に追記済みの前提)

### lineage.py
- `record(run, outputs: list[(kind, id)], inputs: list[(kind, id)])` — lineage_edges への一括登録
- `trace_back(kind, id, max_depth=10)` — 成果物から入力へ再帰的に遡り、木構造を返す(「この仕訳の元データは何か」)
- `trace_forward(kind, id)` — 逆方向(「このニュースはどの成果物に使われたか」)

## 受け入れ基準

- [ ] store→verify が通る。ストレージ上のファイルを書き換えると verify が False
- [ ] 同一内容の store が同じ evidence_id を返す(重複排除)
- [ ] LocalStorage / GcsStorage(GCS はモック)が同一インターフェースで動く
- [ ] run のコンテキストマネージャが正常系 success / 例外系 failed を記録
- [ ] 3段のリネージ(document → report → journal_entry)を登録し、trace_back が全段を返す
- [ ] T-002 の posting.py が evidence.py 経由に置き換わる(JSONB フォールバックの除去)— T-002 実装者と調整

## 非ゴール

GCS の実インフラ作成(デプロイタスクで実施)、監査ジョブ本体、データカタログ UI。

## 完了時

コミット: `feat(provenance): 証憑ストアとリネージ記録 (T-003)`。矛盾発見時は T-003-questions.md に記録して停止。
