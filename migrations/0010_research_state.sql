-- 0010_research_state.sql
-- 分析エージェント + 市場観ステート（T-011）が使う追加ステート。
-- 既存テーブル（0001-0009）は変更しない。docs.market_view / docs.research_reports は
-- 0003 で定義済みなのでここでは触れず、市場観の「決定論的更新規約」（設計 20-research §5）
-- を支える 3 つの追記オンリー台帳とビューだけを足す。
--
-- 冪等: すべて IF NOT EXISTS / CREATE OR REPLACE。追記オンリー（履歴は不変・上書きしない）。

-- ── 慣性ルール用の証拠台帳（append-only）───────────────────────────────────────
-- regime の反転（risk_on→risk_off 等）は「複数ソース・複数日にわたる証拠の蓄積」を要する
-- （§5-2）。反転の試行ごとに 1 行を追記し、蓄積量はこの台帳を集計して判定する。
-- 現在の regime が from_regime に一致する行だけが「生きた蓄積」＝ 反転が適用されると
-- current regime が変わり、古い (from,to) 行は自動的に無効化される（クエリ側で除外）。
CREATE TABLE IF NOT EXISTS docs.regime_flip_evidence (
    evidence_row_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dimension       text NOT NULL,          -- 例: 'jp_equity' | 'rates'
    from_regime     text NOT NULL,          -- 提案時点の現行 regime
    to_regime       text NOT NULL,          -- 反転先として提案された regime
    weight          numeric NOT NULL,       -- この証拠の強さ（0-1・magnitude 由来）
    evidence_day    date NOT NULL,          -- 「複数日」判定の単位
    source_count    int NOT NULL DEFAULT 1, -- この提案が参照した独立ソース数
    report_id       bigint,                 -- 提案元 editor レポート（docs.research_reports）
    applied         boolean NOT NULL DEFAULT false, -- この行の提案で反転が実際に適用されたか
    as_of           timestamptz NOT NULL,
    run_id          bigint NOT NULL
);

CREATE INDEX IF NOT EXISTS regime_flip_evidence_dim_idx
    ON docs.regime_flip_evidence (dimension, from_regime, to_regime);

-- ── 日次スナップショット（append-only）─────────────────────────────────────────
-- 朝刊素材・バックテスト用の point-in-time 市場観（§5-4）。docs.market_view の版（view_id）を
-- 各営業日に紐づける。同日に複数回撮っても追記し、最新（snapshot_id 最大）を「その日の版」とする。
CREATE TABLE IF NOT EXISTS docs.market_view_snapshots (
    snapshot_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_date date NOT NULL,
    view_id       bigint NOT NULL,          -- その時点で現行だった docs.market_view.view_id
    ts            timestamptz NOT NULL,      -- 版の時刻（market_view.ts）
    as_of         timestamptz NOT NULL,      -- スナップショットを撮った時点
    run_id        bigint NOT NULL
);

CREATE INDEX IF NOT EXISTS market_view_snapshots_date_idx
    ON docs.market_view_snapshots (snapshot_date);

-- その日の確定版（最後に撮ったスナップショット）を返すビュー。
CREATE OR REPLACE VIEW docs.market_view_daily AS
SELECT DISTINCT ON (snapshot_date)
    snapshot_date, view_id, ts, as_of, run_id, snapshot_id
FROM docs.market_view_snapshots
ORDER BY snapshot_date, snapshot_id DESC;

-- ── 速報トリガのイベント台帳（append-only）─────────────────────────────────────
-- magnitude が閾値超で発火する速報トリガ（§5-3）。press.outbox はまだ使わず、
-- 発火の事実だけを不変記録する（監査・事後検証用）。
CREATE TABLE IF NOT EXISTS docs.flash_triggers (
    trigger_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    view_id    bigint NOT NULL,             -- 契機となった市場観版
    magnitude  numeric NOT NULL,            -- 変化量スコア（0-1）
    reason     jsonb,                       -- 何が閾値を超えたか（適用差分の要約）
    as_of      timestamptz NOT NULL,
    run_id     bigint NOT NULL
);

-- ── データカタログ用コメント ──────────────────────────────────────────────────
COMMENT ON TABLE docs.regime_flip_evidence IS
    'regime 反転の慣性ルール用の証拠台帳（追記オンリー）。複数日・複数ソースの蓄積で反転可否を判定（§5-2）。';
COMMENT ON COLUMN docs.regime_flip_evidence.dimension IS 'regime の次元（jp_equity, rates 等）。';
COMMENT ON COLUMN docs.regime_flip_evidence.from_regime IS '提案時点の現行 regime（生きた蓄積の識別キー）。';
COMMENT ON COLUMN docs.regime_flip_evidence.to_regime IS '反転先として提案された regime。';
COMMENT ON COLUMN docs.regime_flip_evidence.weight IS 'この証拠の強さ（0-1）。';
COMMENT ON COLUMN docs.regime_flip_evidence.evidence_day IS '複数日判定の単位（営業日）。';
COMMENT ON COLUMN docs.regime_flip_evidence.source_count IS '提案が参照した独立ソース数。';
COMMENT ON COLUMN docs.regime_flip_evidence.applied IS 'この提案で反転が適用されたか。';

COMMENT ON TABLE docs.market_view_snapshots IS
    '市場観の日次 point-in-time スナップショット（追記オンリー・朝刊/バックテスト用・§5-4）。';
COMMENT ON VIEW docs.market_view_daily IS
    '各営業日の確定市場観版（最後に撮ったスナップショット）。';
COMMENT ON TABLE docs.flash_triggers IS
    '速報トリガの発火台帳（追記オンリー）。magnitude 閾値超で 1 行（§5-3）。press.outbox とは未接続。';
