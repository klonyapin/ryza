# T-014: 取引スキーマ+コンプライアンスゲート(発注前ゲート)

- 起草: 2026-08-03 設計リード / 対象: 実装エージェント(この文書だけで実装可能な自己完結粒度)
- 前提知識: CLAUDE.md、docs/design/00-system-design.md §9(中央機能は唯一・ゲートが唯一の発注経路)、docs/design/06-constitution.md(定款)、config/ips.yaml(発効済み・`src/ryza/ips.py` ローダあり)、config/mandates/*.yaml、docs/design/81-fm-mandates.md
- **保護領域**: 本タスクの成果物(ゲート実装・migrations)は定款第5条の保護領域。統合は設計リードが独立役員審査+みなし承認手続で行う。コミットに偽の Approved トレーラを書かないこと(統合側が付す)

## 目的

デモ売買開始の第一歩として、(1) 注文・約定・ポジションの正となるスキーマ、(2) **唯一の発注経路であるコンプライアンスゲート**を実装する。LLM はこの経路に一切関与しない(不変原則1)。FM・PM 層(シグナル→注文案)と執行層(ブローカー接続)は後続タスク(T-015/016)であり、本タスクは「注文案を受け取り、判定し、記録する」まで。

## 1. マイグレーション `migrations/0014_trading.sql`

スキーマ `trading` を新設(既存の流儀: 0005 ledger・0012 killswitch を参照。追記オンリー系のトリガ・REVOKE の書き方は 0005/0013 と同型に):

1. `trading.orders` — 注文(FM の注文案がゲートを通った/落ちた記録を含む)
   - id BIGSERIAL PK / book_id(ledger の帳簿と同じ語彙。DEMO_FUND 等)/ fm text(pod。ben/jim/stan/peter)/ instrument_id(market 側の銘柄参照に整合)/ side CHECK(buy/sell/short/cover)/ qty NUMERIC / order_type CHECK(market/limit)/ limit_price NUMERIC NULL / status CHECK(proposed → passed|blocked → submitted → filled|cancelled|rejected)/ gate_log_id FK(下記)/ run_id FK meta.runs / created_at
   - **status 遷移はアプリ層で強制**(不正遷移は例外)。blocked の注文は端状態
2. `trading.executions` — 約定(追記オンリー: forbid_mutation トリガ+REVOKE)
   - id / order_id FK / qty / price / fee / executed_at / venue text(デモは 'demo')/ run_id / broker_ref text NULL
3. `trading.positions` — 現在ポジション(book_id×fm×instrument_id UNIQUE。qty・avg_cost・updated_at・run_id)。約定適用関数はアプリ層(§3)
4. `compliance.gate_log` — ゲート判定の監査ログ(**追記オンリー**。A-3「ゲート迂回検知 = executions×gate_log 突合」の正)
   - id / order_ref jsonb(判定対象の注文案スナップショット)/ book_id / fm / verdict CHECK(pass/warn/block)/ reasons jsonb(違反・警告の配列。空=pass)/ checked_rules jsonb(評価した規則 ID の列挙 — 監査で「何を見たか」を再現可能に)/ ips_version text / mandates_hash text / run_id / created_at

## 2. ゲート本体 `src/ryza/gate/compliance.py`

純決定論。入力: 注文案(dataclass)+現在状態(positions・当日売買代金・NAV・現金)+設定(IPSConfig・mandates)。出力: `GateResult(verdict, reasons, checked_rules)`。**判定順序は IPS → マンデート**(定款第4条: マンデートは狭める方向のみ)。

実装する規則(すべて config/ips.yaml・config/mandates/*.yaml の発効値から取る。ハードコード禁止):

- **G-0 取引状態**: `ops.trading_state` が normal 以外なら block(frozen 中の例外取引は既存の承認フロー経由であり、ゲートは常に block してよい — 承認済み例外は別経路で submitted にする設計は T-016 で扱う。ここでは block+reason で足りる)
- **G-1 商品許可**: products.default=deny — allowed リストにない商品種別は block。prohibitions.instruments(レバ/インバース ETF・監理銘柄)は block
- **G-2 ユニバース**: 注文の商品種別・市場が当該 FM のマンデート universe に含まれるか
- **G-3 発行体集中度**: 約定後想定ポジションが NAV の 20% 超なら block。**単元例外**(unit_lot_exception): 日本個別株の1単元目は取得価額が NAV の 35% 以下なら集中度超過を許容(信用買いは例外適用不可)
- **G-4 資産クラス**: 約定後の単一資産クラスグロスが NAV の 70% 超なら block(分類は ips.yaml の asset_class_taxonomy。equity_jp/us は別クラス、デリバは原資産分類)
- **G-5 暗号資産休眠**: crypto は block(crypto_dormant=true の間)
- **G-6 現金下限**: 約定後の現金が NAV の 5% を下回るなら block
- **G-7 売買代金**: 当日累計+本注文が NAV の 30% 超なら block
- **G-8 レバレッジ**: 約定後グロス/NAV が 2.0 超なら block。マンデートのポッド別レバ上限も評価(narrow only)
- **G-9 ショート**: 個別銘柄ショートは NAV の 10% まで。マンデートで short 禁止の FM は block
- **G-10 リスク状態**: `risk.limits_state`(§4 のスタブ)を参照し、dd_hard/vol_exceeded/es_exceeded が立っていれば block、dd_soft は新規建て枠半減の評価(ips.yaml の dd_soft_limit 条項)

各規則は独立の小関数+規則 ID を持ち、`checked_rules` に全評価規則を記録する。**fail-closed**: 判定に必要な入力(NAV・positions)が欠けていれば pass ではなく block(reason=入力不足)。

## 3. 付帯アプリ層

- `apply_execution(conn, execution)` — 約定を positions に反映(平均取得単価の更新・クローズ処理)。冪等(同一 execution 再適用は無視)
- `gate_and_record(conn, order_proposal, ...)` — ゲート評価 → gate_log 記帳 → orders 行を passed/blocked で作成、を1トランザクションで行う唯一の入口

## 4. リスク状態スタブ `migrations/0014` 内

`risk.limits_state`(単一行: book_id ごと。dd_soft/dd_hard/vol_exceeded/es_exceeded boolean+as_of+run_id)。値の算出はリスクエンジン(T-015)の管轄。本タスクではスキーマとゲートからの参照のみ。

## 5. テスト(tests/gate/)

- 全規則の pass/block 境界(値は config/ips.yaml の実値を読み込んで検証 — 保護領域のリグレッション検知を兼ねる)
- 単元例外の4象限(1単元/2単元・35%以下/超)
- マンデート narrow-only(IPS より緩いマンデート値が来ても IPS が勝つ)
- fail-closed(NAV 欠落→block)
- gate_and_record の原子性・gate_log 追記オンリー・executions 追記オンリー
- 既存全テストが壊れないこと: `.venv/bin/python -m pytest tests/ -q` + `.venv/bin/ruff check`

## 6. 受け入れ基準

1. 0014 適用後、`gate_and_record` で pass/block の注文が journal どおり記録される
2. ゲートを経ない orders 行を作る公開 API が存在しない(直接 INSERT は監査 A-3 が検知する前提だが、コード上の経路も gate_and_record のみ)
3. 全テスト+ruff 通過。config の値のハードコードなし
4. コミットは規則グループ単位で刻む(schema → gate 規則 → 付帯層)。日本語+Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>。push しない
