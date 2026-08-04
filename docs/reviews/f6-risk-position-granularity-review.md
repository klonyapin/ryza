---
review: f6-risk-position-granularity
reviewed_sha: 6c0ecc5afd625477993502c7f89f7f3619d83668
reviewer: independent-officer
review_date: 2026-08-04
verdict: approve
---

# リスク計測のポジション粒度是正(A-12 F-6・T-021・PR #133)— 独立役員審査

- 審査日: 2026-08-04 / 審査者: 独立役員(非執行・批判専任。起草者・設計リードの選好は不知)
- 対象: branch `f6-risk-position-granularity`(head 11db8e9)— `src/ryza/risk/daily.py`(`load_positions` 行単位化)・`src/ryza/risk/engine.py`(`RiskPosition.fm`・`guardrail_usage` 集計意味論・`es95` ネットゼロ除外)・`tests/risk/test_position_granularity.py`・指示書 `docs/tasks/T-021-risk-position-granularity.md`。**保護領域 `src/ryza/risk/`(リスクリミット — 定款第5条)**
- 判定: **条件付き承認**(条件 1 件の是正をマージ前提とする)

## 判定と理由

F-6 の是正方向は正しく、指示書の集計意味論の裁定(グロス=行単位 Σ|value|・発行体集中=銘柄ネット後 abs・ES=銘柄符号付きネット)は金融的に整合し、ゲート側(`gate/compliance.py` の G-4/G-8 `_class_gross_post`/`_pod_gross_post` 行単位・G-3 `abs(post_fund_qty)×price`)とも一致することを独立に確認した。実装は指示書に忠実で、既存判定の不変はゴールデン(`engine_snapshot.json` — 本ブランチで無改変)が証明している。ただし **es95 のネットゼロ除外が 3 行以上の同一銘柄で機能しない反例**を発見したため、その是正を条件とする。

**条件1(中・要是正 — es95 の銘柄ネットは Decimal 段で行うこと)**: `es95()` は行ごとに `float(pos.value / nav)` へ変換してから加算するため、同一銘柄 3 行以上(3 ポッド以上の両建て — `config/mandates/` には ben・jim・peter・stan の 4 ポッドが存在し到達可能)が経済的にネットゼロでも float 加算が厳密ゼロにならない。再現(乱択 20 万試行で 46,928 件=23.5% が非ゼロ残差): nav=4,608,515・+4,774,829・+529,379・−5,304,208 → 残差 2.22e-16。残差が残ると当該銘柄が `weights` に生き残り、(a) 共通観測日を縛って**偽の `no_common_days` 判定保留**(urgent レポート)、(b) 短系列なら `excluded`/`majority_excluded` の偽計上を招く — 指示書自身が掲げた「両建てが included や共通観測日の計算に混入して余計に判定保留を招くのを防ぐ」という目的が 3 行ケースで達成されない。付属テストは 2 行(`float(x)+float(−x)=0` は常に厳密)しか固定しておらず、この欠陥を検出できない。是正: `guardrail_usage` の issuer と同じ 2 段集計 — **銘柄ごとに Decimal で符号付き合算 → 非ゼロのみ `float(net/nav)` 化**。差は ≤1ulp でスナップショット(12 桁丸め)に影響しない。3 行ネットゼロのテストを追加すること。欠陥の起点は指示書の「weights は符号付き加算のまま」+後段 float フィルタという設計であり、実装エージェントの逸脱ではない。

## 確認した事項

1. **指示書の裁定の金融的妥当性**(独立検討): グロスを行単位で測るのは正しい — ポッド間の逆ポジは執行上の相殺ではなく、両レグとも解消コスト・ショートレグの調達実態を持つ。発行体集中は同一銘柄のロング/ショートが価格・発行体イベントの両面で厳密に相殺するためネット後 abs が正しい。ES は同一銘柄リターンが同一系列のため符号付きネットが厳密。三者は互いに矛盾せず、ゲートの G-3/G-4/G-8 とレポートが同じ数字の思想で動く
2. **判定不変の証拠**: `tests/risk/engine_snapshot.json` は本ブランチで無改変(git log 確認)、`test_engine_invariance.py` 通過。スナップショットのケース集は銘柄 ID 重複を含まないため、今回の変更(重複時のみ挙動が変わる)で不変になるのは設計どおり
3. **テスト実行**: `tests/risk/` 全 174 件通過(PG17 test DB)・`ruff check src/ryza/risk/ tests/risk/` 通過。新テスト 8 件は受け入れ基準の数値(両建て 2|q×price|/nav・部分相殺 |60×price| と 140×price・時価欠落 1 件・行単位返却・qty=0 除外)を固定値で当てている
4. **副作用**: `daily.load_positions` の呼び出し元は `run_risk_daily` のみ。`risk/state.py`・embed・`limits_state` の metrics はポジション行を直接シリアライズせず、スキーマ・レポート形状に変化なし。`gate/orders.py` と `fm/base.py` の同名 `load_positions` は別物(PositionState)で無関係
5. **fm 既定値 ""**: engine 側の既定は既存テスト・スナップショット構築の互換用で、daily 側は必ず実 fm を渡している。fm は集計に不使用(docstring どおり)
6. **時価欠落×ネットゼロの合流**: 旧実装は HAVING で行ごと消え検出不能だった経路が、Exclusion(valuation/missing_price)1 件として銘柄単位で正しく浮上する(F-6 問題 2 の是正を確認)
7. **手続**: 指示書が最初のコミット・コミットは日本語+指定トレーラ・ips.yaml 値のハードコードなし・LLM 非関与

