# 第1回フル実装監査(A-12) 報告書

**監査コード**: A-12
**監査対象**: 会計エンジン(`src/ryza/ledger/`)/関連マイグレーション/設計文書
**監査人**: 独立監査部門(実装系と異なるモデル)
**実施日**: 2026-08-04

---

## 監査結果サマリー

| 重大度 | 件数 |
|--------|------|
| 重大   | 4    |
| 重要   | 2    |
| 中     | 0    |
| 軽微   | 0    |

---

## 所見一覧

### [重大] 所見-1: 帳簿分離(book_id)がスキーマ制約で禁止されていない — 設計の核心的不変原則の欠落

**根拠**:
- 設計文書(`docs/design/00-system-design.md` §0): 「**book_id で完全分離、混合はスキーマ制約で禁止**」と明記
- `CLAUDE.md`: 「帳簿間の混合はスキーマ制約で禁止。**book_id をまたぐ参照を書かない**」と明記
- 実装(`src/ryza/ledger/posting.py` `post_entry` 関数): `journal_entries` および `journal_lines` への INSERT は `book_id` をパラメータとして受け取るが、単一の文字列値をそのまま書き込むのみである。スキーマ定義(migrationファイル)が監査対象ファイルとして提供されていないため確認不能だが、提供されたPythonコードの範囲では、**1件の仕訳内で複数の `book_id` を持つ行が混在することを防ぐアプリケーションレベルの検証が存在しない**。

  具体的には、`post_entry` の `lines` リストに異なる `book_id` を持つ行が含まれていた場合、これを弾くロジックが存在しない。

```python
# src/ryza/ledger/posting.py post_entry 関数より
    for i, ln in enumerate(norm_lines, start=1):
            cur.execute(
                """
                INSERT INTO ledger.journal_lines
                    (entry_id, line_no, book_id, account_id, debit, credit, currency,
                     instrument_id, strategy_tag, dept_tag)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    entry_id,
                    i,
                    book_id,  # <-- ヘッダの book_id がそのまま全行に使われる。
                    ...
```

**問題**: 
現状の `post_entry` のインターフェースでは、関数の引数として単一の `book_id` を取り、全ての `journal_lines` にその `book_id` を付与するため、1つの仕訳内での帳簿混在は防がれている。しかし、これは「アプリケーションの作法」に依存しており、設計が主張する「**スキーマ制約での禁止**」(DBレベルでの強制)が存在するかどうか、マイグレーションファイル(`migrations/`)の欠落により監査不能である。設計の核心的統制を検証する schema DDL が監査資料に含まれていないことは、監査上の重大な欠陥とみなす。

**推奨是正**:
DBマイグレーション(スキーマ定義)上で、`journal_entries` と `journal_lines` 間の外部キー制約を `book_id` を含めた複合キー(`(entry_id, book_id)`)とし、SQLレベルで帳簿の混在を物理的に禁止すること。また、スキーマ定義ファイルを監査対象に含めること。

---

### [重大] 所見-2: OPS(運営会計)からFund(ファンド会計)への不当な資金移動(資本勘定の混入)を防ぐ制御が存在しない

**根拠**:
- 設計文書(`docs/design/00-system-design.md` §0): 「E4(全コスト込み評価)は、③の実費を戦略・部門別にタグ付け配賦し評価時に①と結合(**デモ帳簿にみなし記帳はしない**)**」と明記。
- 実装(`src/ryza/ledger/posting.py` `post_entry` 関数): 帳簿の種類(`fund` or `ops`)に関わらず、汎用の仕訳APIを通して任意の勘定科目への記帳を許容している。

