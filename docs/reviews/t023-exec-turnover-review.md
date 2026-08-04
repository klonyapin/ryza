---
review: t023-exec-turnover-monitor
reviewed_sha: e27cf196c01907e41eeef2d29dabef6f743a529f
reviewer: independent-officer
review_date: 2026-08-04
verdict: approve
---

# T-023 exec turnover monitor: 独立役員審査

## 対象範囲
- worktree: `/Users/mmiyazaki/Projects/sukifura/ryza/.claude/worktrees/agent-a71a6b09`
- ブランチ: t023-exec-turnover-monitor
- HEAD: e27cf196c01907e41eeef2d29dabef6f743a529f
- 仕様書: `docs/tasks/T-023-f12-f14-exec-turnover-and-tests.md`

## 骨子(各観点は下記に順次追記)
1. 仕様との突合
2. 検知条件の適用範囲(取りこぼし)
3. 誤検出の分析
4. 原子性と通知の整合
5. 会計・記帳経路の不変原則
6. G-7 本体の無変更確認
7. F-14a/F-14b
8. IPS ハードコード検査
9. テスト実行
10. 発見事項一覧・verdict

## 1. 仕様との突合

仕様書 T-023 §F-12 の実装要件を1件ずつ確認する。

| 仕様要件 | 実装箇所 | 適合 |
|---|---|---|
| 検知ヘルパー `turnover_breach_after_execution(conn, execution_id)` を `src/ryza/gate/orders.py` に追加 | orders.py L547-624 | 適合 |
| 入力は conn+execution_id | L547-552 のシグネチャ | 適合 |
| 当該約定の JST 日付・book_id で約定ベースのみの当日累計を、before(除く)/after(含む) 双方で計算 | `_executed_turnover_before_and_after` L471-499(`FILTER (WHERE e.id <> %s)` で before、無条件 sum で after) | 適合 |
| `_daily_turnover` の約定側クエリと同じ式 | 双方 `sum(abs(e.qty) * e.price)` + JOIN orders + `(executed_at AT TIME ZONE 'Asia/Tokyo')::date = trade_date`。**式は完全一致** | 適合 |
| 上限 = `ips.hard_limits.daily_turnover_nav_max × NAV` | L610 `Decimal(str(ips.hard_limits.daily_turnover_nav_max)) * nav` | 適合。float→Decimal を str 経由で安全化 |
| NAV はゲート判定時のスナップショット(`gate_log.state_ref`)から取る | `_nav_from_gate_log` L502-544(実行→注文→gate_log と辿り `state_ref->>'nav'` を Decimal 化) | 適合 |
| dd_soft 半減は適用しない、docstring 根拠明記 | L515-516 に明記(半減=新規建て抑制の意味論、事後監視は暴走ガード本体 30%) | 適合 |
| 返り値: 跨いだ場合のみ詳細、それ以外 None | L596-608(fail-closed)、L611-623(跨ぎ)、L624(それ以外 None) | 適合 |
| **跨ぎ判定は `before ≤ limit < after`** | L611 `if before <= limit < after` | 適合 — 継続鳴動抑止 |
| 同一トランザクション内で press outbox に urgent enqueue | runner.py L205-214 の `_execute_one` 内(呼び元 `run_pending` L260 が `with conn.transaction()` で囲む) | 適合 |
| embed に before/after/上限/銘柄/注文 ID を含める | runner.py `_turnover_breach_embed` L50-99 に `before`, `after`, `limit_text`, `instrument_id`, `order_id`, `execution_id`(タイトル)を含む | 適合 |
| G-7 本体は変更しない、docstring 1 行追記可 | compliance.py 差分は `_g7_turnover` docstring のみ(6 行追加、ロジック行 L505-518 は無変更) | 適合 |

**仕様からの逸脱の妥当性評価**:

- 「呼び出し箇所が runner.py」(仕様は demo.py と記載): 起草者の PR 本文で受容済み。実際には `execute.demo` は Broker 実装であり、`record_execution`/`apply_execution` を呼ぶのは runner.py の `_execute_one`(L175-185・L200)。仕様書の「デモ執行経路」を素直に読むと runner が正しい配線先(1 注文=1 トランザクション savepoint の唯一の呼び出し元)。**妥当**
- 「NAV 出所を gate_log.state_ref」(仕様は order_ref とも読める記載): 仕様は「apply_execution が asset_class 抽出に使っているのと同じ経路」と指定しているが、apply_execution は `order_ref ->> 'asset_class'` を使う(L389-405)。実装は state_ref から NAV を取る。仕様書 L23 が「gate_log の order_ref JSON」と書きつつ「NAV はゲート判定時のスナップショット」と要求している点は、`_state_snapshot` が state_ref に NAV を保存する構造(orders.py L189-208)を前提にすれば、**state_ref が正しい格納先**。実装の判断は仕様の意図(判定時 NAV の再現性)に合致し、order_ref に NAV は元々書かれない(_proposal_snapshot は OrderProposal のフィールドのみ)。**妥当・むしろ実装のほうが正しい**

## 2. 検知条件の適用範囲(取りこぼしの分析)

**取りこぼし得るケースの列挙と評価**:

- **book_id が異なる約定の混入**: 検知クエリは `WHERE o.book_id = %s`(L491)で book_id ごとに閉じる。当該約定の book_id を先に読み(L571-579)、その book_id 内でのみ集計。**取りこぼしなし**
- **venue の扱い**: クエリは venue で絞らない。デモ経路と実行経路(将来)が同一 book_id なら同じ枠として合算される。**妥当**(G-7 の枠は book 単位で1つ)
- **JST 日付境界**: `(e.executed_at AT TIME ZONE 'Asia/Tokyo')::date` で切る。`_daily_turnover` と同じ規約。UTC のみで見ると跨る対でも JST で正しく分離される。test_trade_date_scoped_by_jst で回帰固定。**妥当**
- **gate_log を持たない注文経路**: スキーマ上不可能(orders.gate_log_id NOT NULL、migrations/0014 L66)。`gate_and_record` 以外に orders を作る API は無い(A-3 突合の対象)。**構造的に発生しない**
- **エッジトリガの取りこぼし境界**: `before ≤ limit < after`
  - `before == limit` かつ `after > limit`: 発火する(≤ を含むため)。**妥当**
  - `before < limit` かつ `after == limit`: **発火しない**(限界内ちょうどは超過ではない)。**妥当**
  - `before == limit` かつ `after == limit`: 数値上あり得ないケース(qty>0/price≥0 で after ≥ before + 0)。**問題なし**
  - **重要な観察**: 最初の跨ぎ後、超過状態が持続する日中に **NAV が下方修正**されて limit が下がり(ただし本実装では NAV は判定時固定なので該当しない)、その後の約定で新たに before/after が同じ状態のまま鳴らない設計。判定 NAV が約定ごとに違えば同一日でも limit が変わり得るが、本実装は約定に紐づく gate_log の判定時 NAV を使うため、**時系列で異なる注文の判定 NAV が混在**する。同一日内で NAV が動くケース(場中の再評価)では、最初の跨ぎ検知に使う NAV は当該約定の gate_log の NAV(最新版)である一方、before の累計は過去約定の実約定価格。**limit と累計の基準が不一致**。ただし仕様の意図は「その約定の判定時に使った NAV で見て、その約定が枠を破ったか」であり、当該約定の判定 NAV は当該約定の gate_log から取るのが自然。**設計妥当だが微妙な意味論なので docstring への追記が望ましい**(発見: 軽微)
