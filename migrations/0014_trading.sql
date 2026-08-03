-- 0014_trading.sql
-- 取引スキーマ+コンプライアンスゲート(T-014)。デモ売買開始の第一歩。
--
-- 新設スキーマ:
--   trading    … 注文・約定・現在ポジション(正)。0004 の trade スキーマ(v2 設計の
--                判断来歴)とは独立に、ゲートを唯一の発注経路とする本則系。
--   compliance … ゲート判定の監査ログ(追記オンリー)。A-3「ゲート迂回検知 =
--                executions×gate_log 突合」の正。
--   risk       … リスク状態スタブ(risk.limits_state)。値の算出はリスクエンジン
--                (T-015)の管轄。本タスクではスキーマとゲートからの参照のみ。
--
-- 整合性の要(受け入れ基準・テスト対象):
--   1. trading.orders.gate_log_id は NOT NULL — ゲート判定を経ない注文行は
--      スキーマ上つくれない(唯一の発注経路 = 00-system-design §9)
--   2. trading.executions / compliance.gate_log / trading.position_applies は
--      追記オンリー(0005 ledger.forbid_mutation と同型のトリガ+REVOKE)
--   3. 全テーブル run_id を持ち meta.runs へ FK(リネージ — 不変原則3・0013 の慣行)。
--      risk.limits_state のみ NULL 許容(リスクエンジン実装前のスタブのため)
--   4. book_id は ledger.books へ FK(帳簿語彙の統一。帳簿間の混合参照は書かない)
--
-- 保護領域(定款第5条): 本スキーマと対応コード(src/ryza/gate/)の変更は
-- 独立役員審査+みなし承認手続の対象。

CREATE SCHEMA IF NOT EXISTS trading;
CREATE SCHEMA IF NOT EXISTS compliance;
CREATE SCHEMA IF NOT EXISTS risk;