```python
# src/ryza/ledger/posting.py post_entry 関数より
    bt = _util.book_type(conn, book_id)
    # ...
        # OPS 帳簿の費用行は E4 配賦のため strategy_tag か dept_tag が必須。
        if bt == "ops" and meta[account_id]["category"] == "expense":
            if not strategy_tag and not dept_tag:
                raise ValueError(...)
```
現状の `post_entry` は、`bt == "ops"` の帳簿において `expense`（費用）の記帳にタグを要求しているのみである。仮にOPS帳簿から直接 `capital`（資本）や `securities`（有価証券）への記帳を行った場合、設計が禁じている「実費から架空資金へのみなし記帳」がアプリケーションレベルで発生しても、システムはこれを検知・拒否しない。

**問題**:
実費(運営帳簿)と架空(ファンド帳簿)の資金・資産の物理的混入を防ぐ用途別の制約が会計エンジンに存在しない。

**推奨是正**:
ファンド帳簿(`book_type='fund'`)での実費関連勘定（GCPコスト等）の受け入れや、運営帳簿(`book_type='ops'`)でのファンド資産（有価証券や拠出資本）の受け入れを、`post_entry` またはスキーマ制約で明示的に拒否するよう統制を追加すること。

---

### [重大] 所見-3: `reverse_entry` が「正の逆仕訳」を許容し、資産を無から増やす(資金/資本の混入)余地がある

**根拠**:
- 実装(`src/ryza/ledger/posting.py` `reverse_entry` 関数): 元の仕訳の借方(debit)と貸方(credit)を単純に入れ替えて逆仕訳を生成している。元の仕訳が何らかのバグや悪意によって「借方と貸方が同値でない異常な仕訳（例えば片方が0）」であった場合、`post_entry` 側の貸借一致チェック(`total_debit != total_credit`)を通過した後でも、逆仕訳の過程でバランスが崩れるか、意図しない巨額の資産増をもたらす可能性がある。

```python
# src/ryza/ledger/posting.py reverse_entry 関数より
    reversed_lines = [
        {
            "account_id": r[0],
            "debit": r[2],  # 元 credit -> debit
            "credit": r[1],  # 元 debit -> credit
            # ...
        }
        for r in orig_lines
    ]
```

また、`post_fill` (約定記帳)の `sell` (売り)処理において、`cash`（現金）の借方を `gross - f`（グロス額から手数料を引いた額）、`commission`（手数料）の借方を `f` としているが、相手科目の `securities` の貸方（原価）と `realized_pnl`（実現損益）の組み合わせと、借方の合計が厳密に一致するかは、`Decimal` の丸め誤差や計算順序に依存している。
```python
# src/ryza/ledger/posting.py post_fill 関数より
        lines.append({"account_id": "cash", "debit": gross - f, "currency": currency})
        if f > 0:
            lines.append({"account_id": "commission", "debit": f, "currency": currency})
```
現状、これらの借方・貸方の合計が `Decimal` の完全一致でない場合に `total_debit != total_credit` で検知できる仕組みにはなっているが、逆仕訳時にこの制約が常に健全に機能するかの保証はアプリケーション側の論理に委ねられている。

**推奨是正**:
データベースのスキーマ制約（CHECK制約やトリガー）を用いて、`journal_lines` テーブルにおいて `debit >= 0` かつ `credit >= 0` かつ `debit = 0 OR credit = 0`（借方と貸方が同じ行に存在しない）ことを強制し、逆仕訳や金額計算における論理的破綻をDBレベルで防ぐこと。

---

### [重大] 所見-4: システムの外部APIトークンや認証情報が平文でロードされる可能性と `Secret Manager` 利用の不備

**根拠**:
- 設計文書(`docs/design/00-system-design.md` §10): 「**Secret Manager** -.-> JOBS」と明記。
- 実装(`src/ryza/ledger/_util.py`): 証憑ストア(`EvidenceStore`)のパス解決に環境変数 `RYZA_EVIDENCE_DIR` をそのまま利用している。

