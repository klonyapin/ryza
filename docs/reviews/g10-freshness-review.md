---
reviewed_sha: b4f21b6a89a70e7b7e34d4771257dc2c1ef0725b
review_date: 2026-08-04
verdict: conditional_approve
---

# G-10 限度状態鮮度検査(risk.limits_state as_of の 2 営業日超で fail-closed block)— 独立役員審査

- 審査日: 2026-08-04 / 審査者: 独立役員(非執行・批判専任・プロンプト分離)
- 対象: ブランチ `g10-freshness`(単一コミット `b4f21b6`)。差分は
  `src/ryza/gate/compliance.py`・`src/ryza/gate/orders.py`・`src/ryza/jobs/daily.py`・
  `src/ryza/risk/calendar.py`(新設)およびテスト 4 ファイル(+612/-17 行)
- 主張: Issue #100 — G-10 に鮮度検査追加+JP 祝日カレンダー(2025-2027 固定)+
  jobs/daily の risk 段失敗を urgent に昇格。**保護領域(コンプラゲート)**の変更で
  あり、独立役員審査 2026-08-03(T-015)統合条件「T-017 前に実装」への対応
- 判定: **条件付き承認** — 重要 2 件の是正をマージ前提条件とする。fail-closed の
  骨格・境界の定義・迂回可能性・テストのミューテーション耐性は敵対的検査に耐えた。
  条件は保護領域の変更手続に関わる不履行(reminder 未更新・reminder 未新設)であり、
  実装ロジックの是正ではない
- 検証方法: 実証主義 — 全主張をミューテーション試行・実測(`business_days_between`
  の手計算+コードでの再計算)・呼び出しグラフの静的走査で確かめた。伝聞での受理無し

## 検証済み — 起草者の主張のうち実測で裏が取れたもの

fail-closed の骨格は敵対的に検査しても穴が見つからなかった。境界・ミューテーション
耐性・単一入口の担保はすべて実測に基づく:

1. **判定入口は 1 か所**(`src/ryza/gate/orders.py:261` の `evaluate` 呼び出し)。
   `Grep -r "evaluate\("` で本番コードから `compliance.evaluate` を呼ぶのは
   `gate.orders.gate_and_record` のみ、それを呼ぶのは `src/ryza/fm/base.py:547`
   のみ。**G-10 を通らずに発注に到達する経路は無い**(迂回可能性)
2. **未初期化・欠落は G-F(入力完全性)で block**:
   `src/ryza/gate/compliance.py:715-721` は `state.limits is None` と `state.now is None`
   の両方を独立に列挙する。前者=行不存在(engine 未起動)、後者=時刻測定不能。両方
   とも `evaluate` 冒頭で G-F block を返す(`compliance.py:750-754`)。以降の規則
   評価に進まないため、G-10 の assert に到達しない
3. **as_of NULL・未来・境界の 3 分岐**(`compliance.py:644-676`)を独立 Reason で
   出す。dd_hard=True でも鮮度違反は独立に残る(`test_g10_freshness_and_flags_are_independent`
   がそれを検査)ため、「dd_hard=True で block しているから鮮度検査を消しても pass に
   ならない」という**攻撃者の合理化**を封じる
4. **境界の定義**(`> 2` 営業日): 半開区間 `(start, end]` の営業日数で判定。手計算で
   確かめた:
   - `Fri 2026-07-31 → Tue 2026-08-04`: Sat/Sun 非営業 → Mon Aug 3 (1) → Tue Aug 4 (2) = **2**
   - `Thu 2026-07-30 → Tue 2026-08-04`: Fri Jul 31 (1) → Mon Aug 3 (2) → Tue Aug 4 (3) = **3**
   - `Fri 2026-08-07 → Wed 2026-08-12`(山の日 Tue Aug 11 を跨ぐ): Mon Aug 10 (1) →
     山の日は非営業 → Wed Aug 12 (2) = **2**(祝日を「非営業」として数える)

   `python -c "from ryza.risk.calendar import business_days_between; ..."` で全ケース
   再計算し合致を確認
