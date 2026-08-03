-- 0016_nav_daily.sql
-- risk.nav_daily: 帳簿単位の日次 NAV 系列(T-016 締め処理が記帳、T-015 リスクエンジンが読む)。
--
-- 書き手: 締め処理(src/ryza/execution/close.py)が日次で upsert する(同日再締めは
--         上書き — ledger.nav_snapshots と同じ流儀)。正本は ledger(仕訳)であり、
--         本テーブルはリスク計算(DD・実現ボラ・ES)用の導出系列。
-- status: 執行照合(trading.executions × ledger 仕訳)とポジション照合(ledger ×
--         trading.positions)の両方が一致した日のみ confirmed(00 §9「照合 → NAV 確定」)。
--         ledger.nav_snapshots.status はポジション照合のみで確定する ledger 側の判定で、
--         本テーブルの status はそれに執行照合を重ねた厳しい側。
--
-- ★統合時の注意(T-015 との調整 — 設計リード管轄): T-015 指示書も「日次 NAV スナップ
--   ショットが無ければ risk.nav_daily(book_id×date×nav)を 0015 で新設」とするため、
--   並行実装で二重定義になり得る。その場合は本 0016 の定義(status/detail/run_id 付き)を
--   正として T-015 側の CREATE を落とすか、先に統合された側へ ALTER で整合させる。
--
-- 保護領域(定款第5条: スキーマ = migrations)。統合は独立役員審査+承認手続の対象。

CREATE TABLE risk.nav_daily (
    book_id    text NOT NULL REFERENCES ledger.books,
    nav_date   date NOT NULL,
    nav        numeric NOT NULL,
    status     text NOT NULL CHECK (status IN ('provisional', 'confirmed')),
    detail     jsonb NOT NULL DEFAULT '{}'::jsonb,
    run_id     bigint NOT NULL REFERENCES meta.runs (run_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (book_id, nav_date)
);

COMMENT ON TABLE risk.nav_daily IS
    '帳簿単位の日次 NAV 系列(締め処理が記帳)。リスクエンジン(T-015)の DD・ボラ・ES 入力。';
COMMENT ON COLUMN risk.nav_daily.nav_date IS '評価日(JST)。';
COMMENT ON COLUMN risk.nav_daily.nav IS 'NAV = 資産 − 負債(ledger.statements.book_totals)。';
COMMENT ON COLUMN risk.nav_daily.status IS
    'provisional|confirmed。執行照合とポジション照合の両方一致で confirmed(00 §9)。';
COMMENT ON COLUMN risk.nav_daily.detail IS '評価内訳(資産・負債・照合結果の要約)。';