- **同時・並行実行時の累計の読み方**: `_execute_one` は `run_pending` から `with conn.transaction()` で囲まれた 1 注文 1 savepoint 内で呼ばれる。`record_execution` は `SELECT ... FOR UPDATE` で order 行を掴んでから executions を INSERT する(L335)ため、同一 order への並行 fill は直列化される。異なる order の並行 fill は?
  - 検知クエリは `AGG WHERE book_id AND date` で走る。トランザクション分離レベルが READ COMMITTED(psycopg 既定)なら、別トランザクションが未コミットの executions は見えない。実際には savepoint 内 UPDATE 済みなので self では見える。**別注文の並行執行**: `_execute_one` は各注文で `with conn.transaction()` を張り直すので同一 connection 上は逐次。**別 connection**(将来の並列 worker)からの並行は本 PR の想定外(gate_and_record は advisory lock で直列化するが、runner 側にはロックが無い)。実装のカレント配線では順次実行なので**妥当**、将来並列化する場合の課題として認識しておく(発見: 中)
- **`_daily_turnover` の約定側との突合**: `_daily_turnover` の pending 側(orders passed/submitted)は本実装の検知には入らない(仕様通り「約定ベースのみ」)。事後監視は約定成立が前提なので **妥当**
- **fee の扱い**: `_daily_turnover` の約定側もフィーを合算しない(`abs(qty) * price` のみ)。本実装も同一式なので突合齟齬なし。**妥当**

**結論**: 取りこぼしは実装配線内では発見されず、仕様意図通り。将来の並列 worker 導入時は runner 側の advisory lock か SERIALIZABLE 分離が必要になる旨を認識しておくべき(現行は既知の制約下で妥当)。

## 3. 誤検出の分析

- **枠内約定・複数注文の順次執行**: test_within_limit_returns_none / test_edge_trigger_fires_once_on_crossing の前半で回帰固定
- **既に超過中の追加約定**: エッジトリガで抑止(test_no_alarm_while_already_over_limit)
- **前日約定は当日累計に入らない**: test_trade_date_scoped_by_jst
- **`_execute_one` の再試行経路**: 失敗すると savepoint で巻き戻る → executions 行も outbox 行も残らない。次回 `run_pending` で同じ passed 注文が再処理され、成功時のみ enqueue される。**二重通知は発生しない**
- **fail-closed が正常系で誤発火**: `_nav_from_gate_log` が None を返すのは gate_log 欠落・state_ref NULL・nav キー欠落・非正 NAV のみ。正常な `gate_and_record` 経路では `_state_snapshot` が nav を str で必ず入れる(L189-190、nav=None 時は "None" を回避すべく `_s()` を通すが、`state.nav is None` のケースだと文字列 "None" が入るリスク)。
  - **要確認**: `_state_snapshot._s(None)` は None を返す。dict 上は `"nav": None`。`_nav_from_gate_log` は `nav_raw is None` を検知して fail-closed の理由を返す(L536-537)。ゲート判定時に nav=None のケース(fail-closed で block)では state_ref にも None が入るが、そもそも block なら executions は生成されないので、この経路には到達しない。**設計整合済み**

## 4. 原子性と通知の整合

- `run_pending` L260 が `with conn.transaction()` を張り、`_execute_one` 内の record_execution / post_fill / advance_order_status / turnover_breach 検知 / enqueue が同一 savepoint 内で実行される。仕訳失敗や advance_order_status 失敗で例外が発生すると savepoint 全体が巻き戻り、outbox 行も消える。**通知だけ残る経路は無い**
- 逆に「約定は残ったが通知だけ失われる」経路: 
  - 検知ヘルパが例外を投げる → 例外は `_execute_one` 内で catch されない → run_pending L265 の except で拾い、savepoint 全体巻き戻し。約定も outbox も両方消える(次回再試行)。**通知だけ失う経路は無い**
  - enqueue が例外 → 同上で巻き戻し
- **配送側の冪等**: outbox は sent_at IS NULL で条件付き mark(既存)。跨ぎ通知が二重送信になる経路も無し

**唯一の懸念**: 巻き戻し後の再試行で、前回の約定が保存されていないため次回は「新規約定」として扱われ、正しく通知される。実装配線は健全。

## 5. 会計・記帳経路の不変原則

