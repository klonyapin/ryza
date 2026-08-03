# みなし承認の記録・通知配線 独立役員審査(2026-08-04)

対象: `governance/notices.py`(新規)・`governance/decisions.py` CLI・`audit/a18.py`(保護領域)・`bot/main.py`(保護領域)。判定は **条件付き承認**。

- **重大-1**: A-18 の実在照合は `Approved:` トレーラの ID 形式(`a18.py:101`)しか見ないが、本リポジトリの履歴は全件 PR URL であり、deemed 記録の `proposal_ref` も同じ PR URL。したがって否認済み承認は従来どおり受理され、0021 C-5 の穴は実運用上ふさがっていない。是正は `current_decisions.proposal_ref = ref` の照合(GitHub API 不要)。
- **重要-2**: 裸数字 ID は Issue 番号と衝突しうる。不在は fail-closed だが偶然一致は fail-open。`decision:` 接頭辞を必須化する。
- **重要-3**: 「通知」を outbox 投入と定義したため、配送失敗時に「通知なき発効」が成立する。`notice_message_id` は実装されたが呼び出し元が無く、滞留検知は日報の集計のみ(その日報も同じ outbox を通る)。A-18 に未配送 deemed の検査を追加すること。
- **重要-4**: `press.outbox` に enqueue できる主体は誰でも `deemed:` フッター付き embed を #承認 に出せ、任意の決定を指す否認ボタンを代表に見せられる(`bot/main.py:440-461`)。配送時に「対応する deemed 決定が実在するか」を照合してからボタンを付けるべき。
- **重要-5**: オーナー検証は呼び出し側供給の 2 引数比較であり、コード経路からの偽装を防げない。かつ `apply_veto` / `withdraw_veto` は手元の `run_id` を `record_veto` に渡しておらず(`notices.py:358,396`)、否認の出所が事後に区別できない。事後検知という唯一の防御が塞がれている。
- **中**: 権限検査が DB 読取の後で既存 `_require_owner` 流儀と不一致・拒否の痕跡が残らない(-6)、reminders を `done` にしたが自動起票トリガは未実装で呼び出し元 0 件(-7)、`defer()` 無しで 3 秒制限に触れると「commit 済みなのに失敗表示」(-8)。
- **軽微**: マーカー衝突で deemed 通知に承認ボタンが付きうる(-9)、`verify_decision_refs` の「いずれか 1 つ有効なら受理」が自身の docstring と矛盾(-10)、A-18 の長時間 idle-in-transaction(-11)、原子性テストが片方向のみ・CLI の IDLE 分岐と Bot 配線が未カバー(-12)。
- **肯定**: autocommit 拒否は書込前で有効、PR マージによる否認済みトレーラの救済を封じた判断は正しく、Kill Switch 経路の既存保証の弱体化は diff 全体を確認して認められなかった(追加のみ・webhook 条件は AND 追加)。
- **統合条件**: 重大-1、重要-5 後段(`run_id` 受け渡し)、中-7 を同一 PR で是正。他は期限付きで `ops/reminders.yaml` へ登録。本 PR 自身の承認記録も PR URL トレーラになるため、重大-1 未是正のままでは自らが照合対象外になる。

## 設計リード裁定(2026-08-04 追記)

- 本 PR 内で是正: 重大-1(URL の proposal_ref 照合)、重要-2(decision: 接頭辞必須・裸数字は照合不能として開示)、
  重要-3(A-18 に未配送 deemed 検査を追加)、重要-4(配送時に対応 deemed 決定の実在照合をしてからボタン付与)、
  重要-5 後段(run_id 受け渡し)、中-6(オーナー検証を先頭へ+拒否を別接続で記録)、中-7(reminders 記述の実態化)、
  中-8(defer+to_thread)、軽微-9(マーカー検証と判定順)、軽微-10(否認済み参照の独立列挙)。
- リマインダー登録: 重要-5 前段は既存 `veto-origin-column` の優先度note追記、軽微-11(autocommit 照合接続)、
  軽微-12(原子性の逆方向フォールト注入・CLI IDLE 分岐)を `deemed-wiring-followup`(期限 2026-08-17)に。

## 是正の実装記録(2026-08-04・実装エージェント)

裁定の全項目を本 PR で実装した。対応箇所は以下のとおり。

