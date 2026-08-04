# Ryza 第1回フル実装監査報告書

- 監査コード: A-12
- 監査対象: 会計エンジン(`src/ryza/ledger/`)及び会計スキーマ(`migrations/0005, 0006, 0011, 0034`)
- 監査日: 2026-08-04
- 監査人: 独立監査人(Claude系モデルと別系統モデル)

---

## 所見一覧

### [重大] 1. 複式簿記の不変条件が単一仕訳レベルで強制されていない(§2 設計原則5・データの完全性)

**根拠**:
- `migrations/0005_ledger.sql` の `ledger.check_entry_balance()` トリガは `DEFERRABLE INITIALLY DEFERRED` の制約トリガとして実装されている。
- Python 実装 `src/ryza/ledger/posting.py` の `post_entry` 関数(110行目〜)では、`journal_entries` への INSERT 後、`journal_lines` を1行ずつループで INSERT している。
- 仕訳が中途半端な状態(例: 借方1行のみ挿入)でトランザクションが中断した場合、トランザクションがコミットされない限りエラーが検知されない。
- 設計文書 `00-system-design.md` §4 は「日次締め→照合→一致で NAV 確定」と規定しているが、この仕組みはトランザクションの原子性に依存しており、アプリケーションレベルでの検証(DBにコミットする前の検証)が欠如している。

**推奨是正**:
`post_entry` において `cur.executemany()` 等による一括 INSERT、またはコミット前のアプリケーションレベルでの `sum(debit) == sum(credit)` チェックを追加すべき。

### [重大] 2. 証憑ハッシュ(`sha256`)のストア未設定時の改竄検知無効化(§4 証憑必須)

**根拠**:
- `src/ryza/ledger/_util.py` の `create_evidence` 関数(105行目)で、`RYZA_EVIDENCE_DIR` 環境変数が未設定の場合、`payload_ref` にJSON文字列を直接格納し、その文字列に対して `sha256` を計算している。
- 設計文書 `00-system-design.md` §4 では「証憑(約定 API レスポンス原文+ハッシュ、価格スナップショット参照、GCP Billing Export 行、請求書)は証憑ストアに不変保存」と規定。
- DB上の `payload_ref` にインライン格納されたJSONは、DB直接操作やダンプ編集で容易に書き換え可能であり、同時に計算される `sha256` 列も合わせて書き換えれば改竄検知が全く機能しない。
- ストアが未設定の状態では、「不変保存」が名目的なものに退化している。

**推奨是正**:
本番環境においては `RYZA_EVIDENCE_DIR` を必須とするか、ストア未設定時のインライン格納を `if not settings.PRODUCTION:` のような環境ガードで明示的に安全に制限すべき。

### [重要] 3. トランザクション境界における `revoked` 権限の実効性欠如(§2 設計原則5・不変原則3)

**根拠**:
- `migrations/0005_ledger.sql` において `REVOKE UPDATE, DELETE ON ledger.journal_entries FROM PUBLIC;` が定義されている。
- しかし、PostgreSQLの権限モデルにおいて `PUBLIC` への REVOKE は、テーブルの所有者やスーパーユーザーには効果がない。スキーマの所有権を持つアプリケーションロールがこれらを実行する場合、追記オンライン性は実質的にアプリケーションコードの振る舞いにのみ依存している。
- 同ファイルにある `forbid_mutation()` トリガは所有者に対しても効くが、`TRUNCATE` 文に対する防御が存在しない。

**推奨是正**:
`TRUNCATE` 権限の明示的な REVOKE、またはスーパーユーザー/所有者を含めて TRUNCATE を防ぐトリガの追加を推奨する。

### [重要] 4. OPS帳簿とファンド帳簿の分離と E4 配賦のためのタグ必須チェックの対象範囲(§0 E4・CLAUDE.md 不変原則2)

**根拠**:
- `src/ryza/ledger/posting.py` の `post_entry` 内(82行目)において、OPS帳簿の費用行に対する `strategy_tag` または `dept_tag` の存在確認が行われている。
- しかし、このチェック対象は `meta[account_id]["category"] == "expense"` の行のみに限定されている。
- 資産勘定(例えば `cash_bank` や `prepaid` 等)の入金や移転にタグが付与されなかった場合、後から E4(全コスト込み評価)のために「実費を戦略・部門別にタグ付け配賦」する際に、資産側のトラッキングが欠落する可能性がある。

**推奨是正**:
OPS帳簿におけるタグ付け要件を費用勘定のみならず関連する資産・負債勘定にまで拡張するか、設計文書上のE4配賦ロジックが費用のみで完結するのかを明確化し、ドキュメントと実装をすり合わせるべき。

---

## 検査したが所見なし

### 検査1: 帳簿分離・混合禁止の物理的強制(§0 会計二系統・§6 不変原則2)
- **検査内容**: `book_id` をまたぐ参照がFKまたはトリガで防がれているか。
- **結果**: `migrations/0005_ledger.sql` において、`ledger.check_line_book()` が `BEFORE INSERT` トリガとして機能し、`journal_lines.book_id` と `journal_entries.book_id` の不一致を物理的に拒否している。また、`accounts` テーブルの `PRIMARY KEY (book_id, account_id)` 及び `journal_lines` の外部キー制約 `FOREIGN KEY (book_id, account_id) REFERENCES ledger.accounts` により、異なる帳簿間の勘定科目混在使用が構造的に阻止されている。**所見なし。**

### 検査2: 証憑(evidence_id)必須の強制(§4 証憑必須・不変原則3)
- **検査内容**: 全ての仕訳行に `evidence_id` が紐づいているか。
- **結果**: スキーマ定義において `journal_entries.evidence_id bigint NOT NULL` が定義されており、DBレベルで NOT NULL 制約により強制されている。Python API側でも `_util.resolve_evidence` において `ValueError` を発生させる二重の防壁がある。**所見なし。**

### 検査3: 評価替え(MTM)と原価の分離による統制(§4 記帳原則・保護領域)
- **検査内容**: `0034_ledger_mtm_account.sql` により、MTMと原価が分離され、統制が効いているか。
- **結果**: `securities_mtm` 勘定に対する `check_mtm_line()` トリガおよび、原価勘定に対する `check_cost_line()` トリガにより、許可されたジョブ・証憑のみが記帳できる仕組みが確立されている。また、逆仕訳の免除条件をフラグではなく実突合(`reversal_mirrors_line`)で行うことで、迂回記帳を極めて困難にしている。**所見なし。**

### 検査4: 行レベル制約の定義(借方・貸方の健全性)
- **検査内容**: 1行の仕訳明細で借方と貸方が同時に立てられないか、マイナス金額が防止されているか。
- **結果**: `src/ryza/ledger/posting.py` の `post_entry` 関数内(85-88行目)において、`if debit < 0 or credit < 0` および `if debit != 0 and credit != 0` のバリデーションが実装されており、異常な仕訳行の入力をアプリケーションレベルで防御している。**所見なし。**

---
*以上、監査を終了する。*