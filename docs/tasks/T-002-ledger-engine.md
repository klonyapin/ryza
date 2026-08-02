# T-002: 会計エンジン(記帳・締め・財務諸表)

- 発行日: 2026-08-02 / 発行者: 設計リード(Fable)/ 依存: T-001 完了
- 必読: `CLAUDE.md`、`docs/design/10-data-accounting.md` §5、`docs/design/00-system-design.md` §4〜6

## ゴール

ファンド会計部・経営管理部の中核となる会計エンジン `ryza/ledger/` を実装する。記帳 API、日次締め、財務諸表生成、照合(reconciliation)まで。

## モジュール構成

```
src/ryza/ledger/
  posting.py      -- 記帳 API
  closing.py      -- 日次締め(評価替え→アクルーアル→NAV)
  statements.py   -- 試算表・BS・PL・CF の生成
  recon.py        -- 照合とブレイク管理
tests/ledger/
```

## 仕様

### posting.py
- `post_entry(book_id, entry_date, description, lines, evidence, run_id) -> entry_id`
  - `lines`: `[{account_id, debit|credit, currency, instrument_id?, strategy_tag?, dept_tag?}]`
  - `evidence`: 既存 evidence_id または `{kind, payload, source}`(その場で evidence 行を作成。payload は証憑ストアに保存し sha256 を記録 — T-003 完了までは DB 内 JSONB フォールバック)
  - OPS 帳簿の費用行(category='expense')には `strategy_tag` か `dept_tag` が必須(E4 配賦のため)。無ければ ValueError
- `reverse_entry(entry_id, reason, run_id)` — 逆仕訳の生成
- 典型仕訳のヘルパー: `post_fill()`(約定: 現物買い/売り、手数料。実現損益は移動平均法で計算)、`post_mark_to_market()`(評価替え: 未実現損益の洗い替え)、`post_ops_cost()`(GCP/LLM 費用)

### closing.py — 日次締め(設計書 §5 のシーケンス)
- `run_daily_close(book_id, date, price_source, run_id)`:
  1. 未記帳の約定を検出して記帳(冪等: 記帳済み fill はスキップ)
  2. 全ポジションを終値で評価替え(price_snapshot を evidence 化)
  3. アクルーアル(当面は手数料のみ。金利は TODO コメント)
  4. NAV 算出 → `nav_snapshots` に `provisional` で保存
  5. `recon.py` の照合結果が全件 matched なら `confirmed` に更新、不一致があれば provisional のまま

### statements.py
- `trial_balance(book_id, as_of_date)` / `balance_sheet(...)` / `income_statement(book_id, period)` / `cash_flow(book_id, period)`
- 出力は DataFrame(スキーマ固定)。CF は OPS=直接法(現金勘定の相手科目別集計)、ファンド帳簿=投資/財務区分
- 外貨は期末レート換算、換算差損益は PL の独立行(基準通貨 JPY)

### recon.py
- `reconcile(book_id, date, broker_snapshot)` — broker_snapshot(ポジション・約定・評価額。**現金総額は対象外** — デモ口座の仮想残高は帳簿と一致しない設計)と帳簿を突合し、`reconciliations` に記録。不一致は `break_open` + 通知フック(通知実装は後続タスク、ここではコールバック interface のみ)

## 受け入れ基準

- [ ] フィクスチャ(数銘柄の約定・価格系列)で: 記帳→締め→試算表がゼロバランス、BS 資産合計=負債+資本、NAV = 資産-負債
- [ ] 買い→値上がり→一部売却のシナリオで実現損益(移動平均法)と未実現損益が手計算と一致
- [ ] 貸借不一致・証憑なし・OPS 費用のタグなしが例外になる
- [ ] 照合一致で NAV が confirmed、意図的に壊した snapshot で break_open + provisional のまま
- [ ] 逆仕訳後の試算表が元に戻る
- [ ] すべての書き込みが run_id を持つ

## 非ゴール

ブローカー API 接続(モックの broker_snapshot を使う)、GCS 証憑ストア(T-003)、予算管理(T-004 予定)、Discord 通知。

## 完了時

コミット: `feat(ledger): 会計エンジン(記帳・日次締め・財務諸表・照合) (T-002)`。矛盾発見時は T-002-questions.md に記録して停止。