- **trading.executions への記帳**: runner.py `_execute_one` は `record_execution` 経由(L175)。本 PR の変更行に直接 INSERT は無い(git grep 済み)
- **ledger への記帳**: `posting.post_fill` 経由(runner.py L186)。本 PR の変更行に直接 INSERT/UPDATE は無い
- **テストコードの直接 INSERT**: test_turnover_breach.py `_insert_execution`(L64-85)が `trading.executions` に直接 INSERT する。理由は同ファイル冒頭 docstring(L1-12)で明示: 「`record_execution` は累積約定 ≤ 注文数量 制約を持つため、NAV×30% を跨ぐ大きな累計を単一注文で作れない」「検知ヘルパは executions の合算式のみを見るので直接 INSERT でも意味論は同一」。docstring の理由付けは**妥当**
- **テストの分離**: gate/conftest.py の `conn` フィクスチャ(L38-46)が rollback で隔離。共有 DB に書き残さない。**妥当**
- **test_fail_closed_when_gate_log_state_ref_lacks_nav の DISABLE TRIGGER**: 追記オンリー制約を一時的に外して state_ref を書き換えるが、DDL はトランザクション内で巻き戻る(rollback される)。**テスト専用の破壊がリポジトリに漏れない**

## 6. G-7 本体の無変更確認

git diff で機械的に確認済み: `src/ryza/gate/compliance.py` の変更は `_g7_turnover` の docstring 追加 6 行のみ(L498 の関数シグネチャ以降のロジック行に変更なし)。**適合**

## 7. F-14a/F-14b

**F-14a**(tests/risk/test_daily.py の 3 テスト追加):

1. `test_f14a_future_entry_date_goes_to_pending`: 最終スナップより未来の entry_date が points に混入せず pending_flows に出ることを固定。points の `net_flow` を before(投入前)と直接比較しており、二重計上・黙消の両方を塞ぐ。**適合**
2. `test_f14a_entry_before_series_start_attaches_to_first_point`: 系列開始より過去の entry_date は先頭点に寄る(実測経路の固定)。past_amount と `points[0].net_flow` の増分を assert し、pending が空であることも同時検証。**適合(仕様通り「実測結果を期待値として固定」)**
3. `test_f14a_sum_preservation_across_anomalies`: 過去・当日・未来の 3 種フローを投入し、`points + pending` の増分総和 == 投入総和を検証。**適合**

**F-14b**(tests/gate/test_lock.py + pyproject.toml):

- ファイル冒頭 docstring に分離の理由を追加(commit する理由・trading_state singleton の干渉・原状復帰の仕組み)。**仕様1適合**
- `pytestmark = pytest.mark.commits_shared_state` をファイル全体に付与。**仕様2適合**
- pyproject.toml `[tool.pytest.ini_options]` の `markers` に `commits_shared_state` を登録。名前・説明が spec 通り。**整合**
- `committed_prereqs` フィクスチャ docstring に「commit を伴う理由・干渉し得る対象・原状復帰の仕組み」を追記。既存の try/finally は変更なし(既に finally 済み)。**仕様3適合**

## 8. IPS 値のハードコード検査

grep 結果:

- `src/ryza/gate/orders.py`: `30%` は L516 の docstring のみ(半減不適用の説明文)
- `src/ryza/execution/runner.py`: マッチなし
- `tests/gate/test_turnover_breach.py`: L10 docstring と L91 コメントの `30%` のみ

判定処理(`limit = Decimal(str(ips.hard_limits.daily_turnover_nav_max)) * nav`)と embed 表示(`float(breach.limit / breach.nav):.0%`)はいずれも IPS 値と NAV から動的算出。**IPS 変更時に表示・判定が乖離するリスクはない**。**適合(受け入れ基準クリア)**

## 9. テスト実行結果

```
tests/gate/test_turnover_breach.py 5 passed
tests/gate/test_lock.py           1 passed
tests/risk/test_daily.py -k f14a  3 passed
ruff check src/ryza/gate/ src/ryza/execution/ tests/gate/  All checks passed!
```

追加のエッジトリガ紙上検証(before ≤ limit < after)を実施:

