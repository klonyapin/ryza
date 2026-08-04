---
review: f1-a18-reminders-tamper-r2
reviewed_sha: d6c7b9d1b3b44e60e6994bd7b300d6e216904334
reviewer: independent-review-agent (opus)
review_date: 2026-08-04
verdict: approve
---

# 独立審査記録(第2ラウンド): A-18-9 reminders 台帳改ざん検査(Issue #117 / T-020)

対象ブランチ: f1-a18-reminders-tamper
対象コミット: d6c7b9d(reviewed_sha)
r2 対応の追加コミット: 69e033b(F-1〜F-6 実装 +339/-45 audit・+7/-4 weekly)/ ccd8841(F-11 テスト 8 件 +212)/ d6c7b9d(r1 意見書保存 + F-7/F-9/F-10 の追随フォロー登録)
r1 意見書: `docs/reviews/f1-a18-reminders-tamper-review-r1.md`(request_changes、F-1〜F-11)

---

## §1 r1 指摘の解消照合

| # | r1 重要度 | 内容 | r2 での対応 | 判定 |
|---|---|---|---|---|
| F-1 | **重大** | 実履歴で所見 52 件(受け入れ基準「所見ゼロ」不合格)。証跡 regex がファイルパス+日付運用に不整合・A-18-9 固有基準コミット無し・受容経路無し | (a) `REMINDER_TAMPER_BASELINE_COMMIT = d232a56` を導入(A-18-4/7 と同じ「前向き適用」流儀)+(b) `_REMINDER_FIRED_EVIDENCE_RE` で weekly.py の `fired: <ISO日付>` を証跡として認識(`+` 行限定)+(c) `kind: a18-9` の受容経路を実装(index/partition/embed 別枠+notes)。実履歴実行(reviewed_sha)で **findings=0 / checked=2 / trailered=0** を確認、origin/main へのローカルマージ後も findings=0 / checked=8 / trailered=1 | **解消**(§4 動的確認) |
| F-2 | 重要 | 偽 Approved トレーラの完全対象外化(存在検査のみ) | `trailered` を返り値・run_a18 結果・embed に分母として開示(0 件でも `Approved トレーラ付きで対象外 N 件(存在検査のみ・参照実在照合は未実装)` と常に表記)。docstring に「既知の限界」として明記し、実在照合(pr_verifier 流用)は `a18-9-review-followups`(2026-11-01)へ登録 | **部分解消**(可視化+文書化。**回避経路自体は残る**が独立役員に「見える」形にはなった — §3 新規所見 N-1 参照) |
| F-3 | 重要 | 非終端・非 pending status への遷移が無音(fail-open) | 終端ホワイトリスト `_REMINDER_TERMINAL_STATES` を撤廃し、pending → pending 以外の**全遷移**に証跡必須化(kind 名は互換のため `terminal_without_evidence` 維持)。動的 probe で「pending→paused→done の2コミット分割」も step1 で捕捉されることを確認 | **解消** |
| F-4 | 重要 | マージコミット常時スキップ(`diff-tree` にパス名が出ない) | touched 判定を `_blob_sha` の第1親比較に置換。マージも自然に対象化(evil merge)。テスト `test_a18_9_merge_commit_tampering_is_detected` で固定。誤コメント2箇所も docstring 書き換えで訂正 | **解消** |
| F-5 | 中 | クオート外し(datetime.date)で後ろ倒し検出漏れ・date_after 条件削除が無音 | `_reminder_deadline` を `str(d).strip()` 正規化に修正。`deadline_removed` kind を新設し、テスト2件で固定 | **解消** |
| F-6 | 中 | ops コミット(7b12487)が最初の `superseded` を導入したが weekly.py の終端語彙外 → 2026-08-25 に誤発火の恐れ | `TERMINAL_STATUS_PREFIXES = ("fired","done","superseded","cancelled")` に拡張。docstring に A-18-9 との語彙整合コメント。**weekly.py は保護領域外**(governance.yaml protected_areas に無し)なので変更経路の懸念は無し(§3 で確認) | **解消** |
| F-7 | 中 | 生テキスト証跡の laundering(同エントリ内の既存参照行を編集すると削除行の参照で成立) | 追随フォロー `a18-9-review-followups`(2026-11-01)登録。内容は「+ 行のみ対象への絞り込みと巻き添え測定」を求める設計裁定 | **追随フォロー化**(仕様の内在限界として文書化 — 妥当) |
| F-8 | 中 | 受容(kind: a18-9)経路が無い | `ACK_KIND_REMINDER_TAMPER` / `acknowledged_reminder_tamper_index` / `partition_acknowledged_reminder_tamper` を実装。`has_findings` は未受容分のみを見る。受容済みは embed の別枠で必ず開示。テスト `test_a18_9_acknowledged_finding_does_not_alert` で固定 | **解消** |
| F-9 | 軽微 | 偽証跡(7〜8桁数字列・hex-only 英単語)= 検出漏れ方向 | 追随フォロー登録(SHA regex の絞り込み+巻き添え測定を要求) | **追随フォロー化**(妥当) |
| F-10 | 軽微 | 履歴線形の全走査(27.9 秒 → 1 年後 20〜30 分見込み) | 追随フォロー登録(`rev-list -- ops/reminders.yaml` 事前絞り込み+`--first-parent` 検証を要求)。r2 では実測 22.8 秒(reviewed_sha 時点)で許容範囲 | **追随フォロー化**(妥当) |
| F-11 | 軽微 | ハンク帰属分離・非終端遷移・2コミット分割・マージ・非クオート日付・cancelled のテスト無し | ccd8841 で 8 件追加(hunk 分離・pending→paused・merge 改ざん・非クオート日付 defer・deadline 削除・fired 表記の証跡認識・trailered 分母開示・ack 経路)。「2コミット分割」と「cancelled」の直接テストは無いが、前者は F-3 撤廃で step1 側が単独テストと同じ経路で捕まる(§4 の probe で確認)。cancelled 単独テストは無いが F-3 の撤廃で全遷移が同じロジックを通るため回帰保護としては十分 | **解消**(2コミット分割・cancelled の追加テストは推奨だが必須ではない) |

