# Ryza 第1回フル実装監査(A-12) 報告書

**監査コード**: A-12
**監査対象**: コンプライアンスゲート(`gate/`)・リスクエンジン(`risk/`)・FM層(`fm/`)
**監査担当**: 独立監査部門(別系統モデル)
**監査日**: 2026-08-04

---

## 所見一覧

### [重大] 所見-1: `compliance.py` における売却注文の現金計算不備(現金下限リミットの迂回)

**根拠**: `src/ryza/gate/compliance.py` `_g6_cash_floor` 関数
```python
def _g6_cash_floor(ctx: _Ctx) -> list[Reason]:
    assert ctx.state.nav is not None and ctx.state.cash is not None
    post_cash = ctx.state.cash - ctx.delta * ctx.price
    if post_cash >= ctx.state.cash:  # 売り等で現金が増える注文は現金下限を悪化させない
        return []
```
本実装では、売却注文(sideが`sell`または`short`)の場合、`ctx.delta` は負の値となり `post_cash >= ctx.state.cash` が成立するため、G-6の現金下限チェック全体がスキップされる。
しかし、「売り」のうち**信用取引による新規空売り(`short`)**の場合、現金の増加は担保預託額の増加であり、即時的に自由に使える現金(`cash`)が増加するわけではない。`short` 注文に対してこの早期リターンを行うと、ファンドの現金が枯渇している状況でも新規空売り注文が G-6 を素通りしてしまう。
また、本実装ではポジションの平均取得単価(`avg_cost`)を一切用いない簡易計算(`ctx.delta * ctx.price`)であるため、クローズ注文による実際の現金増加量も正確ではない。

**推奨是正**: `short`（新規建て）は `sell`（クローズ）と区別し、新規ショートの場合は現金増加とみなして早期リターンしない設計に変更する。また、クローズ注文における正確な現金回収額の計算には平均取得原価等の考慮が必要である。

### [重大] 所見-2: `daily.py` における信用取引のポジション評価とリスク計算の不整合

**根拠**: `src/ryza/risk/daily.py` `load_positions` 関数
```python
        cur.execute(
            """
            SELECT p.instrument_id, p.asset_class, sum(p.qty)
            FROM trading.positions p
            WHERE p.book_id = %s
            GROUP BY p.instrument_id, p.asset_class
            HAVING sum(p.qty) <> 0
            """,
```
本実装では銘柄と資産クラスごとに `qty` を単純合算している。将来ショートが解禁されロングとショートが混在した場合、`sum(p.qty)` で相殺される。しかし、後続のリスクエンジン(`engine.py` の `es95`)ではポートフォリオのウェイト(`pos.value / nav`)を用いて計算され、`value` が正負の符号を持つため、ロングとショートのヘッジ効果がリターン系列上で過大評価される危険がある（分散が不当に縮小される）。
また、設計文書(`00-system-design.md` §9)において「グロス計算は行単位 Σ|qty|×時価（ポッド間でネットしない — 両建ての過小評価を防ぐ）」と定義されているが、リスク日次でのポジション集計時に FM（ポッド）間でネットして帳簿単位に集約しているため、この設計原則に違反している。

**推奨是正**: ポッド(FM)・銘柄単位でリスク計算に渡すか、グロス・エクスポージャーを適切に評価できる集計ロジック（ネットではなく絶対値での集約や、ロング・ショートの分離）に変更する。

### [重要] 所見-3: リミット注文における当日売買代金の過小評価(TOCTOU)

**根拠**: `src/ryza/gate/orders.py` `_daily_turnover` 関数
```python
        cur.execute(
            """
            SELECT COALESCE(sum(qty * COALESCE(limit_price, ref_price)), 0)
            FROM trading.orders
            WHERE book_id = %s
              AND status IN ('passed', 'submitted')
              AND (created_at AT TIME ZONE 'Asia/Tokyo')::date = %s
            """,
```
G-7（当日売買代金）の計算において、未約定の注文（`passed`, `submitted`）に対する評価額は、注文時に指定された `limit_price` または `ref_price` のいずれかを用いる。しかし、これらは注文時点での予想額に過ぎない。
実際の約定価格がこれを大きく上回った場合（例：流動性が乏しく大きくスリップした場合）、実際の当日累計売買代金は IPS のハードリミット（`daily_turnover_nav_max`：30%）を逸脱する。これは「暴走ガード」の目的（`dd_soft` 中の枠半減も含む）を迂回・無効化する可能性がある。

**推奨是正**: 実行（`apply_execution`）段階においても約定ベースでの当日累計売買代金を監視し、上限を超過しそうな場合は以降の注文を停止・ブロックする等の実行時キャパシティ管理の仕組みを追加検討する。

### [中] 所見-4: 設計文書のコンプライアンスゲート規則定義と実装の乖離

