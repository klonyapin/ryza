-- 0002_market.sql
-- market スキーマ: 銘柄マスタ・時系列バー・指標。設計書 §2 に完全準拠。
-- bars は PARTITION BY RANGE (ts) の親テーブルとし、pg_partman で月次パーティションを
-- 自動管理する（run_maintenance() の pg_cron 化は後続タスク。本タスクは partman 登録まで）。

CREATE SCHEMA IF NOT EXISTS market;

-- pg_partman は専用スキーマに導入する。
CREATE SCHEMA IF NOT EXISTS partman;
CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman;

-- 銘柄マスタ（SCD2: 履歴保持）
CREATE TABLE market.instruments (
    instrument_id  bigint GENERATED ALWAYS AS IDENTITY,
    symbol         text NOT NULL,             -- '7203.T', 'AAPL', 'USD/JPY', 'BTC-PERP'
    asset_class    text NOT NULL,             -- equity|etf|future|option|fx|crypto|bond
    venue          text NOT NULL,             -- TSE|NASDAQ|SAXO|DERIBIT|BINANCE_TESTNET...
    currency       text NOT NULL,
    multiplier     numeric NOT NULL DEFAULT 1,
    tick_size      numeric,
    margin_params  jsonb,                     -- 証拠金率・維持率等（資産クラス別）
    valid_from     timestamptz NOT NULL,
    valid_to       timestamptz,               -- NULL=現行
    PRIMARY KEY (instrument_id, valid_from)
);

-- 時系列バー（pg_partman で月次パーティション）
CREATE TABLE market.bars (
    instrument_id bigint NOT NULL,
    ts            timestamptz NOT NULL,       -- バーの時刻
    timeframe     text NOT NULL,              -- 1d|1h|5m ...
    open          numeric, high numeric, low numeric, close numeric,
    volume        numeric,
    source        text NOT NULL,             -- jquants|ibkr|saxo|binance_testnet
    as_of         timestamptz NOT NULL,      -- このデータを知り得た時点
    run_id        bigint NOT NULL,
    PRIMARY KEY (instrument_id, timeframe, ts, source, as_of)
) PARTITION BY RANGE (ts);

-- pg_partman v5 に月次パーティションとして登録する。PK に ts を含むため要件を満たす。
SELECT partman.create_parent(
    p_parent_table := 'market.bars',
    p_control      := 'ts',
    p_interval     := '1 month'
);

-- 指標（マクロ統計・派生指標）
CREATE TABLE market.indicators (
    series_code text NOT NULL,               -- 'JP_CPI', 'US_10Y', 'PORTFOLIO_VOL' ...
    ts          timestamptz NOT NULL,
    value       numeric NOT NULL,
    revision    int NOT NULL DEFAULT 0,       -- 統計の改定に対応
    as_of       timestamptz NOT NULL,         -- 発表時点（改定は as_of が進む）
    run_id      bigint NOT NULL,
    PRIMARY KEY (series_code, ts, revision)
);

-- point-in-time クエリ規約: 分析・バックテストは必ず as_of <= :t を通す。
-- 生テーブルへの直接クエリはリサーチ・戦略コードでは禁止（監査 A-10 対象）。
CREATE FUNCTION market.bars_asof(knowledge_time timestamptz)
RETURNS SETOF market.bars
LANGUAGE sql STABLE
AS $$
    SELECT DISTINCT ON (instrument_id, timeframe, ts, source) *
    FROM market.bars
    WHERE as_of <= knowledge_time
    ORDER BY instrument_id, timeframe, ts, source, as_of DESC
$$;

-- データカタログの源泉となるコメント。
COMMENT ON SCHEMA market IS '銘柄マスタ・時系列・指標。データ基盤部ジョブのみ書き込み。';

COMMENT ON TABLE market.instruments IS '銘柄マスタ（SCD2 で履歴保持）。';
COMMENT ON COLUMN market.instruments.instrument_id IS '銘柄の一意 ID（valid_from と複合 PK）。';
COMMENT ON COLUMN market.instruments.symbol IS 'ティッカー（7203.T, AAPL, USD/JPY, BTC-PERP）。';
COMMENT ON COLUMN market.instruments.asset_class IS 'equity|etf|future|option|fx|crypto|bond。';
COMMENT ON COLUMN market.instruments.venue IS '取引所・ブローカー（TSE|NASDAQ|SAXO|...）。';
COMMENT ON COLUMN market.instruments.currency IS '取引通貨。';
COMMENT ON COLUMN market.instruments.multiplier IS '契約乗数。';
COMMENT ON COLUMN market.instruments.tick_size IS '呼値。';
COMMENT ON COLUMN market.instruments.margin_params IS '証拠金率・維持率等（資産クラス別）。';
COMMENT ON COLUMN market.instruments.valid_from IS 'この版が有効になった時点。';
COMMENT ON COLUMN market.instruments.valid_to IS 'この版が無効になった時点（NULL=現行）。';

COMMENT ON TABLE market.bars IS '時系列バー。ts で月次パーティション（pg_partman）。';
COMMENT ON COLUMN market.bars.instrument_id IS '銘柄 ID。';
COMMENT ON COLUMN market.bars.ts IS 'バーの時刻（パーティションキー）。';
COMMENT ON COLUMN market.bars.timeframe IS '足種（1d|1h|5m ...）。';
COMMENT ON COLUMN market.bars.open IS '始値。';
COMMENT ON COLUMN market.bars.high IS '高値。';
COMMENT ON COLUMN market.bars.low IS '安値。';
COMMENT ON COLUMN market.bars.close IS '終値。';
COMMENT ON COLUMN market.bars.volume IS '出来高。';
COMMENT ON COLUMN market.bars.source IS 'データ源（jquants|ibkr|saxo|binance_testnet）。';
COMMENT ON COLUMN market.bars.as_of IS 'このデータを知り得た時点（point-in-time）。';
COMMENT ON COLUMN market.bars.run_id IS '生成ジョブ実行（リネージ）。';

COMMENT ON TABLE market.indicators IS 'マクロ統計・派生指標。改定は revision と as_of で表現。';
COMMENT ON COLUMN market.indicators.series_code IS '系列コード（JP_CPI, US_10Y, ...）。';
COMMENT ON COLUMN market.indicators.ts IS '対象時点。';
COMMENT ON COLUMN market.indicators.value IS '値。';
COMMENT ON COLUMN market.indicators.revision IS '統計の改定番号。';
COMMENT ON COLUMN market.indicators.as_of IS '発表時点（改定は as_of が進む）。';
COMMENT ON COLUMN market.indicators.run_id IS '生成ジョブ実行（リネージ）。';

COMMENT ON FUNCTION market.bars_asof(timestamptz) IS
    'point-in-time ビュー: as_of <= :t の最新版バーのみを返す（監査 A-10）。';