5. **ミューテーション検知(3 種試行・全て検出)**:
   - `elapsed > 2` → `elapsed >= 2`: 経過 2 の pass ケース(`test_g10_freshness_exactly_two_business_days_passes`)が
     block に反転 → テスト失敗
   - `elapsed > 2` → `elapsed > 3`: 経過 3 の block ケース
     (`test_g10_freshness_three_business_days_blocks`)が pass に反転 → テスト失敗
   - `is_business_day` から祝日テーブル参照を外す(weekday-only 化): 祝日跨ぎ pass
     ケース(`test_g10_freshness_holiday_crossing_passes`)が block に反転 → テスト
     失敗(手計算で確認: 山の日を営業日にすると経過が 2→3)
6. **祝日テーブルの週日整合を全件抽出で確認**: 振替休日候補(元休日の翌平日)は全て
   Mon になっている(2025-02-24, 2025-05-06, 2025-11-24, 2026-05-06, 2027-03-22)。
   2027 春分の日=3/21(Sun)→ 振替 3/22(Mon)は暦要項と一致(WebSearch で内閣府
   官報告示を確認)
7. **監査再現性**: `gate_log.state_ref` に `limits_state.as_of`(ISO8601 文字列)と
   `now` の両方が残る(`src/ryza/gate/orders.py:179-207`)。「何営業日で block したか」
   を事後に再計算できる(`tests/gate/test_store.py:126-128` がその存在を検査)
8. **単一入口の呼び出しは now を必ず入れる**(`src/ryza/gate/orders.py:239, 259`)。
   `datetime.now(UTC)` が唯一の実行時ソースであり、テスト以外の経路で bypass する
   経路は無い

## 所見(重大度付き)

### [重要] R-1 保護領域変更の reminder 更新が未実施

`ops/reminders.yaml:225-237` の `risk-limits-state-freshness-gate` は本 PR そのものの
アクション要求である(「T-017 前に必須」)。差分ではこの reminder の status を
`pending` のまま放置している。**将来アクションの制度化**(CLAUDE.md「LLM セッションは
使い捨て」)は登録だけでなく**完了時のクローズ**まで含めた運用であり、実施したのに
`pending` のまま残せば同じ登録が再燃して二重実装や周辺編集を誘発する。

**是正**: 同 PR で `status: done # 2026-08-04 G-10 に鮮度検査を追加(b4f21b6)` に
更新すること。他 reminder の完了記法(例 L453 の `fm-quarantine-runbook`)に揃える。

**裁定(設計リード 2026-08-04)**: 採用

### [重要] R-2 祝日テーブル延伸の reminder が未登録

`src/ryza/risk/calendar.py:22-24` のコメントは「テーブルの延伸は運用課題として
`ops/reminders.yaml` に登録する」と明言しているが、実際には登録が無い(`Grep -i
"calendar|holiday|2028|jp_holidays"` で件数 0 件を確認)。**執筆規格**の「レベル 1
(ファクト)は出典必須」に照らすと、コード内で reminder 経由の追跡を宣言しながら
実物を作らないのは**執行可能性のない約束**であり、CLAUDE.md「セッション内の約束は
無効」原則の違反に近い(セッションを跨いだ機械可読な追跡が無い)。

いま 2026 なので table 末尾 2027-11-23 まで約 15 か月あり、経過的な危険は低い。
しかし 2027-12-31(Fri・JPX 大納会翌日 = 東証は 12/31 は休場)が既にテーブルから
**漏れている**(2026-12-31 は入っているのに 2027-12-31 が無い — 一貫性欠如)。
out-of-range フォールバックにより 12/31 Fri は営業日として数えられ、経過を過大評価
= 早期 block になる(fail-closed 方向)。ロジック上の危険は無いが、**「一貫した
運用ルール」の欠落**は今後の延伸作業のたびに再発する構造的欠陥である。