**要約**: F-1・F-3〜F-6・F-8・F-11 は実装で解消、F-7・F-9・F-10 は追随フォロー化(妥当)、F-2 は限界を可視化した部分解消。

---

## §2 実履歴での動作確認

**worktree**: `/tmp/review-f1r2` を FETCH_HEAD で作成(reviewed_sha = d6c7b9d)。

### 2.1 reviewed_sha 単体
```
PYTHONPATH=/tmp/review-f1r2/src .venv/bin/python -c "from ryza.audit.a18 import check_reminder_tampering; r=check_reminder_tampering('/tmp/review-f1r2'); print(r)"
→ findings=0 checked=2 unparseable=0 trailered=0
```
基準 `d232a56` 以降の `ops/reminders.yaml` 変化があるコミットは 2 件(本 PR の 7b12487 と d6c7b9d のうち reminders 変更を含むもの)で、両者とも所見なし。T-020 受け入れ基準「所見ゼロ」を**満たす**。

### 2.2 origin/main へのローカルマージ後
`git merge --no-ff --no-edit origin/main` を worktree 上でローカル実行(push しない)。マージは自動成功(コンフリクト無し)。
```
→ findings=0 checked=8 unparseable=0 trailered=1
```
基準以降に main が進んでいる分(20 コミット中 reminders.yaml に触れているもの)を含めて所見ゼロ。trailered=1 は正当な Approved 付き変更が分母として可視化されている(F-2 の可視化要件を満たす)。

### 2.3 テストスイート
```
PYTHONPATH=/tmp/review-f1r2/src .venv/bin/python -m pytest tests/audit/ -q
→ 209 passed in 318.76s
```
想定 209 件と一致(r1 の 201 件 + F-11 対応 8 件)。DB 依存の失敗は無し。ruff は本審査では未実行(スコープ外)。