| 指摘 | 対応 |
| --- | --- |
| 重大-1 | `a18._verdict_for_ref` が非 ID 参照を `current_decisions.proposal_ref` 一致で解決(PR URL が主経路)。行が無ければ従来の存在検査 |
| 重要-2 | `_DECISION_REF_RE` を `decision:<id>` のみに限定。裸の数字は `unverifiable` として notes 開示 |
| 重要-3 | A-18-5 `check_unnotified_deemed`(`outbox:<id>` が 60 分超未配送 → 違反・urgent) |
| 重要-4 | `notices.resolve_deemed_view` が deemed 決定の実在(かつ未否認)を照合してからボタン付与。不在は警告ログ+ボタンなし(fail-closed) |
| 重要-5 後段 | `apply_veto` / `withdraw_veto` が `run_id` を否認記録へ渡す |
| 中-6 | `_require_owner` を DB 読取前に実行。拒否は `record_denied_attempt` が別接続(autocommit)で #運営 へ記録 |
| 中-7 | reminders の done 記述を実態化し、自動起票を `deemed-auto-announce`(期限 2026-08-17)へ分割 |
| 中-8 | `_run_governance_action` が `defer(ephemeral)` + `asyncio.to_thread` |
| 軽微-9 | `build_deemed_notice_embed` がマーカー混入・空参照を拒否。配送判定は deemed 優先 |
| 軽微-10 | 受理した場合でも否認済み参照は `trailer_findings` に列挙し embed の専用フィールドへ |
| 軽微-11・12 | `deemed-wiring-followup`(期限 2026-08-17)に登録 |
| 重要-5 前段 | `veto-origin-column` の body に判断材料の確定を追記(origin 列を「足す」方向で再評価) |

## 後続の是正(2026-08-04・reminders `deemed-auto-announce` / `deemed-wiring-followup`)

期限付きで登録した3件を前倒しで実装した。**中-7(呼び出し元 0 件)は完全解消ではない** —
GitHub イベント受信基盤が無いため PR 起票の自動検知は依然できず、下記は「叩き忘れを事後に
検出する」+「叩く手間を減らす」の2方向からの次善策である。

| 指摘 | 対応 |
| --- | --- |
| 中-7(自動起票) | 監査に **A-18-7**(`check_unrecorded_protected_prs`)を新設。`DEEMED_RECORD_BASELINE_COMMIT` 以降の保護領域 PR マージのうち、トレーラの参照でも PR 番号(`proposal_ref` の `.../pull/<N>`)でも承認記録が引けないものを列挙する。叩き忘れは週次で #運営 に出る |
| 中-7(手間) | `--deemed-for-pr <PR番号>` を追加。`gh api` で PR タイトル・URL・変更ファイルを取得し文面を自動生成する。`--review`(独立役員審査の参照)を必須にして「審査前の発効」をワンコマンドで作れないようにした |
| 軽微-11 | `run_a18_readonly` を新設し、照合は autocommit・`default_transaction_read_only` の別接続で完結させて閉じてから報告用の書込接続を開く。git 走査中の idle-in-transaction が消える |
| 軽微-12 | 逆方向のフォールト注入(`enqueue` が行を書いた直後に落ちる/`run_id` NOT NULL 違反)で記録・通知の双方が消えることを検証。CLI の IDLE 分岐は「IDLE 接続で `announce` しても commit に化けない」+ CLI 本経路の end-to-end で被覆 |

**トレーラ v2 ライン(PR #84)との統合結果**: A-18-7 は独立した関数
(`_decision_exists` / `decision_for_pr_number` / `check_unrecorded_protected_prs`)に閉じて
あり、判定ロジックの競合は無かった。競合したのは `run_a18` / `run_and_report` の引数
(`deemed_since_commit` と v2 の `verify_prs`)と `build_alert_embed` の field 追加位置だけで、
どちらも併記で解決した。`run_and_report` は `run_a18_readonly(**run_kwargs)` に委譲するため
v2 が追加した `verify_prs` はそのまま透過する。件名からの PR 番号抽出は v2 の
`pr_number_from_subject` に寄せ、本ラインの重複定義は削除した(番号の解釈を二重に持たない)。
A-18-7 は `PRVerifier` による PR 実在照合を掛けない —— 件名が偽なら対応する承認記録も引けず、
「記録が無い」として同じ経路で拾われるため、追加の API 照合なしに偽装が検出される。

**A-18-7 が拾えないもの**: PR 件名は自己申告であり(A-18-1/4 と同じ限界)、承認記録が DB の外に
ある PR は「記録なし」と判定される。後者を例外扱いする必要が出たら `acknowledged_findings` と
同型の受容記録を足すこと(黙って除外しない)。Bot 配送側の分岐(`VetoView` 付与)は discord
オブジェクトに依存するため未被覆のままで、判定ロジック部分(`resolve_deemed_view`)のみ被覆済み。

**PR 承継ライン(`a18-ack-l1`)との統合について**: 承認の有効性判定を
`a18.trailer_approves(conn, message, trailer)` に集約した。承継規則が「マージ M のトレーラで
配下を承認する」と判定する箇所は、素の `has_approval_trailer` ではなく本関数を通すこと —
通さないと**否認済みの PR トレーラが配下コミット群に承継され**、重大-1 の是正が承継経路から
迂回される。`check_protected_commits` の戻り値は本ラインが `(violations, checked,
trailer_findings)`、承継ラインが `(violations, inherited, checked)` であり、統合時は
両方の所見を持つ形へ揃える必要がある(関心は独立しており、判定順の競合は無い)。
