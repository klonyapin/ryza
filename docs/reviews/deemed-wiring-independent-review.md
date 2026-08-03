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