### 2.4 ベースライン方式の妥当性
- **origin/main の祖先か**: `git merge-base --is-ancestor d232a56 origin/main` → **祖先である**(20 コミット遡る)。前方適用の起点として整合。
- **前方限定適用の趣旨**: A-18-4 = `PR_RULE_BASELINE_COMMIT` / A-18-7 = `DEEMED_RECORD_BASELINE_COMMIT` と同型で、遡及所見を避ける先例と一致。docstring にも 52 件の「時間的な爆風」を避ける理由が明記(a18.py L299 帯コメント)。
- **改ざん耐性**: 定数は `src/ryza/audit/a18.py` 内=**保護領域**(governance.yaml protected_areas に `src/ryza/audit/**` が含まれる — 確認済み)。定数の書き換えは A-18-1 の PR 突合対象になるので、A-18-9 単独ではなく既存の統制に載る。妥当。
- **軽微所見(N-3)**: r2 コミットメッセージは「origin/main HEAD を採用」と書くが、実際は 20 コミット遡った `d232a56`。作業時点の origin/main HEAD だった可能性はあるが、審査時点の HEAD(`68a3a1b`)とは異なる。事実関係の記録が誤っている(意味は前向き適用として妥当なので機能的影響は無し)。

---

## §3 検出対象外ケースの残余(新規所見)

r2 実装(blob 直接比較・全遷移証憑必須・fired 表記認識)を probe 7 件で追試して残る意味的変更のケース:

### N-1(重要度: 中・**追随フォロー相当**)偽 Approved トレーラの完全対象外化は残る
- **状況**: `Approved: https://github.com/x/y/pull/999999`(架空 PR)を書いたコミットで期日を後ろ倒し → probe で `findings=0 / trailered=1 / checked=0` を確認。トレーラは存在検査のみで実在照合が無い。
- **緩和**: r2 で `trailered` が分母として embed に **必ず**表示される(0 件でも表記)ため、独立役員が「トレーラ付きの件数」と「実際の承認記録件数」を目視で突合できる。
- **既知の限界**: docstring に明記済み。追随フォロー `a18-9-review-followups` に「pr_verifier 流用」を含める形で登録済み(2026-11-01)。
- **判定**: 検出そのものは実装されていないが、r1 F-2 の中核要求(「静かに件数から外さない」)は満たしており、可視化+フォロー登録で妥当。**verdict を左右しない**が、フォローの実施は必ず必要。

### N-2(重要度: 低)laundering(F-7)は明示的に据え置き
- 前回 F-7(同エントリ内の既存参照行を編集すると `-` 行の参照で証跡成立)は追随フォロー化。probe で `title: alpha updated https://a.b/c` を同時追加+status: done で `findings=0` を確認。仕様上の内在限界として文書化されているので **verdict を左右しない**。

### N-3(重要度: 情報)コミットメッセージの記述誤り
69e033b のメッセージは「`REMINDER_TAMPER_BASELINE_COMMIT`(origin/main HEAD を採用)」と書くが、実際は審査時点の origin/main HEAD(68a3a1b)から 20 コミット遡った d232a56。作業時点で origin/main HEAD だった可能性はあるが、監査記録として precise ではない。機能的影響なし・**verdict を左右しない**。

### N-4(重要度: 情報)`trailered_suffix` は常に表示
embed の A-18-9 セクションで、trailered=0 でも `Approved トレーラ付きで対象外 0 件(存在検査のみ・参照実在照合は未実装)` と冗長な注記が毎週出る。「参照実在照合は未実装」は独立役員視点では見える必要があるが、代表向けの表示としては冗長感がある。F-2 の可視化要求とは両立する範囲での UX の微調整余地。**verdict を左右しない**。

### 肯定的確認(検出動作の確認)
- 2コミット分割(pending→paused→done):step1 で **terminal_without_evidence** を検出。F-3 撤廃が効いている。
- 2コミット分割(pending→paused→削除):step1 で検出、step2 は before が pending でないため無音 — F-3 の分割回避は step1 で閉じられた。
- status フィールドを削除(欠落)した pending エントリ:`terminal_without_evidence` として検出(to_status=""、fail-closed 方向)。
- deadline_removed の証跡なし遷移:期待通り検出。
- fired 表記の除去行(`-`)は証跡と誤認しない:正規表現の `^\+` 制約で確認済み。

