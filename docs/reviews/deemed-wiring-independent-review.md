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

### 後続配線審査への是正(2026-08-04・設計リード裁定 → 実装エージェント)

| 指摘 | 対応 |
| --- | --- |
| 後-1 | `--review` を真に必須化。`--deemed-for-pr` は `--notice` 併用でも必須、旧来形も `--kind pr` では必須(`REVIEW_REQUIRED_KINDS`)。他 kind に課さないのは、戦略昇格・IPS 改訂などは独立役員審査が必ずしも前置される手続ではなく、一律必須化が正当な発効経路を塞ぐため。`--notice` で差し替えた文面にも審査参照の行を足す(`_with_review_line`)ので、必須化が「引数を渡させるだけ」にならない。審査の実証コマンド2本(`--deemed-for-pr 99 --notice x` / `--deemed --kind pr --notice x`)が rc=1 になるテストを追加 |
| 後-2 | `--review` が**実在検査のない形式要件**である旨を CLI help・`_resolve_deemed_args` / `build_pr_notice` の docstring・モジュール docstring に開示。構造化列と実在検査は `decision-reviewed-sha` の body に追記(審査対象 SHA 列と同じ列に載せること) |
| 後-3 | 帰属の判定を `proposal_ref == https://github.com/<slug>/pull/<N>` の完全一致1本に変更。トレーラ参照は、指す決定の `proposal_ref` が当該 PR のときだけ帰属と認める。実証ケース(#601 の記録+#602 へのトレーラ複写)が検出されるテストを追加。非 PR の `proposal_ref`(IPS 改訂等)も帰属とは認めず、理由に参照先を出して切り分けられるようにした |
| 後-4 | 緑に分母を表示。`検査対象 N 件` を必ず書き、N=0 は ✅ ではなく「対象 PR なし(squash マージ運用へ移行した場合も同じ表示になる)」と明示する。所見ありの見出しも `M/N 件` |
| 後-5 | `LIKE '%/pull/<N>'` は**帰属判定ではなく所見の材料**に降格。他リポジトリの記録は「PR 番号は一致するが別リポジトリの記録」として理由に出し、救済しない。`origin` を解決できない実行は末尾一致まで落ちるので、その旨を notes に開示する(`UnrecordedPRScan.repo_slug`) |
| 後-6 | 「件名が偽なら記録も引けないので偽装は検出される」を撤回。`--deemed` は `proposal_ref` を無検証で受けるため架空 PR 番号の記録は作れる。偽装の封鎖は A-18-1 の `PRVerifier`(**API 不達時は fail-open**)の担当であり、A-18-7 はそれに依存する、と docstring・reminders を訂正 |
| 後-7 | `deemed-webhook-trigger` の body に期限後退(8/17 → 9/14)の理由と、残リスク(最大7日の通知なき発効の窓は「永久に気づかれない」→「最大7日で気づく」に縮小しただけで閉じていない)を明記。許容できない場合は日付を戻す判断材料として書いた |
| 後-8 | `run_a18_readonly` の記述を「read-only 原則の執行点」→「**うっかり書込の検出点**」に訂正(セッション既定であって権限境界ではなく、`SET TRANSACTION READ WRITE` で上書き可)。テストの docstring も同様 |
| 後-9 | baseline は動かさない。PR #84 が初回の対象になり ⚠️ が出るのは統制が効いた証拠であり、記録の登録で消す(検査の無効化はしない) |
| 肯定への訂正 | `VetoView` 未被覆の根拠を「フェイク構築コスト」から「`/veto` という冗長経路があり否認権が失われないこと」へ差し替え(reminders body) |

**トレーラ v2 ライン(PR #84)との統合結果**: A-18-7 は独立した関数
(`_decision_exists` / `decision_for_pr_number` / `check_unrecorded_protected_prs`)に閉じて
あり、判定ロジックの競合は無かった。競合したのは `run_a18` / `run_and_report` の引数
(`deemed_since_commit` と v2 の `verify_prs`)と `build_alert_embed` の field 追加位置だけで、
どちらも併記で解決した。`run_and_report` は `run_a18_readonly(**run_kwargs)` に委譲するため
v2 が追加した `verify_prs` はそのまま透過する。件名からの PR 番号抽出は v2 の
`pr_number_from_subject` に寄せ、本ラインの重複定義は削除した(番号の解釈を二重に持たない)。
A-18-7 は `PRVerifier` による PR 実在照合を自分では掛けない。**ただしこれは「偽装を検出できる」
という意味ではない**(後-6 で訂正): `--deemed` は `proposal_ref` を無検証で受けるため、架空の PR
番号を指す記録は作れる。偽装の封鎖は A-18-1 の `PRVerifier`(API 不達時は fail-open で縮退し、
件数は報告に出る)が担い、A-18-7 はその照合に依存する。

**A-18-7 が拾えないもの**: PR 件名は自己申告であり(A-18-1/4 と同じ限界)、承認記録が DB の外に
ある PR は「記録なし」と判定される。後者を例外扱いする必要が出たら `acknowledged_findings` と
同型の受容記録を足すこと(黙って除外しない)。`origin` を解決できない実行では帰属照合が PR 番号の
末尾一致まで落ちる(notes に開示)。Bot 配送側の分岐(`VetoView` 付与)は未被覆だが、`/veto`
コマンドという冗長経路があるため否認権は失われない(判定ロジック `resolve_deemed_view` は被覆済み)。

## 後続配線審査記録(2026-08-04・独立役員)

対象は A-18-7・`--deemed-for-pr`・`run_a18_readonly`・フォールト注入。判定は **条件付き承認**(統合前に 後-1/3/4 を是正)。

- **後-1(重要)**: `--review` は必須ではない。`_resolve_deemed_args` の条件は `not (args.review or args.notice)` で、`--deemed-for-pr 99 --notice x` は審査参照ゼロで rc=0(実証済み)。旧来の `--deemed --proposal-ref <PR URL> --kind pr --notice x` も従来どおりワンコマンド。「審査前の発効をワンコマンドで作れないようにした」は成立していないので、`--deemed-for-pr` では `--notice` 併用時も `--review` を必須にするか、記述を実態に合わせること。
- **後-2(重要)**: `--review` は任意文字列で実在検査が無く(`--review 嘘` が通る)、値は通知本文にしか残らないため事後に監査できない。形式要件にとどまる旨を docstring と本文書に開示すること。
- **後-3(重要)**: A-18-7 は**別 PR の承認記録で緑になる**。`_decision_exists` は参照先決定の実在しか見ず、その決定がこの PR のものかを見ない。PR #601 だけ `--deemed` し #601/#602 の両方に同じトレーラを書くと findings は空(一時テストで実証・削除済み)。追い PR のトレーラ複写という**事故で起きやすい**経路なので、救済をこの PR 番号に一致する `proposal_ref` に限るか、不一致は `notes` に出すこと。
- **後-4(重要)**: 緑に分母が無い。保護領域 PR が 0 件の場合も、squash merge へ移行して `Merge pull request #N` 形式が消えた場合も同じ「✅ 記録漏れなし」になる。PR 実在照合が縮退件数を必ず出す流儀(重要-4)と不整合。`検査対象 N 件 / 漏れ 0 件` と分母を書くこと。
- **後-5(中)**: `LIKE '%/pull/<N>'` はリポジトリ部分を見ない。他リポの `/pull/610` の記録が自リポ #610 を救済する(実証済み)。単一リポ運用では実害は小さいが fail-closed の検査に fail-open が 1 箇所入る。
- **後-6(中)**: 「件名が偽なら承認記録も引けないので偽装は追加照合なしに検出される」は不正確。`--deemed` は `proposal_ref` を無検証で受けるため架空 PR 番号の記録は作れる。偽装を封じているのは A-18-1 の `PRVerifier`(API 不達時は fail-open)であり、A-18-7 は**それに依存する**と書き換えること。
- **後-7(中)**: `deemed-auto-announce` を done にした分割自体は妥当(残課題が無条件トリガで登録され、残リスクが body に具体化されている)。ただし期限が 2026-08-17 → 2026-09-14 へ 4 週後退しており、残リスク(最大 7 日の通知なき発効の窓)は不変。延長理由を body に書くか日付を据え置くこと。
- **後-8(軽微)**: `SET default_transaction_read_only` はセッション既定であって権限境界ではない(`SET TRANSACTION READ WRITE` で上書き可・ロールは書込権限を保持)。「read-only 原則の執行点」ではなく「うっかり書込の検出点」と記述するのが正確。接続の切替順序(検査接続を閉じてから書込接続を開く)自体は正しく、テストが `verify_closed` / `same_conn` / `opened == 2` で押さえている。
- **後-9(注意)**: baseline 649c4e2 の直後 cbc805f(PR #84)は `config/governance.yaml` と `src/ryza/audit/a18.py` に触れるため、A-18-7 の最初の対象になる。対応記録が無ければ初回実行で ⚠️ が出るが、これは統制が効いた証拠である。**ノイズを理由に baseline を後ろ倒しする対応は検査の無効化と同義**であり禁じる。
- **肯定(反対点を探して見つからなかったもの)**: 否認済みを「記録はある」として A-18-7 で鳴らさない判断、A-18-7 を urgent に含めない理由付け、逆方向フォールト注入 2 種(Python 例外・NOT NULL 違反による tx abort)が SAVEPOINT の両方向と呼び出し側 tx の生存まで押さえている点、IDLE 接続で `transaction()` が COMMIT に化けない検証。`bot/main.py` の `VetoView` 未被覆も結論は妥当だが、根拠は「フェイク構築コスト」ではなく **`/veto` コマンドという冗長経路があり否認権が失われないこと**に置くべき(コスト論を根拠にすると安全弁が 1 本の箇所も同じ論法で落とせる)。
- 検証: `tests/audit/test_a18.py` + `tests/governance/` 379 passed、`ruff check src tests` clean。

**PR 承継ライン(`a18-ack-l1`)との統合について**: 承認の有効性判定を
`a18.trailer_approves(conn, message, trailer)` に集約した。承継規則が「マージ M のトレーラで
配下を承認する」と判定する箇所は、素の `has_approval_trailer` ではなく本関数を通すこと —
通さないと**否認済みの PR トレーラが配下コミット群に承継され**、重大-1 の是正が承継経路から
迂回される。`check_protected_commits` の戻り値は本ラインが `(violations, checked,
trailer_findings)`、承継ラインが `(violations, inherited, checked)` であり、統合時は
両方の所見を持つ形へ揃える必要がある(関心は独立しており、判定順の競合は無い)。