**是正**: 同 PR で `ops/reminders.yaml` に次を追加する(2027 年末より十分前で発火):
```yaml
  - id: jp-holiday-table-extend
    what: "JP 祝日カレンダー(src/ryza/risk/calendar.py:_JP_HOLIDAYS)を 2028 年ぶんへ延伸(G-10 鮮度検査の依存)"
    conditions:
      - type: date_after
        date: "2027-06-01"
    action:
      type: issue_create
      title: "祝日カレンダー延伸: 2028 年+東証休場(12/31, 1/2, 1/3)を追加"
      labels: [impl, risk]
      body: "内閣府「国民の祝日について」の 2028 年官報告示(2027-02-01 前後に確定)を反映し、東証の市場慣行(12/31・1/2・1/3 休場)を含めて追記する。同時に 2027-12-31 が現行テーブルから漏れている不一致も是正する(2026-12-31 は入っている)。テーブル外の日は営業日フォールバックで fail-closed 側(経過過大評価 → 早期 block)に倒れるためロジック上の危険は無いが、監査ログの理由文が実態と食い違う(=リスク engine 停止と誤認しうる)ので早期に潰す。"
    status: pending
```

**裁定(設計リード 2026-08-04)**: 採用

### [中] M-1 「urgent 昇格」が既存の urgent 経路と二重立ちする可能性

`src/ryza/jobs/daily.py:936-941` は risk 段が **`stage.ok=False`(例外で落ちた場合)**
のみ urgent 昇格する。他方 `src/ryza/risk/daily.py:456, 525` は risk エンジンが**内部で**
urgent 埋め込みを ops チャンネルに投げる(締め失敗・材料性のあるフロー未反映など)。
`_run_stage` は例外を握って `ok=False, error=...` を返す(`daily.py:271-272`)ので、
`run_risk_daily` が**部分的に失敗**(帳簿の片方だけ例外)しても外側の stage は成功
判定になり、当該経路の urgent は risk_daily 側で立つ。stage.ok=False になるのは
`run_risk_daily` 自体が**全域で例外**を上げた場合のみ(DB down・code bug 等)。

つまり `risk_stage.ok=False → ops_summary を urgent` は「risk エンジン全域停止」の
検知であり、G-10 鮮度検査が想定する「エンジンが数日止まる」の**最初の 1 日目**を
確実に捕らえる。**論理は正しい**が、コメント(`daily.py:931-934`)は「失敗した run
は必ず urgent」と一般的に書きすぎており、実態(全域例外のみ)とやや齟齬がある。
また `risk_stage is None` の防御(`daily.py:935`)で「stage リストに risk が無い」
という起き得ないケースを黙って `risk_failed=False` にしているのは、fail-closed
原則からはやや逸脱(呼び出し順序を壊す改変が urgent を消せる)。

**是正提案**(強制ではなく検討推奨):
- コメントを「risk 段が**全域例外で**落ちた場合、実行サマリを urgent で昇格」に
  修正して、部分失敗が risk_daily 側の urgent 埋め込みで拾われる**二段構え**である
  ことを明記する
- `risk_stage is None` は`assert risk_stage is not None`(呼び出し順序が壊れたら
  fail-closed で例外を出す)に変える。ops_summary 段自体は `_run_stage` で握られる
  ので、日次サイクルは止まらない

**裁定(設計リード 2026-08-04)**: 採用

### [中] M-2 as_of の「日付単位切り捨て」は仕様の妥当性を明示すべき

`_g10_risk_state` は `to_jst_date(now)` と `to_jst_date(limits.as_of)` の**日付差**
で判定する。00:00:01 JST 更新のリミット行を 23:59:59 JST に判定すると「経過 0
営業日」(同日)= 新鮮扱いになる。時刻ベースなら約 24 時間分の遅延が「新鮮」に
入る = 実質 3 営業日ぶんの遅延を許容しうる**最悪ケース**が存在する(as_of が 月曜
00:00 JST、判定が 水曜 23:59 JST → JST 日付で 2 営業日差 → pass)。

これは JP 業日運用(engine は夜間に走り、朝の判定は前夜のスナップショットを
「同日」扱いする)を前提とすると自然だが、**設計意図が明示されていない**。仕様
書きの `docs/design/00-system-design.md` にも `ops/reminders.yaml:225-236` にも
「2 営業日=48 時間」ではなく「2 営業日=JST 日付差 ≤ 2」だとは書かれていない。