-- ────────────────────────────────────────────────────────────────────────────
-- compliance.gate_log: ゲート判定の監査ログ(追記オンリー)
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE compliance.gate_log (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_ref     jsonb NOT NULL,             -- 判定対象の注文案スナップショット
    book_id       text NOT NULL REFERENCES ledger.books,
    fm            text NOT NULL,              -- pod(ben/jim/stan/peter)
    verdict       text NOT NULL CHECK (verdict IN ('pass', 'warn', 'block')),
    reasons       jsonb NOT NULL DEFAULT '[]'::jsonb,  -- 違反・警告の配列。空=pass
    checked_rules jsonb NOT NULL,             -- 評価した規則 ID の列挙(監査で再現可能に)
    ips_version   text NOT NULL,              -- 判定に使った IPS の版(config/ips.yaml)
    mandates_hash text NOT NULL,              -- 判定に使ったマンデート集合の sha256
    run_id        bigint NOT NULL REFERENCES meta.runs (run_id),
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX gate_log_book_created_idx ON compliance.gate_log (book_id, created_at);

-- ────────────────────────────────────────────────────────────────────────────
-- trading.orders: 注文(ゲートを通った/落ちた記録を含む)
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE trading.orders (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id       text NOT NULL REFERENCES ledger.books,   -- ledger と同じ語彙(DEMO_FUND 等)
    fm            text NOT NULL,              -- pod(ben/jim/stan/peter)
    instrument_id bigint NOT NULL,            -- market.instruments の instrument_id(SCD2 のため FK なし — 0004 と同じ)
    side          text NOT NULL CHECK (side IN ('buy', 'sell', 'short', 'cover')),
    qty           numeric NOT NULL CHECK (qty > 0),
    order_type    text NOT NULL CHECK (order_type IN ('market', 'limit')),
    limit_price   numeric CHECK ((order_type = 'limit') = (limit_price IS NOT NULL)),
    ref_price     numeric,                    -- ゲート判定に使った参照価格(成行の想定代金計算 = G-7 の正)
    status        text NOT NULL CHECK (status IN
                  ('proposed', 'passed', 'blocked', 'submitted',
                   'filled', 'cancelled', 'rejected')),
                  -- 遷移: proposed → passed|blocked → submitted → filled|cancelled|rejected。
                  -- blocked は端状態。遷移の強制はアプリ層(src/ryza/gate/orders.py)
    gate_log_id   bigint NOT NULL REFERENCES compliance.gate_log (id),
                  -- ★NOT NULL = ゲート判定を経ない注文行はスキーマ上つくれない
    run_id        bigint NOT NULL REFERENCES meta.runs (run_id),
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX orders_book_created_idx ON trading.orders (book_id, created_at);
CREATE INDEX orders_status_idx ON trading.orders (status);

-- ────────────────────────────────────────────────────────────────────────────
-- trading.executions: 約定(追記オンリー)
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE trading.executions (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id    bigint NOT NULL REFERENCES trading.orders (id),
    qty         numeric NOT NULL CHECK (qty > 0),
    price       numeric NOT NULL CHECK (price >= 0),
    fee         numeric NOT NULL DEFAULT 0,
    executed_at timestamptz NOT NULL,
    venue       text NOT NULL,                -- デモは 'demo'
    broker_ref  text,
    run_id      bigint NOT NULL REFERENCES meta.runs (run_id)
);
CREATE INDEX executions_order_idx ON trading.executions (order_id);
CREATE INDEX executions_executed_idx ON trading.executions (executed_at);

-- ────────────────────────────────────────────────────────────────────────────
-- trading.positions: 現在ポジション(約定適用関数はアプリ層 apply_execution)
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE trading.positions (
    book_id       text NOT NULL REFERENCES ledger.books,
    fm            text NOT NULL,
    instrument_id bigint NOT NULL,
    asset_class   text NOT NULL,              -- IPS §8.1 タクソノミー(ゲート G-4/G-5 の分類)
    qty           numeric NOT NULL,           -- 符号付き(負=ショート)
    avg_cost      numeric NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    run_id        bigint NOT NULL REFERENCES meta.runs (run_id),
    PRIMARY KEY (book_id, fm, instrument_id)  -- book_id×fm×instrument_id UNIQUE
);

-- apply_execution の冪等性台帳: 適用済み execution の追記オンリー記録。
-- 同一 execution の再適用は PK 衝突(ON CONFLICT DO NOTHING)で無視される。
CREATE TABLE trading.position_applies (
    execution_id bigint PRIMARY KEY REFERENCES trading.executions (id),
    applied_at   timestamptz NOT NULL DEFAULT now(),
    run_id       bigint NOT NULL REFERENCES meta.runs (run_id)
);

-- ────────────────────────────────────────────────────────────────────────────
-- risk.limits_state: リスク状態スタブ(book_id ごと単一行。算出は T-015)
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE risk.limits_state (
    book_id      text PRIMARY KEY REFERENCES ledger.books,
    dd_soft      boolean NOT NULL DEFAULT false,  -- DD 15%: 警告+新規建て枠半減(IPS §3.2)
    dd_hard      boolean NOT NULL DEFAULT false,  -- DD 25%: 全新規発注停止(復帰は委員会のみ)
    vol_exceeded boolean NOT NULL DEFAULT false,  -- 実現ボラ上限超過: 新規建てブロック
    es_exceeded  boolean NOT NULL DEFAULT false,  -- 日次 ES(95%)上限超過: 新規建てブロック
    as_of        timestamptz NOT NULL,
    run_id       bigint REFERENCES meta.runs (run_id)  -- NULL 許容(T-015 実装前のスタブ)
);

-- ────────────────────────────────────────────────────────────────────────────
-- 追記オンリーの強制(0005 ledger.forbid_mutation / 0013 と同型)
-- ────────────────────────────────────────────────────────────────────────────
CREATE FUNCTION trading.forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        '% は % では禁止(追記オンリー)。約定・ゲート判定は監査証跡(A-3)。訂正は追記で行う',
        TG_OP, TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER executions_no_mutation
    BEFORE UPDATE OR DELETE ON trading.executions
    FOR EACH ROW EXECUTE FUNCTION trading.forbid_mutation();

CREATE TRIGGER position_applies_no_mutation
    BEFORE UPDATE OR DELETE ON trading.position_applies
    FOR EACH ROW EXECUTE FUNCTION trading.forbid_mutation();

CREATE TRIGGER gate_log_no_mutation
    BEFORE UPDATE OR DELETE ON compliance.gate_log
    FOR EACH ROW EXECUTE FUNCTION trading.forbid_mutation();

REVOKE UPDATE, DELETE ON trading.executions FROM PUBLIC;
REVOKE UPDATE, DELETE ON trading.position_applies FROM PUBLIC;
REVOKE UPDATE, DELETE ON compliance.gate_log FROM PUBLIC;

-- ────────────────────────────────────────────────────────────────────────────
-- データカタログ用コメント
-- ────────────────────────────────────────────────────────────────────────────
COMMENT ON SCHEMA trading IS
    '注文・約定・現在ポジション(本則系)。発注経路は compliance ゲート経由のみ(§9)。';
COMMENT ON SCHEMA compliance IS
    'コンプライアンスゲートの判定ログ。追記オンリー。A-3 迂回検知の正。';
COMMENT ON SCHEMA risk IS
    'リスク状態(limits_state)。算出はリスクエンジン(T-015)、参照はゲート G-10。';

COMMENT ON TABLE compliance.gate_log IS
    'ゲート判定の監査ログ(追記オンリー)。orders.gate_log_id が必ずこれを指す。';
COMMENT ON COLUMN compliance.gate_log.order_ref IS '判定対象の注文案スナップショット(JSON)。';
COMMENT ON COLUMN compliance.gate_log.verdict IS 'pass|warn|block。';
COMMENT ON COLUMN compliance.gate_log.reasons IS
    '違反・警告の配列 [{rule, severity, message}]。空=pass。';
COMMENT ON COLUMN compliance.gate_log.checked_rules IS
    '評価した規則 ID の列挙(監査で「何を見たか」を再現可能に)。';
COMMENT ON COLUMN compliance.gate_log.ips_version IS '判定に使った IPS 版(config/ips.yaml)。';
COMMENT ON COLUMN compliance.gate_log.mandates_hash IS '判定に使ったマンデート集合の sha256。';

COMMENT ON TABLE trading.orders IS
    '注文。gate_log_id NOT NULL によりゲート判定を経ない行はつくれない。遷移はアプリ層で強制。';
COMMENT ON COLUMN trading.orders.status IS
    'proposed → passed|blocked → submitted → filled|cancelled|rejected。blocked は端状態。';
COMMENT ON COLUMN trading.orders.ref_price IS
    'ゲート判定時の参照価格。成行注文の想定代金(G-7 売買代金)の計算根拠。';
COMMENT ON COLUMN trading.orders.gate_log_id IS 'ゲート判定ログ(NOT NULL=唯一の発注経路)。';

COMMENT ON TABLE trading.executions IS '約定(追記オンリー)。venue=demo はデモ執行。';
COMMENT ON COLUMN trading.executions.broker_ref IS 'ブローカー側の約定参照(デモは NULL 可)。';

COMMENT ON TABLE trading.positions IS
    '現在ポジション(book_id×fm×instrument_id 一意)。更新は apply_execution のみ。';
COMMENT ON COLUMN trading.positions.qty IS '符号付き数量(負=ショート)。';
COMMENT ON COLUMN trading.positions.asset_class IS
    'IPS §8.1 タクソノミー(equity_jp 等・デリバは原資産分類)。ゲート G-4/G-5 が分類に使う。';

COMMENT ON TABLE trading.position_applies IS
    'apply_execution の冪等性台帳(追記オンリー)。適用済み execution_id を記録。';

COMMENT ON TABLE risk.limits_state IS
    'リスク状態スタブ(book_id ごと単一行)。算出は T-015。ゲート G-10 が参照(行が無ければ fail-closed)。';
COMMENT ON COLUMN risk.limits_state.dd_soft IS 'DD 15% 到達: 警告+新規建て枠半減(IPS §3.2)。';
COMMENT ON COLUMN risk.limits_state.dd_hard IS 'DD 25% 到達: 全新規発注停止(IPS §3.2)。';
