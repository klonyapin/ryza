# 独立役員意見書 — 0021_decision_vetoes / 0022_stances_source / governance.decisions writer

- 日付: 2026-08-03 / 対象: migrations/0021・0022、src/ryza/governance/decisions.py ほか(10 files, +1151/-28)
- 審査者: 独立役員(非執行・批判専任。起草者の選好は不知)
- 根拠: 定款 v0.4 第3条・第5条、config/governance.yaml、migrations/0007・0013・0015・0018・0019、docs/reviews/0019-decisions-deemed-independent-review.md(C-1 設計案A)
- 検証: 追記オンリー実装は 0015/0018 の標準に到達。view の同時刻 tie-break(vetoed_at DESC, veto_id DESC)は正。TRUNCATE 封鎖の既存表への追加は既存挙動を壊さない(TRUNCATE 使用箇所ゼロ・conftest は DELETE のみ・migrate は適用済みスキップで冪等)

## 判定: 条件付き承認

- **C-1(重大・マージ前必須)**: 0021 は「decisions を UPDATE すると Approved トレーラの意味が遡及改変される」を別表化の根拠に据えるが、その UPDATE/DELETE 自体は今も封鎖されていない(0021 は TRUNCATE のみ追加)。派生記録が不変で原本が可変という保護の逆転。是正: decisions に forbid_mutation 行トリガ+REVOKE UPDATE,DELETE(全経路 INSERT のみのため安全 — grep 確認済み)。
- **C-2(重大・マージ前必須)**: 否認を全 decision へ一般化した根拠「効力を弱める方向にしか働かない」は `reject`/`question` に対して偽。却下記録に否認1行で effective_decision='vetoed' となり、将来の阻止判定が fail-open で外れる。加えて vetoed_by は DB でもアプリでも未検証(検証は「未実装の呼び出し側」へ委譲)。是正: 否認対象を approve/deemed に限る INSERT トリガ + record_veto のオーナー検証(record_decision と同型)。
- **C-3(重大〜中)**: 否認の撤回表現が無く、誤った decision_id への1行で承認が恒久的に汚染される(UNIQUE(proposal_ref) で再記録も不可)。是正: 行種別列(veto/revert_complete/withdrawal)+ expected_proposal_ref 照合。
- **C-4〜C-6(中)**: view の最新行採用が行単位のため、情報の無い追記が revert_commit を消しうる / dashboard.fetch_decisions が decisions を直読しており否認済み承認を承認として表示(A-13 実在照合の後続仕様も同じ穴)/ 0022 の盲検除外が denylist で、新規 source・source 指定漏れが盲検へ透過(起草者自身が挙げた失敗モードに fail-open)。
- **C-7〜C-11(低)**: record_veto に SAVEPOINT 無し(deemed 側と非対称)/ run_id NULL 許容は結論として妥当だが理由「Discord は Run を持たない」は誤り(press.outbox.run_id は NOT NULL、start_run で取得可)/ REVOKE TRUNCATE は実質 no-op・所有者ロールでトリガ無効化可能で governance がロール分離リマインダーの対象外 / 3専決の対応表突合が片方向のみ(CHECK 側の余剰を検出しない)。

## 起草者の意識的逸脱3点への独立評価

(a) 否認の全 decision 一般化 = **不採**(C-2)/ (b) run_id NULL 許容 = **採、ただし付された理由は誤り**(C-8)/ (c) 盲検除外の超集合化 = **採、ただし denylist ではなく allowlist にすべき**(C-6)。

## 設計リード裁定(2026-08-03 追記)

スキーマは未適用(どの環境にも無い)ため、後続扱いの指摘も本 PR 内で修正する:

- 本 PR 内: C-1・C-2(必須)、C-3(行種別列+expected_proposal_ref)、C-4(view の列単位解決+情報の無い追記が既記録を消さないテスト)、C-5(dashboard を current_decisions へ切替+stale docstring 修正+deemed-notice-wiring リマインダー本文の是正)、C-6(盲検 allowlist 化+record_stance の事前検証対称化)、C-7(record_veto の SAVEPOINT)、C-8 のコメント訂正(「否認は代表の作為でありジョブ生成物ではない」)、C-10(ORDER BY veto_id DESC 単独へ)、C-11(CHECK 集合一致の双方向テスト)。
- リマインダー登録: C-8 の origin 列(otsr 検討込み)、C-9(i) governance スキーマのロール分離を実弾移行前提条件に追加。

## 0030(veto origin)・0031(runs.status CHECK)追記審査 — 2026-08-04 独立役員(起草者の選好は不知)

- 対象: migrations/0030_veto_origin.sql・0031_runs_status_check.sql ほか(14 files +746/-62)。C-8 の決着「origin を足す」を**妥当**と認める — 根拠「run_id では経路を識別できない(ボタンと /veto が同じ job_name)」はコードで確認。writer の origin 必須化(既定値なし)・Discord 3経路の AST 固定・view 末尾追加はいずれも適切。「バックフィル値は推定」の開示は正確で、かつ現存全環境に 0030 以前の行が無い(運用 DB は 0015 止まりで表自体が未作成)ため推定値が実データ化する環境は現時点で存在しない。
- DEFAULT→DROP バックフィルを PG17 で実測検証: DDL は行トリガを発火せず、DROP DEFAULT 後も attmissingval で既存行の値が残存、追記オンリートリガと NOT NULL は健在。0028 の DISABLE/ENABLE TRIGGER 方式と異なり**可変ウィンドウが生じない**ため、定数バックフィルでは今後の標準として推す(行ごとに値が異なるバックフィルのみ 0028 方式が残る)。
- 0031: 語彙3値を独立に全書き手列挙で確認(start_run='running' / finish 呼び出し側は success・failed のみ / ledger.create_run 既定 success・明示指定なし / seed 0006・0011)。第4の値はコードにも実データにも無い。NOT VALID 不使用は表規模から妥当(「入れたのに効いていない」の回避)。
- **重大(マージ前必須)**: migration 番号 0030–0031 が別の未マージ worktree の連番(0030_ledger_mtm_account 〜 0033)と衝突し、共有 ryza_test では本ブランチの 0030 が**無言でスキップ済み**(runner は4桁 version のみで適用判定・ledger PK も version)。実測: 本ブランチの origin テストは ryza_test で UndefinedColumn で失敗し、runner は「no pending migrations」と報告する。後からマージする側の連番振り直し+ryza_test 再作成が必要。runner への「同一 version 重複のエラー化・ledger filename 不一致の検出」をリマインダー登録すること。
- 敵対的検討: origin は申告制であり、書き手の無い 'cli'・'job' が語彙に常設されるため、想定外の書き手が 'job' を名乗れば CHECK も VETO_ORIGINS も素通りする(検出は #運営 embed を読む人間のみ)。起草者は「捕まるのは想定外経路からの記録であって詐称ではない」と正確に開示しており、語彙先行登録の理由(CHECK 改定が保護領域手続で「既存値で誤魔化す」誘因を生む)も筋が通る — 了としたうえで記録する。
- 軽微: 0030/0031 の pg_constraint ガードが conname のみでスキーマ・表非限定(同名衝突時に無言スキップ — 番号衝突と同族の失敗モード)。0031 コメントの「テスト DB は success のみ」は現状 failed も存在(いずれも語彙内で無害)。
- 判定: **条件付き承認**(条件は番号衝突の解消のみ。0030・0031 の設計・実装自体への差し戻し事項なし)。