**是正提案**: `_g10_risk_state` のドキュストリング(または `compliance.py:139-141` の
定数コメント)に「営業日は JST 日付で判定(engine が夜間更新する運用と揃える)」を
1 行明記する。ロジックは変更しなくてよい。

**裁定(設計リード 2026-08-04)**: 採用

### [軽微] L-1 「境界のちょうど」テストが実装のコピーになっていないか

境界テスト(2 → pass / 3 → block)は自分で `business_days_between` を手計算し、
コード実行結果と一致した。実装は同関数を使うが、テスト側は**独立に日付を明示**
(`Fri Jul 31`・`Thu Jul 30`)して verdict を検査しており、実装のコピーではない
(実装の閾値 2 に対しテストは日付を経由)。**問題なし**として記録。

**裁定(設計リード 2026-08-04)**: 是正不要を確認

### [軽微] L-2 祝日テーブルの 2 年ぶん抜き検査

2025-2027 の祝日から代表点を抜き取り WebSearch で暦要項と照合:
- 2027 春分の日=3/21(Sun)→ 振替 3/22(Mon) ✓
- 2026 春分の日=3/20(Fri) ✓
- 天皇誕生日=2/23 ✓、2025 は Sun → 振替 2/24 ✓
- 2026 国民の休日=9/22(敬老の日 9/21 と秋分の日 9/23 に挟まれた Tue) ✓
- 2027 は連続する平日祝日が無いので国民の休日はテーブル外 ✓
- 東証慣行の市場休場(1/2, 1/3, 12/31)は 2026 に反映(2027 は R-2 で指摘した漏れ)
**問題なし**として記録(R-2 の一貫性不足は別途)。

**裁定(設計リード 2026-08-04)**: 是正不要を確認(2027-12-31 の漏れは R-2 側で是正)

### [軽微] L-3 テストの一部で `LimitsState(as_of=...)` を直接構築している

`tests/gate/test_rules.py:621-624` は `_limits_at(as_of, **flags)` を新設しているが、
`conftest.py:92-98` の `fresh_limits` と機能が近い。前者は as_of を可変・後者は
`_NOW` 固定。共存は妥当だが、`_limits_at` が private 名(先頭アンダースコア)で
`fresh_limits` は module 内公開関数、という命名の一貫性が弱い。**是正不要**(実害
なし)。

**裁定(設計リード 2026-08-04)**: 是正不要を確認

### [軽微] L-4 `_state_snapshot` の型注釈で `type: ignore`

`src/ryza/gate/orders.py:186` に `# type: ignore[union-attr]` が付いている。直前
に `if limits_dump.get("as_of") is not None:` で守っているので `state.limits.as_of`
は None でないが、mypy が絞り込めない。ロジックは正しい。**是正不要**(実害なし)。

**裁定(設計リード 2026-08-04)**: 是正不要を確認

## 検査したが問題を見つけられなかった観点

- **fail-closed の徹底**: as_of NULL・行なし・now 不明・未来 as_of・タイムゾーン
  変換の 5 経路すべて block 側に倒れる。特に「now を渡さないだけで鮮度検査が素通し」
  にならないか(=攻撃者が単純に呼び出しを改変する余地)を検査したが、`_missing_inputs`
  が `state.now is None` を独立列挙して G-F block にする(`compliance.py:720-721`)。
  この行を消すミューテーションは `test_g10_freshness_now_missing_blocks_via_gf` で
  失敗する
- **カレンダーの正しさ**: 2025-2027 の祝日テーブルを内閣府暦要項と照合、weekday
  整合(振替はすべて Mon)を全数確認、境界の年跨ぎ(2026-12-30 → 2027-01-05 で 1
  営業日)を手計算とテストで確認
- **境界の起算**: `to_jst_date` で as_of と now を同一 TZ の日付に落としてから
  半開区間 `(start, end]` の営業日数を数える。「as_of と now が同日」で 0 になる
  ため、閾値 2 は「JST 日付差 3 の翌日から block」を意味する。日付境界の混乱は無い
