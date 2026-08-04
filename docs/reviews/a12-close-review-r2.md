---
review: a12-adjudication-close (r2)
reviewed_sha: d5a9d50704c3932ffc83ff8cab72d27eb9cbe202
reviewer: 独立役員(再検証・プロンプト分離)
review_date: 2026-08-04
verdict: approve
---

# PR #145 再検証審査(r2)— A-12 裁定書 §6 是正完了記録

**アーギュメント**: r1 の4所見はすべて修正コミット d5a9d50 で解消されており、修正が導入した記述は PR #135 本文・main の実体(reminders.yaml / migrations / governance.yaml)・PR #135 独立審査記録と一次資料レベルで一致するため、軽微な用語ゆれ1件(非ブロッキング)を除き承認する。

## 1. 検証の方法

- 検証対象: `docs/reviews/a12/00-adjudication.md` §6(reviewed_sha: d5a9d50)および `ops/reminders.yaml` の `a12-2026q4-second-full-audit` エントリ
- 差分確認: `git diff fc62932..d5a9d50` — 変更は `docs/reviews/a12/00-adjudication.md` のみ(3箇所: F-1 行・F-4 行・表直後の説明注記)。`ops/reminders.yaml` は無変更(r1 で有効と判定済みのエントリが手つかずであることを確認)
- 事実照合の一次資料: PR #135 本文(`gh pr view 135`)、main の `ops/reminders.yaml`(reminders-status-tamper-detection)、main の `migrations/`、main の `config/governance.yaml` protected_areas、`docs/reviews/f1-a18-reminders-tamper-review-r2.md`、PR #145 本文

## 2. r1 4所見の解消照合

| # | r1 所見 | 判定 | 根拠 |
|---|---|---|---|
| 重大-1 | F-1 行の事実誤認(「保護領域化+A-18 改変検出」) | **解消** | F-1 行は「意味的改ざん検査(A-18-9)を新設 — 当初是正案(protected_areas 登録)は不採用」に書き換え済み。PR #135 本文と一致: A-18-9 の3検出パターン(pending 期日後ろ倒し・pending 削除・証跡なし終端遷移)、「全体の protected_areas 登録は不採用」を明記。main の `config/governance.yaml` protected_areas に `ops/reminders.yaml` が存在しないことを全 path の列挙で確認 |
| 中-1 | §4「実弾移行前に全て解消」との整合(F-1 の統制性質転換)が無説明 | **解消** | 表直後に「F-1 の統制性質の転換について」注記を追加。事前禁止→事後検知への転換理由(直近1週間で全コミットの 35%=166/473 が本ファイルに触れる・週次ジョブが `fired:` 更新を Contents API で main に直接書き込む → 登録すれば毎週 A-18-1 が赤になる)は PR #135 本文の記載と逐語一致。「§4 は無承認改変を検出する統制の稼働をもって満たす」と整合の読み替えを明示。転換の承認実績も実在: `docs/reviews/f1-a18-reminders-tamper-review-r2.md`(verdict: approve)|
| 軽微-1 | F-4 のマイグレーション名が「0035」と省略 | **解消** | `0035_ledger_truncate_guard.sql` にフルネーム化。main の `migrations/` に同名ファイルの実在を確認(blob cc5e848) |
| 軽微-2 | `reminders-status-tamper-detection` の superseded 化への言及欠落 | **解消** | F-1 行末尾に「既存リマインダー `reminders-status-tamper-detection` は superseded 化」を明記。main の `ops/reminders.yaml` 該当エントリが `status: superseded`(superseded_by: T-020 A-18-9)であることを確認 |

## 3. 新規問題の検査

- **PR #145 本文**: 旧本文の「reminders.yaml は保護領域」の誤記は修正済み。現本文の補足は「reminders.yaml は protected_areas 未登録(F-1 是正 PR #135 は登録を不採用とし A-18-9 事後検知へ転換)」と正しい記述になっており、本 PR を独立審査+みなし承認手続に載せる根拠を「監査裁定書(監査コード領域の成果物)への追記」に正しく置いている
- **執筆規格**: 追加注記は一文アーギュメント先行(「§3 の当初是正案は protected_areas 登録(事前禁止)だったが、実装検討で不採用とし、事後検知へ転換した」)。レベル1のファクト(35%=166/473、Contents API 直接書込)には出典(PR #135 本文)、承認の事実には出典(PR #135 の独立役員審査)が付く。規格適合
- **軽微-1(r2 新規・非ブロッキング)**: 注記の「**証憑**なし終端遷移」は用語ゆれ。A-18-9 の実装(`src/ryza/audit/a18.py` L69, L2526)と PR #135 本文は一貫して「**証跡**(SHA/PR/Issue/URL)なし」を用いる。「証憑」は 00-system-design で会計の evidence(evidence_id・証憑ストア・ゼロトレランス領域「証憑の欠損・改竄」)に予約された定義済み用語であり、リマインダー台帳の参照痕跡に流用すると将来の監査人が A-1(仕訳と証憑の突合)系の統制と混同しうる。「証跡なし終端遷移」への修正が望ましいが、指示対象は文脈から一意でありマージをブロックしない
- 上記以外に、追加された記述で一次資料と矛盾するものは見つからなかった。§6 の他の行(F-2〜F-14・派生3件)は本修正で変更されておらず、r1 で審査済みの範囲

## 4. 反対意見書(この approve が間違っている場合の理由トップ3+代替案)

1. **「§4 を検出統制で満たす」という読み替えは、元所見 A-12-15 の残余リスクを過小評価している可能性がある**。A-18-9 は3パターン(pending 期日延期・pending 削除・証跡なし終端遷移)しか見ておらず、たとえば pending エントリの `action.body` 文面の無承認改変や、`what` の書き換えは検出対象外(a18.py の注記自体が「コメントや what の変更は無音で通す」と自認)。「無承認で書き換え可能」という元所見は、狭義には今も真である。**代替案**: §6 注記に A-18-9 の検出範囲の限界(3パターン限定)を1文追記させてから approve する。ただしこの限界は PR #135 とその独立審査(approve)で開示・受容済みであり、裁定書追記 PR に再審を求めるのは審査の二重化になるため、本審査では採らなかった
2. **「転換の妥当性は PR #135 の独立役員審査で承認済み」は、審査が『転換そのもの』を独立の争点として判定した証拠としては弱い可能性がある**。f1-a18-reminders-tamper-review-r1/r2 は A-18-9 実装の審査であり、protected_areas 不採用は PR 本文の前提として与えられていた。**代替案**: 表現を「PR #135(不採用の理由を本文に記載)が独立役員審査 approve を経て統合された」に弱める。ただし審査対象 PR の本文に明記された設計判断は審査範囲に含まれるのが本プロジェクトの運用であり、実質は変わらない
3. **35%(166/473)という実測値を本審査は再計測していない** — PR #135 本文からの転記一致のみを確認した。数値自体が誤っていれば裁定書は誤記を継承する。**代替案**: `git log --since` で再計測してから approve する。ただし本 PR の主張は「PR #135 にそう記録されている」であり出典明示の形を取っているため、一次資料側の検証は PR #135 の審査責任範囲とした

## 5. verdict

r1 の4所見は全件解消。新規問題は軽微1件(証憑/証跡の用語ゆれ・非ブロッキング、フォローアップでの修正を推奨)。**verdict: approve**
