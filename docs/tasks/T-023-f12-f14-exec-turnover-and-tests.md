# T-023: 約定ベース売買代金の事後監視(F-12)+リスク/ゲートのテスト異常系補強(F-14) — Issue #124

- 起草: 2026-08-04 設計リード / 前提: F-6(PR #133)統合済み main に対して作業
- 前提知識: CLAUDE.md、docs/reviews/a12/00-adjudication.md §3(pass2-3 / pass5-2 / pass5-4)、src/ryza/gate/{compliance,orders}.py、src/ryza/execution/demo.py、src/ryza/risk/{daily,navflow}.py、tests/gate/test_lock.py
- **保護領域**(コンプラゲート・リスクリミット周辺)。統合は設計リードが独立役員審査+みなし承認手続で行う
- 本仕様書自体を実装ブランチの最初のコミットとして `docs/tasks/T-023-f12-f14-exec-turnover-and-tests.md` に含めること

## F-12: G-7 の TOCTOU(注文時価格と約定価格の乖離)

### 問題(pass2-3)

G-7(compliance.py `_g7_turnover` L497-518)は「当日累計+本注文 ≤ NAV×30%」を**注文時価格**(limit_price / ref_price)で評価する。成行のスリッページで約定額面が膨らむと、ゲート通過時点では枠内でも**約定後には枠超過**が起こり得る。累計側(`orders._daily_turnover` L133-162)は約定を実約定価格で数えるため**次の注文からは自動的に閉まる**が、超過が起きたこと自体は誰も検知・通知しない。

### 是正方針(設計リード裁定)

**事後遮断はしない(できない — 既に約定済み)。約定ベースの累計が上限を跨いだ瞬間を決定論的に検知し、`#運営` へ urgent 通知する**。以後の注文は現行 G-7 が自動的に塞ぐので、応答は検知・通知で足りる。LLM は関与しない。

### 実装

1. **検知ヘルパー** `src/ryza/gate/orders.py` に追加(名称例 `turnover_breach_after_execution`):
   - 入力: conn、execution_id(適用済みの約定)
   - 当該約定の JST 日付・book_id で**約定ベースのみ**の当日累計(`trading.executions` を実約定価格で合算 — `_daily_turnover` の約定側クエリと同じ式)を、**当該約定を含む額(after)と除いた額(before)**の両方で計算する
   - 上限 = `ips.hard_limits.daily_turnover_nav_max × NAV`。**NAV は当該注文のゲート判定時のスナップショット**(`compliance.gate_log` の order_ref JSON — apply_execution L389-405 が asset_class 抽出に使っているのと同じ経路)から取る。理由: ゲートが適用した上限と同一基準で跨ぎを判定でき、再読み込みによる非決定性(日中の NAV 変動)を持ち込まない。dd_soft の半減は適用しない(半減は「新規建て注文の抑制」の意味論であり、事後監視は暴走ガード本体の 30% に対して行う — 判断根拠として docstring に明記)
   - 返り値: 跨いだ場合のみ詳細(book_id・日付・before/after・上限・execution_id)、それ以外 None。**跨ぎ判定は `before ≤ limit < after`** — 超過状態が続く限り毎約定で鳴らさない(通知が毎回赤だと意味を失う — navflow.urgent_pending L257 と同じ設計判断)
2. **呼び出し**: デモ執行経路(src/ryza/execution/demo.py が record_execution/apply_execution を呼ぶ箇所)で、適用成功後に検知ヘルパーを呼び、跨ぎがあれば **同一トランザクション内で** press outbox に urgent で enqueue(risk/daily.py L456 の `enqueue(conn, channel_ops, embed, run.run_id, urgent=True)` の流儀)。embed 本文には before/after/上限/銘柄/注文 ID を含める
3. ゲート側(G-7 本体)は**変更しない**。注文時価格ベースの事前評価は「近似としては正しく、事後監視とセットで完結する」— この構図を `_g7_turnover` の docstring に1行追記してよい(ロジック変更は不可)

## F-14a: load_nav_series の異常タイムスタンプテスト(pass5-2)

`src/ryza/risk/daily.py` L106-123 `load_nav_series` / `src/ryza/risk/navflow.py` の既存挙動(未来仕訳は pending_flows へ・黙って落とさない)を**リグレッション固定**する DB テストを tests/risk/ に追加:

1. **測定日(最終スナップ)より未来の entry_date を持つ外部フロー仕訳** → points に混入せず `pending_flows` に出る
2. **系列開始(最初の snap_date)より過去の entry_date を持つ仕訳** → 現行挙動を実測し、その挙動を期待値として固定(「最初のスナップに BOP 帰属」なら、そう。実測結果が「黙って消える」だった場合は**テストを書かず設計リードに差し戻す** — 是正が別途要るため)
3. どちらも合計フロー額が保存されること(points+pending の総和が投入額と一致)

## F-14b: tests/gate/test_lock.py の分離の明示(pass5-4)

`committed_prereqs`(L26-60)は autocommit 接続で `ops.trading_state` / `risk.limits_state` を commit する(advisory lock の複数接続検証に必要)。並行実行での干渉リスクを**明示**する:

1. フィクスチャ docstring に「commit を伴う理由・干渉し得る対象(trading_state は singleton)・原状復帰の仕組み」を明記
2. pytest マーカー(例 `@pytest.mark.commits_shared_state`)を pyproject.toml の markers に登録してファイル全体に付与 — 将来 xdist 等を導入するときに直列化対象を機械的に選別できるようにする(現状の実行構成は変更しない)
3. 後片付け(L47-60)の原状復帰がテスト失敗時にも走ることを確認(finally 済みならそのまま)

## テスト(tests/gate/ ほか)

- F-12: 跨ぎ検知の単体+DB テスト — (a) before ≤ limit < after で通知1件、(b) 既に超過中の追加約定では鳴らない、(c) 枠内なら None、(d) gate_log スナップショットから NAV を取れないケース(異常)は**fail-closed で urgent**(検知不能を黙殺しない — 理由コード付き)
- F-14a: 上記 1〜3
- 既存スイート全通過(特に tests/gate/ の G-7 既存テストが無変更で通ること)

## 受け入れ基準

全テスト+ruff 通過 / G-7 本体のロジック無変更 / 通知は跨ぎの1回のみ(エッジトリガ)/ LLM 非関与 / ips.yaml 値のハードコードなし / コミットは日本語+`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`、push しない(統合は設計リードが行う)。DB テストは `RYZA_DATABASE_URL=postgresql://ryza:ryza@localhost:15432/ryza`、worktree では `PYTHONPATH=$PWD/src` 必須
