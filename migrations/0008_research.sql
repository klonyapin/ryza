-- 0008_research.sql
-- リサーチ層の取込が使うテーブル: 経済カレンダー・ウォッチリスト。
-- 設計書 docs/design/20-research.md §6 に準拠。market スキーマへ追加する
-- （0002_market.sql と同スキーマ。参照は「下→上」規約に従う）。

-- 経済カレンダー（指標発表・決算予定・政策イベント）。
-- 冪等性: 同一イベント（種別・タイトル・時刻・対象銘柄）の再取込で行が増えないよう
-- UNIQUE を張る。政策・指標イベントは instrument_id が NULL のため NULLS NOT DISTINCT
-- （PG15+）で「NULL 同士も同一」とみなし、ON CONFLICT DO NOTHING を効かせる。
CREATE TABLE market.calendar_events (
    event_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type    text NOT NULL,             -- indicator|earnings|policy|other
    title         text NOT NULL,
    scheduled_at  timestamptz NOT NULL,
    instrument_id bigint,                     -- 決算なら対象銘柄
    importance    int NOT NULL DEFAULT 1,     -- 1-3
    meta          jsonb,
    as_of         timestamptz NOT NULL,       -- この予定を知り得た時点
    run_id        bigint NOT NULL,            -- 生成ジョブ実行（リネージ）
    UNIQUE NULLS NOT DISTINCT (event_type, title, scheduled_at, instrument_id)
);

CREATE INDEX calendar_events_sched_idx ON market.calendar_events (scheduled_at);

-- ウォッチリスト（ユーザー + FM のウォッチ銘柄。前処理の重要度判定が参照）。
-- 追記のみ・冪等（PK で二重登録を弾く）。
CREATE TABLE market.watchlist (
    instrument_id bigint NOT NULL,
    added_by      text NOT NULL,              -- 'owner' | FM 名
    reason        text,
    added_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id, added_by)
);

-- データカタログの源泉となるコメント。
COMMENT ON TABLE market.calendar_events IS
    '経済カレンダー（指標・決算・政策イベント）。前処理の重要度判定と朝刊素材が参照（§6）。';
COMMENT ON COLUMN market.calendar_events.event_id IS 'イベントの一意 ID。';
COMMENT ON COLUMN market.calendar_events.event_type IS 'indicator|earnings|policy|other。';
COMMENT ON COLUMN market.calendar_events.title IS 'イベント名（例: 日銀金融政策決定会合）。';
COMMENT ON COLUMN market.calendar_events.scheduled_at IS '予定時刻。';
COMMENT ON COLUMN market.calendar_events.instrument_id IS '決算等の対象銘柄（政策・指標は NULL）。';
COMMENT ON COLUMN market.calendar_events.importance IS '重要度 1-3。';
COMMENT ON COLUMN market.calendar_events.meta IS '発表機関・国・詳細等。';
COMMENT ON COLUMN market.calendar_events.as_of IS 'この予定を知り得た時点（point-in-time）。';
COMMENT ON COLUMN market.calendar_events.run_id IS '生成ジョブ実行（リネージ）。';

COMMENT ON TABLE market.watchlist IS
    'ウォッチ銘柄（ユーザー + FM）。前処理の一次重要度スコアが保有/ウォッチ状況として参照（§3）。';
COMMENT ON COLUMN market.watchlist.instrument_id IS 'ウォッチ対象の銘柄 ID。';
COMMENT ON COLUMN market.watchlist.added_by IS "'owner' または FM 名。";
COMMENT ON COLUMN market.watchlist.reason IS '追加理由。';
COMMENT ON COLUMN market.watchlist.added_at IS '追加時刻。';
