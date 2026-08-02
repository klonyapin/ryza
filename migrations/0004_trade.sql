-- 0004_trade.sql
-- trade スキーマ: シグナル・注文意図・注文・約定（判断来歴の背骨）。設計書 §4 に完全準拠。
-- decisions は独立テーブルではなく fills→orders→intents→signals の外部キー連鎖そのもの。

CREATE SCHEMA IF NOT EXISTS trade;

CREATE TABLE trade.signals (
    signal_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    strategy_id    text NOT NULL,
    strategy_ver   text NOT NULL,             -- git タグ。E1〜E7 検証記録と対応
    instrument_id  bigint NOT NULL,
    direction      text NOT NULL,             -- long|short|close|rebalance
    score          numeric,                   -- 生スコア（キャリブレーション前）
    rationale_refs jsonb NOT NULL,            -- report_id / view_id / 特徴量スナップショット
    ts             timestamptz NOT NULL,
    run_id         bigint NOT NULL
);

CREATE TABLE trade.order_intents (
    intent_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    track         text NOT NULL CHECK (track IN ('demo','live')),
    signal_ids    bigint[] NOT NULL,
    instrument_id bigint NOT NULL,
    side          text NOT NULL, qty numeric NOT NULL, order_type text NOT NULL,
    limit_price   numeric,
    sizing_calc   jsonb NOT NULL,             -- キャリブレーション・サイジングの計算過程
    risk_snapshot jsonb NOT NULL,             -- 発注時点のリスク指標
    gate_verdict  text NOT NULL CHECK (gate_verdict IN ('pass','warn','block')),
    gate_detail   jsonb NOT NULL,             -- 各ルールの判定（監査 A-3 対象）
    ts            timestamptz NOT NULL,
    run_id        bigint NOT NULL
);

CREATE TABLE trade.orders (
    order_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    intent_id        bigint NOT NULL REFERENCES trade.order_intents(intent_id),
    track            text NOT NULL,
    broker           text NOT NULL,           -- ibkr_paper|saxo_sim|binance_testnet|...
    broker_order_ref text,
    state            text NOT NULL,           -- draft|submitted|filled|partial|expired|rejected
    state_history    jsonb NOT NULL,          -- [{state, ts, evidence_ref}]
    ts               timestamptz NOT NULL
);

CREATE TABLE trade.fills (
    fill_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id    bigint NOT NULL REFERENCES trade.orders(order_id),
    qty         numeric NOT NULL, price numeric NOT NULL,
    fee         numeric NOT NULL DEFAULT 0,
    filled_at   timestamptz NOT NULL,
    evidence_id bigint NOT NULL               -- ブローカー約定レスポンス（ledger.evidence）
);

-- データカタログの源泉となるコメント。
COMMENT ON SCHEMA trade IS 'シグナル・注文・約定・判断来歴。戦略〜執行の各モジュールが書き込み。';

COMMENT ON TABLE trade.signals IS '戦略シグナル（判断来歴の起点）。';
COMMENT ON COLUMN trade.signals.signal_id IS 'シグナルの一意 ID。';
COMMENT ON COLUMN trade.signals.strategy_id IS '戦略 ID。';
COMMENT ON COLUMN trade.signals.strategy_ver IS '戦略バージョン（git タグ、E1〜E7 対応）。';
COMMENT ON COLUMN trade.signals.instrument_id IS '対象銘柄 ID。';
COMMENT ON COLUMN trade.signals.direction IS 'long|short|close|rebalance。';
COMMENT ON COLUMN trade.signals.score IS '生スコア（キャリブレーション前）。';
COMMENT ON COLUMN trade.signals.rationale_refs IS 'report_id / view_id / 特徴量スナップショット。';
COMMENT ON COLUMN trade.signals.ts IS 'シグナル時刻。';
COMMENT ON COLUMN trade.signals.run_id IS '生成ジョブ実行（リネージ）。';

COMMENT ON TABLE trade.order_intents IS '注文意図（サイジング・ゲート判定を含む）。';
COMMENT ON COLUMN trade.order_intents.intent_id IS '注文意図の一意 ID。';
COMMENT ON COLUMN trade.order_intents.track IS 'demo|live。';
COMMENT ON COLUMN trade.order_intents.signal_ids IS '根拠シグナル ID の配列。';
COMMENT ON COLUMN trade.order_intents.instrument_id IS '対象銘柄 ID。';
COMMENT ON COLUMN trade.order_intents.side IS '売買方向。';
COMMENT ON COLUMN trade.order_intents.qty IS '数量。';
COMMENT ON COLUMN trade.order_intents.order_type IS '注文種別。';
COMMENT ON COLUMN trade.order_intents.limit_price IS '指値。';
COMMENT ON COLUMN trade.order_intents.sizing_calc IS 'キャリブレーション・サイジングの計算過程。';
COMMENT ON COLUMN trade.order_intents.risk_snapshot IS '発注時点のリスク指標。';
COMMENT ON COLUMN trade.order_intents.gate_verdict IS 'pass|warn|block。';
COMMENT ON COLUMN trade.order_intents.gate_detail IS '各ルールの判定（監査 A-3 対象）。';
COMMENT ON COLUMN trade.order_intents.ts IS '意図生成時刻。';
COMMENT ON COLUMN trade.order_intents.run_id IS '生成ジョブ実行（リネージ）。';

COMMENT ON TABLE trade.orders IS '注文（ブローカーへの発注状態）。';
COMMENT ON COLUMN trade.orders.order_id IS '注文の一意 ID。';
COMMENT ON COLUMN trade.orders.intent_id IS '元の注文意図。';
COMMENT ON COLUMN trade.orders.track IS 'demo|live。';
COMMENT ON COLUMN trade.orders.broker IS 'ブローカー（ibkr_paper|saxo_sim|...）。';
COMMENT ON COLUMN trade.orders.broker_order_ref IS 'ブローカー側注文参照。';
COMMENT ON COLUMN trade.orders.state IS 'draft|submitted|filled|partial|expired|rejected。';
COMMENT ON COLUMN trade.orders.state_history IS '状態遷移履歴 [{state, ts, evidence_ref}]。';
COMMENT ON COLUMN trade.orders.ts IS '注文時刻。';

COMMENT ON TABLE trade.fills IS '約定（証憑必須）。';
COMMENT ON COLUMN trade.fills.fill_id IS '約定の一意 ID。';
COMMENT ON COLUMN trade.fills.order_id IS '元の注文。';
COMMENT ON COLUMN trade.fills.qty IS '約定数量。';
COMMENT ON COLUMN trade.fills.price IS '約定価格。';
COMMENT ON COLUMN trade.fills.fee IS '手数料。';
COMMENT ON COLUMN trade.fills.filled_at IS '約定時刻。';
COMMENT ON COLUMN trade.fills.evidence_id IS 'ブローカー約定レスポンスの証憑（ledger.evidence）。';