| before | limit | after | 期待 | 実測 |
|---|---|---|---|---|
| 100 | 100 | 101 | 発火 | True |
| 99 | 100 | 100 | 非発火 | False |
| 99 | 100 | 101 | 発火 | True |
| 101 | 100 | 102 | 非発火(超過継続) | False |
| 0 | 100 | 100 | 非発火(等しいは超過ではない) | False |

すべて意味論通り。「跨ぎ=超過に転じた瞬間」の定義に整合。

## 10. 発見事項一覧 と verdict

### 発見事項

| 重要度 | 分類 | 内容 |
|---|---|---|
| 軽微 | 意味論の明示 | 同一 book 同一 JST 日の異なる注文が異なる NAV スナップショット(場中の再評価)を持つ場合、「当該約定の gate_log の NAV × 30%」を limit とする一方 before は過去約定(別 NAV で判定された)を含む。現行 NAV スナップショットは 1 日固定運用なので実害はないが、日中 NAV 更新が導入された際の意味論が曖昧。docstring での「limit は本約定判定時の NAV、before/after は当日累計」の意味の明示があると将来の保守で助かる。**現行運用では問題なし** |
| 中 | 将来の並列化 | `_execute_one` は同一 connection 内では逐次だが、複数 connection からの並列 `run_pending` に対する直列化は現状無い(`gate_and_record` は advisory lock を張るが runner は張らない)。並列 worker 導入時は runner 側にも book_id ロックを入れる必要がある。**T-023 のスコープ外**(実装当時の設計で妥当) |

### 肯定的確認(問題を探して見つからなかった点)

- **仕様の 11 項目すべて実装済み**(§1 の突合表): 検知ヘルパーの入出力/エッジトリガ/NAV 出所/dd_soft 不適用+docstring 根拠/同一トランザクション内 enqueue/embed の必須フィールド/G-7 本体無変更、いずれも仕様に厳密に一致
- **原子性の壁**: `_execute_one` は savepoint 内で 記帳→検知→enqueue を実行し、通知だけ残る/通知だけ失う経路は存在しない
- **fail-closed 経路**: NAV 判定不能を黙殺せず TurnoverBreach を返し、embed で「判定不能」を表示(数値偽装なし)。実運用では到達し得ないが、A-3 検知経路との整合性として妥当
- **記帳経路の不変原則**: executions は record_execution 経由のみ、ledger は post_fill 経由のみ、本 PR で直接 INSERT/UPDATE の混入なし。テストの直接 INSERT は理由付き docstring 付きで rollback 隔離
- **G-7 本体無変更**: git diff で機械的に確認、docstring 6 行追加のみ
- **IPS ハードコードなし**: 判定・embed ともに `ips.hard_limits.daily_turnover_nav_max × NAV` から動的算出
- **エッジトリガの意味論**: 5 通りの境界パターンすべて仕様の意図に整合
- **F-14a**: 3 テストが「未来 → pending」「過去 → 先頭点」「総和保存」を独立に固定、実測固定は仕様の指定通り
- **F-14b**: マーカー登録・pytestmark 付与・docstring 追記が仕様の 3 項目に対応
- **ruff クリーン**、対象テスト全通過(6/6 + F-14a 3/3)

### verdict

**approve**

根拠: T-023 仕様書の全要件を実装しており、逸脱項(呼び出し配線 runner.py・NAV 出所 state_ref)は仕様の意図に照らして**むしろ実装のほうが正確**(demo.py は Broker 実装であり `record_execution` を呼ばない、_state_snapshot に NAV が格納される構造から state_ref が正しい格納先)。原子性・fail-closed・エッジトリガ・G-7 本体無変更・IPS ハードコードなしの受け入れ基準はすべて満たし、テストは対象範囲で全通過・ruff クリーン。発見事項は「軽微」1 件(将来保守向け docstring 追記の助言)と「中」1 件(スコープ外の将来並列化課題)のみで、いずれも **request_changes のブロッキング要因ではない**。