### r2 で追加テストが望ましい残り(**必須ではない**、任意フォロー)
- 2コミット分割(F-3 撤廃の直接テスト — probe で確認済みだが回帰保護なし)
- cancelled 単独遷移(F-3 撤廃で全遷移が同一経路のため代替済みだが個別テストが無い)
- 偽 Approved トレーラの `trailered` カウント(F-2 の可視化要件は既存テストで開示行はカバー、しかし架空 PR 番号による bypass の実演テストは無し)
- ローカルマージ後の履歴で findings=0 が保たれること(§2.2 で本審査は目視確認、テストとしての固定は無し)

---

## §4 追随フォロー(reminders 登録)の内容確認

`a18-9-review-followups`(2026-11-01)の body は F-7・F-9・F-10 の各項目について:
- **再評価の材料**(何を測るか)を具体的に列挙(F-7: 偽陽性件数の実データ・+ 行絞り込みの巻き添え測定・pr_verifier 流用/F-9: 見逃し実例・regex 絞り込みの巻き添え・fired 拡張の稼働確認/F-10: 週次実行時間実測・first-parent の evil merge 検出欠落・1 年後予測)
- **出典**(意見書パス+実装ファイル)を記載

**判定**: r1 の指摘の趣旨(「実データで再評価する」「絞り込みが正当運用を巻き添えにしないか測る」)を保存しており妥当。F-7 に対しては pr_verifier 流用まで踏み込んだ設計裁定の材料が並んでいる点も良い。**必要事項の欠落なし**。

---

## §5 verdict の根拠

- **重大所見**: 無し(F-1 は解消・実履歴 findings=0・マージ後も findings=0)。
- **重要所見の解消**: F-2(可視化+文書化+フォロー登録)・F-3(実装)・F-4(実装)。
- **中所見の解消**: F-5(実装)・F-6(実装)・F-7(追随フォロー化)・F-8(実装)。
- **軽微所見**: F-9・F-10・F-11 のいずれも追随フォロー化ないしテスト追加で対応。
- **新規所見(§3)**: N-1〜N-4 のうち verdict を左右するものは無し。N-1 は r1 F-2 の中核要求(可視化)を満たした部分解消で、実在照合は追随フォロー登録済み。N-2 は既知の内在限界として据え置き妥当。N-3・N-4 は情報レベル。
- **テスト**: 209 passed(意見書指示の 209 件想定と一致)。DB 不要で実行成功。
- **実履歴**: 単体・マージ後ともに findings=0(T-020 受け入れ基準を満たす)。
- **ベースライン**: origin/main の祖先(20 コミット遡り)・保護領域内定数・A-18-4/7 の前例と整合。

r1 の request_changes の理由(F-1 の実履歴不合格・F-6 の 2026-08-25 誤発火・F-2〜F-4 の対象外経路・誤コメント2箇所)は**全て解消**。残る N-1 は既存の A-18-1 実在照合機構を A-18-9 に流用する設計裁定の題材で、r2 内で決着させるより実データを持ってフォローで議論する方が良い(追随フォロー化に賛成)。

**verdict: approve**

## 任意フォロー(承認と両立、非ブロッキング)
- 2コミット分割・cancelled 単独遷移・架空 Approved トレーラ bypass の回帰テスト追加
- コミットメッセージ「origin/main HEAD を採用」の記述を「origin/main の直近祖先」に訂正(記録の正確性向上)
- embed の `trailered_suffix` を trailered=0 のときは短縮する(UX 微調整)

## 審査手順の記録
1. r1 意見書の読み込み(F-1〜F-11 と再提出要求の把握)
2. r2 対応コミット3件の diff 精読(69e033b・ccd8841・d6c7b9d)
3. worktree 作成(`git worktree add /tmp/review-f1r2 FETCH_HEAD`)
4. reviewed_sha 単体で `check_reminder_tampering` 実行 → findings=0
5. origin/main をローカルマージして再実行 → findings=0
6. reset --hard で reviewed_sha に戻し、`pytest tests/audit/ -q` → 209 passed
7. residual probes 7 件(2コミット分割・laundering・偽トレーラ・deadline_removed・status 欠落 等)
8. ベースライン祖先関係・保護領域収容の確認
9. 追随フォロー登録内容の精査
10. リポジトリへの書き込みは行っていない(worktree はレビュー後に `git worktree remove --force` で除去する)