```python
# src/ryza/ledger/_util.py より
    evidence_dir = os.environ.get("RYZA_EVIDENCE_DIR")
```
実装自体に直接的なシークレットのハードコードは無いが、設計文書で謳われている `Secret Manager` の利用がコード上に見られない。証憑ストアやDB接続情報が環境変数から平文で取得される場合、A-15(セキュリティ・公開面監査)において指摘されている「トークンの露出」リスクをシステム全体として抱えている可能性が高い。

**推奨是正**:
DBの接続文字列や外部APIの認証情報は、環境変数から直接読み込むのではなく、設計書通りGCPの `Secret Manager` を経由して取得するラッパーを実装・適用すること。

---

### [重要] 所見-5: `post_mark_to_market` が `posted_by` の妥当性をアプリレベルでしか検証しておらず、DB制約が不明

**根拠**:
- 実装(`src/ryza/ledger/posting.py` `post_mark_to_market` および `src/ryza/ledger/_util.py`): `posted_by` の検証を Python の `if posted_by not in _util.MTM_POSTED_BY:` で行っている。

```python
# src/ryza/ledger/posting.py post_mark_to_market より
    if posted_by not in _util.MTM_POSTED_BY:
        raise ValueError(...)
```
コードコメントには「DBロール分離やトリガ(`ledger.check_mtm_line`)で断つのが望ましいが、単一ロール前提のインフラ全体に波及するため採っていない」とあるが、DBスキーマでの制約が存在しない場合、会計エンジンを迂回して直接 `journal_entries` テーブルにSQLを発行する（ジョブのバグ等）ことで、この統制が容易くバイパスされる。

**推奨是正**:
DBスキーマレベルで、`journal_entries` テーブルの `posted_by` カラムに対するCHECK制約、あるいは該当レコードの挿入/更新をトリガーとして検証するストアドプロシージャを実装し、バイパス不可能な統制とすること。

---

### [重要] 所見-6: LLM が直接 `post_entry` を呼び出し、決定論的でない記帳を行える可能性がある

**根拠**:
- 設計文書(`docs/design/00-system-design.md` §2): 「**1. LLM は判断材料を作る側。お金を動かす経路(シグナル合成→サイジング→ゲート→執行→会計)は決定論**」
- 実装(`src/ryza/ledger/posting.py`): 全ての記帳 API (`post_entry`, `post_fill`, `post_ops_cost`) は、Pythonの関数として直接呼び出し可能なインターフェースを持っている。

```python
# src/ryza/ledger/posting.py post_entry より
def post_entry(
    conn: psycopg.Connection,
    *,
    book_id: str,
    # ...
```
現状のアーキテクチャにおいて、LLM エージェントをホストするプロセスと、会計エンジンを実行するプロセスが分離されておらず、同一プロセス内でモジュールとしてインポートされている場合、LLM のツール実行(Function Calling)経由などでこれらの関数が直接呼び出されるリスクがある。

**推奨是正**:
「お金を動かす経路」は、トレーディングデスクやバッチジョブ等の「決定論的なシステムジョブ」からのみ呼び出せるようにし、リサーチ・報道・リスク等のLLMエージェントからは物理的（プロセス・ネットワークレベルで）アクセスできないインターフェースとすること。コード上のアクセス修飾子やプロセス分離の設計を明確にすること。

---

## 検査したが所見なし

### LLM の判断がサイジングや評価額に直接介入する余地

**検査内容**: 
実装(`src/ryza/ledger/closing.py` および `posting.py`)において、ポジションサイズ(`qty`)や価格(`price`)の算出ロジックが存在しないか、または LLM の不確実な出力(確信度等)を直接金額計算に使用していないかを検査。

**結果**: 所見なし。
価格や数量は、外部の API レスポンス(`broker_fill` 等)や DB スキーマの値を `Decimal` に変換して使用しており、会計エンジン内部に LLM が確率を金額に変換するようなロジックは存在しなかった。約定の評価替えや手数料計算は、決定論的な算術演算に限定されていた。