- **迂回可能性**: `evaluate` を呼ぶ本番経路は 1 か所(`gate_and_record`)。
  `_g10_risk_state` は `evaluate` 内のループ内で必ず呼ばれ、`checked_rules` に "G-10"
  が積まれる — 監査ログで G-10 抜けの検知が可能
- **ミューテーション観点**: 閾値 > の書き換え・祝日テーブル無視・as_of NULL 分岐
  の削除・now 欠落チェックの削除 — いずれもテストで検出可能
- **urgent 昇格の握り潰し**: `enqueue` は失敗時に例外を上げる(DB エラー)ため、
  緊急通知が黙って消える経路は無い。`_run_stage` は例外を握るが、ops_summary 段は
  失敗しても日次サイクルの他段と独立に扱われる

## 敵対的検査 — 反対すべき点を追加で探した結果

「懸念ゼロ」の再発防止(CLAUDE.md 議論規約 §5)のため、追加観点を敵対的に探した:

- **執筆規格の遵守**: `compliance.py:29-33` のドキュストリング追記は簡潔で、G-10 の
  1 文アーギュメント先行になっている。calendar.py 冒頭も**なぜ既存機構を流用しない
  か**を明示している(`ledger.closing.age_business_days` との差分、`jpholiday` を
  避けた理由)。執筆規格 U 字の抽象度遷移として自然。**問題なし**
- **保護領域の変更手続**: PR は独立審査(本文書)を経る手順に乗っている。ただし
  `Approved:` トレーラは PR マージ時に必要(定款第4条)。本審査は起草者情報から
  遮断されて実施した。**手続は正しく踏まれている**
- **A-18 との整合**: 差分は `src/ryza/gate/**`・`src/ryza/risk/**` の保護領域に
  該当する。両者は `config/governance.yaml:154` および `config/governance.yaml`
  の risk 系エントリで既に protected 登録済み。新規モジュール `src/ryza/risk/calendar.py`
  は既存 area の下位パス(`src/ryza/risk/**` 一致)であり、新規 area 登録不要。
  **問題なし**
- **性能**: `business_days_between` は 1 日ずつループする(最悪でも 5-6 日 = O(定数))。
  ゲート判定 1 回あたりの計算量は無視できる。**問題なし**
- **観測性**: block reason に経過営業日数と as_of・判定日が両方入る(`compliance.py:671-673`)。
  「何日経過で block したか」が Discord 通知の理由に露出する。**問題なし**

## マージ前提条件(条件付き承認の「条件」)

以下 2 件を同 PR(または追加コミット)で満たすまで merge しない。ロジックの是正
ではなく制度的な追跡の不履行への対応:

1. **R-1**: `ops/reminders.yaml` の `risk-limits-state-freshness-gate`(L225-237)を
   `status: done # 2026-08-04 G-10 に鮮度検査を追加(b4f21b6)` に更新
2. **R-2**: 同 `reminders.yaml` に祝日テーブル延伸の reminder(id: `jp-holiday-table-extend`)
   を新設。同時に **2027-12-31 の欠落**(2026-12-31 は table にある)を追記して
   2026/2027 の年末休場の扱いを揃える(1 行追加で足りる — 判断は R-2 の是正で
   まとめて行うのが妥当)

## サマリ

- verdict: **conditional_approve**
- 所見数: [重大] 0 / [重要] 2 / [中] 2 / [軽微] 4
- ロジック(fail-closed・境界・迂回可能性・ミューテーション耐性)は敵対的検査に
  耐えた。条件は保護領域変更の制度的追跡(reminder 更新+新設)であり、実装の
  是正は不要
- 検査に使ったコマンド: `git diff origin/main...HEAD`・pytest(tests/gate tests/risk
  tests/jobs/test_daily.py 276 件全 pass 確認)・手計算による境界検証・ミューテーション
  3 種の模擬・grep による evaluate 呼び出しグラフ確認・WebSearch による内閣府暦要項
  照合