## 所見(条件以外)

- **低**: `run_risk_daily` が `load_instrument_returns` に渡す `[p.instrument_id for p in positions]` は行単位化により重複を含む。`ANY(%s)` の意味論上は無害(結果同一)だが、重複排除しておくのが読み手に親切
- **低**: 同一銘柄を複数ポッドが異なる `asset_class` で持つことをスキーマは禁じていない(PK は book×fm×instrument)。`by_class` は行の申告クラスに計上・issuer/ES は銘柄でネットという現挙動はデータを信じる前提では正しいが、分類不整合の検出は T-015 系の分類 completeness 側の課題として既知
- **情報**: `issuer_concentration` は instrument 単位であり発行体エンティティ単位ではない(同一発行体の別商品は合算されない)。ゲート G-3 と同スコープの既存仕様で、本変更の対象外
- **情報**: ネットゼロ銘柄の時価が欠落した場合、その両建てエクスポージャーはグロスにも入らない(評価除外)。Exclusion で開示され、発注時はゲートが fail-closed で block するため、既存の fail-safe 方針と整合

## サマリ(200字以内)

F-6 是正の方向・測度別集計意味論・ゲートとの整合・スナップショット不変を確認し条件付き承認。唯一の条件: es95 の銘柄ネットを float 加算でなく Decimal 段で行うこと。3 ポッド以上の両建てで float 残差(再現率 23.5%)がネットゼロ除外を破り、偽の判定保留を招く。是正は 2 段集計への小変更でスナップショット影響なし。

## 第2ラウンド(条件解消検証)

- 審査日: 2026-08-04 / 対象: 是正コミット 6c0ecc5afd625477993502c7f89f7f3619d83668(branch head — worktree で `git rev-parse HEAD` 一致を確認)/ 前回 reviewed_sha 11db8e9 との差分全体を精読
- 判定: **承認(approve)** — 条件1 は解消。新規問題なし

### 検証結果(全て実測)

1. **差分の範囲**: `git diff 11db8e9..6c0ecc5` は 3 ファイルのみ(`src/ryza/risk/engine.py` +19/−7 相当・`docs/tasks/T-021-risk-position-granularity.md` §4・`tests/risk/test_position_granularity.py` +26)。条件対応以外の紛れ込みなし
2. **実装の正しさ**: `es95` は instrument_id ごとに Decimal で符号付き合算(`signed: dict[int, Decimal]`)→ 厳密ゼロの銘柄を落とす → `float(v / nav)` で weights 化。条件1 の指定どおりの 2 段集計。行単位 `pos.value != 0` ガードは残るが Decimal 加算への寄与ゼロのため無害(最適化として整合)。ゼロ落とし後の included/excluded/共通観測日ロジックは無変更で、weights のキー集合が正しくなる以外の影響なし。`nav <= 0` は合算前に早期 return(既存)。Decimal 合算は金額スケールで文脈精度 28 桁内に収まり厳密
3. **新テストが旧実装で fail することの確認**: `git checkout 11db8e9 -- src/ryza/risk/engine.py` で旧 es95 に戻し `test_es95_three_row_net_zero_is_dropped_without_float_residual` を実行 → **fail**(`n_obs=0`・`deferral_reason='no_common_days'` — 前回指摘の偽保留がそのまま再現)。`git checkout 6c0ecc5 -- src/ryza/risk/engine.py` で復元し `git status` クリーンを確認
4. **乱択再検証**: 前回と同型の 3〜4 行ネットゼロ乱択 **20,000 試行**(nav ¥1M〜100M・値 ±10M・4 fm 混在・観測日が交わらない別銘柄を併置)を新実装に対して実行 → **残差ゼロ 20,000/20,000・偽保留 0 件・偽 excluded 0 件**。同一試行集合で旧方式(行ごと float 化→加算)なら 8,642 件(43.2%)が非ゼロ残差(試行集合が欠陥を踏む能力を持つことの確認)
5. **テストスイート**: `tests/risk/` **175 件全通過**(前回 174 → 新テスト +1)。`ruff check src/ryza/risk/ tests/risk/` 通過
6. **スナップショット不変**: `tests/risk/engine_snapshot.json` は差分範囲で無変更(git diff 空・最終変更コミットは本ブランチ以前の 63e12cb)。`test_engine_invariance.py` は 175 件に含まれ通過 — `float(Σ Decimal / nav)` と旧 `Σ float(v/nav)` の差 ≤1ulp が 12 桁丸めに現れないという前回の見立てどおり
7. **指示書の是正**: T-021 §4 が Decimal 段ネットを規定し「float 加算で合算してはならない」と根拠(3行以上・残差 ~1e-16・偽保留)込みで明記。設計欠陥の起点が指示書側にあった点も文書に反映された

### 所見(判定に影響しない)

- **情報**: 新テストの 3 行目が `fm="pam"` だが、実在ポッドは ben・jim・peter・stan(`config/mandates/`)。fm は es95 の集計に不使用(engine docstring どおり)のため挙動に影響なし。将来リネームの機会があれば実在名に揃えるのが読み手に親切

### サマリ(第2ラウンド)

条件1 は解消。Decimal 段ネットは指定どおり実装され、旧実装で fail する決定的テストが追加され、乱択 2 万試行で残差・偽保留ともゼロを実測。スナップショット不変・175 件通過・ruff 通過・差分に紛れ込みなし。残課題なし(fm="pam" は情報レベルのみ)。**最終判定: approve**。