**根拠**: `docs/design/00-system-design.md` にはG-8の定義として「G-8 レバレッジ: 約定後グロス/NAV が 2.0 超なら block。ポッド別レバ上限も評価」とある。一方で、実装(`src/ryza/gate/compliance.py`)のdocstringでは「G-8 レバレッジ: 約定後グロス/NAV ≤ 2.0(IPS §3.2)+ポッド別上限(narrow only)」と記載されている。
また、`00-system-design.md` において「G-8 レバレッジ」の判定基準値（2.0）が実装上はハードコードではなく `ctx.ips.hard_limits.gross_leverage_max` から取得する形でパラメータ化されている。これは IPS による柔軟な運用を可能とする良い実装である反面、設計文書上は「2.0超ならblock」と固定値で断言されているため、ドキュメントの追従が必要である。

**推奨是正**: 設計文書（`00-system-design.md` など）の記述を、パラメータ化された実装の現状（`config/ips.yaml` に依存する形）に合わせて修正する。

### [軽微] 所見-5: `compliance.py` におけるナンピン（増し玉）時の平均単価計算の非対称性

**根拠**: `src/ryza/gate/orders.py` `apply_execution` 関数
```python
        if pre_qty == 0 or (pre_qty > 0) == (delta > 0):
            # 新規または増し玉: 移動平均で取得単価を更新。
            new_avg = (abs(pre_qty) * pre_avg + abs(delta) * price) / (abs(pre_qty) + abs(delta))
        elif new_qty == 0:
            new_avg = Decimal(0)  # 全クローズ
```
移動平均法による平均取得単価の計算において、`pre_qty` が負（ショートポジション）かつ `delta` が負（ナンピンでショート増し）の場合も `(abs(pre_qty) * pre_avg + abs(delta) * price)` の計算が適用される。理論上は正しいが、今後の信用取引解禁時に前提となる `pre_avg`（ショート時の売却単価）の会計的取り扱い（負の数量に対する原価の符号等）について、会計エンジン側の仕様と整合性を担保するテストが必須である。

**推奨是正**: 将来の空売り解禁時に会計・リスク評価が破綻しないよう、負の数量・負の原価が混在した状態での増し玉・部分決済の挙動を明示的にテストする項目を追加する。

---

## 検査したが所見なし（健全な領域）

### 検査項目: ①LLM 出力が発注・サイジング・リスク経路に直接入る箇所がないか（不変原則1）
**検査内容**: 
1. FM(Ben)の出力スキーマ(`fm/schemas.py`)において、`direction` は `buy` のみのEnumに制限されており、確信度・スコア・数量・金額を表すプロパティが定義されていないことを確認。
2. LLMの出力は `Intent` クラス(`fm/base.py`)を経由し、数量の決定は `fm/sizing.py` の決定論的スロット計算(`entry_qty`)にのみ委ねられていることを確認。サイジング関数の引数には確信度等のパラメータが一切存在しない。
3. `gate/compliance.py` は純粋な決定論的計算のみで構成されており、LLM推論や確率的プロセスへの依存がないことを確認。
**所見**: なし（設計原則「LLMは判断材料を作る側」がコード契約レベルで厳格に遵守されている）。

### 検査項目: ②ゲートが「唯一の発注経路」である保証
**検査内容**: 
1. 発注は `gate.orders.gate_and_record` 関数に集約されており、DBへの注文挿入（`INSERT INTO trading.orders`）はこの関数内にしか存在しないことを確認。
2. 適用除外（アドバイザリロックの`_GATE_LOCK_CLASS = 4014`）により、同一帳簿での並行ゲート判定時の競合（TOCTOU）が防止されていることを確認。
3. 状態遷移機械（`_TRANSITIONS`）により、ブロックされた注文(`blocked`)が約定プロセスに進むことを防ぐ二重防御が機能していることを確認。
**所見**: なし（経路の唯一性と排他制御が適切に保たれている）。

### 検査項目: ③fail-closed 原則の一貫性
**検査内容**: 
1. リスク状態を示す `risk.limits_state` が存在しない場合や、NAV等が未測定の場合は `None` として扱い、ゲート(`_g0_trading_state`, `_missing_inputs`)で全て `block` 判定とすることを確認。
2. 銘柄時価データ(`prices`)が欠落している場合、平均取得原価で代用せず `block` とする設計を確認。
3. リスクエンジン(`risk/engine.py`)において、データサンプル数が規定の営業日(`days`)に満たない場合は、`fail-safe` としてリスク超過フラグを立てない（偽陽性を防ぐ）一方で、測定不能であることを `notes` や `deferred` に記録し、システム全体が安全な方向に倒れることを確認。
**所見**: なし（データ欠落時や異常時において、安全側（閉じる側）に倒れる設計が徹底されている）。

### 検査項目: ④リスクリミット(dd_hard 等)の解除経路
**検拠**: `src/ryza/risk/state.py` `release_dd_hard` 関数
**検査内容**: 
`dd_hard`（ドローダウンのハードリミット）は `upsert_limits_state` において `OR` ラッチにより一度立ったら測定値低下では自動解除されない設計となっている。解除は `release_dd_hard` 関数のみから行われ、引数として `actor` と `reason` が必須であり、データベースの GUC 設定(`ryza.dd_hard_release`)を用いたトリガーベースの強制防御機構が備わっている。
**所見**: なし（リスクリミット解除の統制（Segregation of Duties）と監査証跡が適切に設計・実装されている）。