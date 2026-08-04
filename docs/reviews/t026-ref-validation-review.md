---
review: t026-ref-validation
reviewed_sha: 11cfd4633ec95a630250f2da262b1a5bd05baa6f
reviewer: independent-reviewer (opus)
review_date: 2026-08-04
verdict: approve
---

# T-026 参照形式の検証(F-10)+軽微堅牢化群(F-13)独立審査意見書

## 総評(一文アーギュメント)

**本 PR は指示書のとおり F-10 と F-13-1〜6 を実装しており、writer に検証を閉じる原則も既存呼び出しとの互換性も満たされている — approve が妥当である。** 反対すべき点(重大・中の所見)を能動的に探したが見つからなかった。軽微な指摘 2 件のみ後述する。

## 検証観点別の合否

### 1. 仕様適合

- **F-10 (a) PR URL**: `_PROPOSAL_REF_PR_URL_RE = ^https://github\.com/[A-Za-z0-9][A-Za-z0-9._-]*/…/pull/[1-9][0-9]*$` (`src/ryza/governance/decisions.py:160-162`)。指示書「本リポジトリの PR URL」と一致(3形式は本リポジトリ限定ではなく github.com 汎用 — 指示書冒頭の 3 形式列挙もそう読める)
- **F-10 (b) `decision:<数字>`**: 実装済み(同 L163)。`decision:0` を弾く(`[1-9][0-9]*`)、`decision:abc` を弾くのを目視+単体テストで確認(`tests/governance/test_decisions.py:99-105`)
- **F-10 (c) `manual:<[a-z0-9][a-z0-9-_]{2,63}>`**: 実装済み(L164)。指示書と一致。3〜64 文字。単体テスト(境界含む)`tests/governance/test_decisions.py:79-101`
- **F-10 source 正規表現**: `re.fullmatch(r"[\w][\w.:/\-]{0,127}", source)`(`src/ryza/provenance/evidence.py:52`)。指示書と一致。DB 実在の 11 種全通過を `tests/provenance/test_evidence.py:175-192` で固定
- **配線先**: F-10 は writer 2箇所 — `record_decision`(`src/ryza/bot/approvals.py:129`)と `record_deemed_approval`(`src/ryza/governance/decisions.py:352`)で `validate_proposal_ref` を呼ぶ。source は `EvidenceStore.store`(`src/ryza/provenance/evidence.py:238` 相当)と `ledger._util.create_evidence`(`src/ryza/ledger/_util.py:147-150`)の 2 経路で `validate_source` を呼ぶ。両者とも狙い通り writer に閉じられている
- **F-13-1 (A-12-09)**: 実装は `src/ryza/audit/a18.py:2078-2086` の docstring 注記のみ。ロジック変更なし(指示書「監査コードの意味論を変えない — 表示・注記のみ」に厳守)
- **F-13-2 (pass5-5)**: `tests/risk/test_classify.py:399` で正規表現を `'([a-z_]+)'::text` → `'([^']+)'::text` に拡張。テストのみの変更で、対象は `_constraint_vocabulary` ヘルパー
- **F-13-3 (A-12 pass4 所見2)**: `migrations/README.md` を新設し、REVOKE FROM PUBLIC が所有者ロールに no-op であり、主防壁は文トリガ側であることを明記。既存 migration の書き換えなし(指示書遵守)
- **F-13-4 (A-12-16)**: `_AMOUNT_PATTERN` を科学的記数法対応に拡張(`src/ryza/governance/boardroom.py:280-284`)。所見原文(`docs/reviews/a12/pass3-governance.md:466-489`)の推奨是正どおり
- **F-13-5 (A-12-19)**: `sanitize_speech` にコードフェンス追跡を追加(`src/ryza/governance/boardroom.py:670-689`)。開閉記号(``` / ~~~)一致だけを追い、入れ子・言語指定は厳密パースしない旨は所見原文の推奨(「完全にパースするのは複雑」)と整合
- **F-13-6 (pass4-security 所見5)**: `_mask_channel_id`(`src/ryza/bot/main.py:63-75`)で下 4 桁のみ残す伏字化。適用対象は `RuntimeError(f"チャンネル取得失敗: id={_mask_channel_id(channel_id)}")` の 1 箇所(L586)。もう一つの `f"チャンネル未解決 …: {msg.channel}"`(L557)は `msg.channel` が **論理チャネル名(approval/ops/press/dev)** であり Discord 内部 ID ではないため対象外 — 該当箇所のコメント(L552-556)で明示

### 2. fail-closed 性

- **proposal_ref**: writer 側 2 箇所とも `validate_proposal_ref` を先に呼び、`raise` するため書込前に停止する
- **source**: writer 側 2 箇所とも `validate_source` を先に呼ぶ。`create_evidence` はストア経由・インラインの両分岐の**前段**で検証しており、経路によって規則が変わる余地がない
- **迂回経路の探索**: `INSERT INTO governance.decisions` を含む生 SQL は `src/` 側では `record_decision` と `record_deemed_approval` の 2 箇所のみ(`Grep INSERT INTO governance.decisions` で確認 — 他は tests/dashboard のフィクスチャで、統制範囲外)。`INSERT INTO ledger.evidence` も `src/` 側では `EvidenceStore.store` と `_util.create_evidence` の 2 箇所のみ
- **末尾改行 bypass の懸念(検証済み)**: Python 正規表現の `$` はデフォルトで trailing `\n` の直前に一致するが、`validate_proposal_ref` は先に `value != value.strip()` で trailing whitespace(改行含む)を弾いており、`validate_source` は `re.fullmatch` を使うため trailing `\n` も拒否される。実測で `https://github.com/x/y/pull/1\n` / `TDnet\n` などが全て REJECTED になることを確認

### 3. 既存データとの整合(DB 実測)

- `SELECT DISTINCT source FROM ledger.evidence`(read-only)の 11 種(BOE / ECB / FRB / FRED / J-Quants / TDnet / demo / intl_banks / investment_committee / 日銀 / 米経済分析局BEA)を `validate_source` に流して 11/11 通過を実測
- `SELECT DISTINCT proposal_ref FROM governance.decisions` の 33 行はすべて `https://github.com/klonyapin/ryza/pull/<数字>` 形式で、`_PROPOSAL_REF_PR_URL_RE` を通過することを実測(指示書は「28 distinct」だが計測時点差 — 全数が (a) 形式である事実は変わらない)
- 既存 DB 行の遡及書き換えは行っていない(migration 変更なし)。追記オンリー原則に反しない

### 4. A-12 元所見との突合

- **F-13-1 ↔ A-12-09**: A-18-8 の「全ゼロが正常状態と無音経路を区別できない」問題を注記のみで開示 — 過剰・過少なし
- **F-13-4 ↔ A-12-16**: 推奨は「`[eE][+-]?\d+` を追加」— 実装と一致
- **F-13-5 ↔ A-12-19**: 推奨は「コードブロックの開始・終了を追跡、入れ子・言語指定の完全パースは不要」— 実装と一致
- **F-13-6 ↔ pass4-security 所見5**: 所見は 2 箇所を挙げたが、うち 1 箇所は論理チャネル名で Discord 内部 ID ではない — 対象特定は妥当

### 5. 回帰

- `record_veto` / `record_veto_withdrawal` / `record_revert_completion` は既存 `expected_proposal_ref` チェックのままで、DB から引いた `proposal_ref` との一致を見るだけ(新規 INSERT はしない)。1提案=1決定制約と veto 系のシグネチャに変更なし
- 既存テスト(`test_decisions.py` 127 件、`test_evidence.py` 42 件)全数 pass。`test_boardroom.py` は sanitize 系 28 件 pass。`test_a18.py` の変更範囲 7 件 pass。`test_classify.py` は共有 DB 由来の既知失敗(未変更 main でも失敗する範囲)に該当しない範囲で pass
- 変更したテスト(proposal_ref を `manual:xxx` に前置)は既存の振る舞い(UNIQUE 制約・二重記録防止・veto ワークフロー)を維持する意味で妥当。writer に F-10 検証を入れたことによる必要な追随であり、テストの意味を弱めていない
- ruff: `src/ryza/governance/decisions.py src/ryza/governance/boardroom.py src/ryza/provenance/evidence.py src/ryza/ledger/_util.py src/ryza/audit/a18.py src/ryza/bot/main.py src/ryza/bot/approvals.py` 全通過

## 所見

### 重大

なし。

### 中

なし。

### 軽微

**所見-1(軽微): `_PROPOSAL_REF_PR_URL_RE` は「本リポジトリ」に限定していない**

- **根拠**: `src/ryza/governance/decisions.py:161` — 正規表現は `https://github\.com/[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*/pull/[1-9][0-9]*$`。任意の owner/repo を許す
- **裁定**: 指示書 §F-10 実装項目 1(a)は「本リポジトリの PR URL」と書く一方、コメント L151 は「本リポジトリ規則で使う PR URL — `https://github.com/<owner>/<repo>/pull/<数字>`」と書き、両義的。DB 実在の proposal_ref は全て `klonyapin/ryza` に見えるので、より厳しく `klonyapin/ryza` に絞る余地はある。ただし A-18-1 側の突合(`repo_slug` チェックが別途走る)がリポジトリ整合を見ており、writer 側で絞らないと二重統制になる/実運用の混乱もない — 指示書のより広い読み方を採ったのは合理的判断。**修正必須ではない**が、将来別プロジェクトの PR URL が誤って入る余地は残る
- **是正案(任意)**: 環境変数 or 定数で本リポジトリの owner/repo を読み込み、他リポジトリ URL を拒否する。優先度は低い(A-18-1 が事後検出する)

**所見-2(軽微): `sanitize_speech` の fence パーサはコードブロック外側の閉じフェンス孤立を「開始」と解釈する**

- **根拠**: `src/ryza/governance/boardroom.py:670-689` — 状態機械は「フェンス行を見たら in_code をトグル」する単純実装で、`~~~` 単独行が本文中に一つ(閉じ忘れ・意図しない波線)出るとその後の全文が code とみなされ、話者行の引用化が止まる
- **裁定**: 指示書は「厳密パースはしない・同じ記号の対で開閉することだけを追う」と明示しており、この実装は指示に合致する。fail-open ではなく「引用化しない」方向(なりすまし表示可能性の増大)に倒れるが、`parse_speaker_sequence` は ASCII 厳密一致で議事録の解釈に影響しない旨がコード内に注記(L644-646)されており、統制ではなく表示上の是正である旨を維持している
- **是正案(不要)**: 現時点で追加の是正は不要。将来コードフェンス内の詐称行が問題化したら fence の閉じ忘れ検出を別途足す

## 反対意見書(議論規約 2)

3 案の想定失敗理由と代替案:

1. **「manual スラッグ下限 3 文字は緩すぎ・偶然衝突が残る」**: manual: プレフィックスが 7 文字あり、3+7=10 文字のスラッグ空間は実質 `[a-z0-9]` の 10 桁 = 60 兆通り。偶然衝突は起きない。**採用しない**
2. **「PR URL を `klonyapin/ryza` に絞るべき」**: 所見-1 で述べたが、A-18-1 側の repo_slug 検証が既に存在(`src/ryza/audit/a18.py:1823` 付近)し、writer 側は「様式の妥当性」だけを見る責任分担が合理的。**採用しない**(A-18-1 で二重統制になる)
3. **「F-13-6 は `msg.channel` 側も伏字化すべき」**: 所見原文が挙げた 2 行のうち 1 行だが、`msg.channel` は論理チャネル名(approval/ops/press/dev)で秘密ではない。伏字化するとログの可読性が落ち、`grep` での分類ができなくなる副作用のほうが大きい。**採用しない**

## verdict

**approve** — 反対すべき点を能動的に探して見つからなかった(所見-1・2 は軽微、修正必須ではない)。統合を推奨する。
