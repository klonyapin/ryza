-- 0015_risk.sql
-- リスクエンジン MVP(T-015)。risk.limits_state の権限設計+イベント台帳+銘柄分類。
--
-- 対応する引き継ぎ事項(docs/reviews/t014-design-decisions.md §3):
--   1. 「risk.limits_state に REVOKE が無く任意接続が dd_hard を消せる」への権限設計:
--      - dd_hard の true→false 遷移はトリガで禁止し、解除は release_dd_hard
--        (src/ryza/risk/state.py — 委員会の明示操作)だけが持つ解除キー
--        (トランザクション局所 GUC ``ryza.dd_hard_release``)を立てて行う。
--        エンジンの日次更新経路はキーを立てないため、バグっても dd_hard を消せない。
--        dd_hard 保持中の行 DELETE は禁止(DELETE→INSERT(false) による迂回の封鎖)
--        + REVOKE DELETE。
--        ※ アプリ用 DB ロールが単一のため、悪意ある接続が GUC を自ら立てる操作までは
--        防げない(それは release 関数の呼び出しと等価な明示操作)。ロール分離による
--        完全な権限分離は実弾移行時の課題として宣言する。
--   2. 全状態変更の追記オンリー台帳 risk.limits_state_events(誰が・いつ・なぜ —
--      dd_hard 解除は actor と reason 必須)。
--   3. 「instrument_flags の空 vs 未取得の区別」+「G-2 universe_tags の銘柄マスタ由来
--      分類の配線」: market.instrument_classification を新設。**行が無い=未取得**
--      (ゲートはタグ空で fail-closed block)、**行があり配列が空=取得済みで該当なし**。
--      分類の生成は決定論コード(src/ryza/risk/classify.py)のみ。LLM 不関与。
--
-- 保護領域(定款第5条): 本スキーマと対応コード(src/ryza/risk/)の変更は
-- 独立役員審査+みなし承認手続の対象。

-- ────────────────────────────────────────────────────────────────────────────
-- risk.limits_state_events: リスク状態変更の追記オンリー台帳
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE risk.limits_state_events (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id      text NOT NULL REFERENCES ledger.books,
    event        text NOT NULL CHECK (event IN ('engine_update', 'dd_hard_release')),
    dd_soft      boolean NOT NULL,
    dd_hard      boolean NOT NULL,           -- 変更後の実効値(ラッチ適用後)
    vol_exceeded boolean NOT NULL,
    es_exceeded  boolean NOT NULL,
    metrics      jsonb NOT NULL DEFAULT '{}'::jsonb,  -- 測定値(dd・実現ボラ・ES 等)
    actor        text NOT NULL,              -- 実行主体(risk.daily / 委員会操作者)
    reason       text,                       -- dd_hard_release は必須(下の CHECK)
    as_of        timestamptz NOT NULL,
    run_id       bigint NOT NULL REFERENCES meta.runs (run_id),
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT release_requires_reason CHECK (
        event <> 'dd_hard_release' OR (reason IS NOT NULL AND length(trim(reason)) > 0)
    )
);
CREATE INDEX limits_state_events_book_idx ON risk.limits_state_events (book_id, created_at);

CREATE TRIGGER limits_state_events_no_mutation
    BEFORE UPDATE OR DELETE ON risk.limits_state_events
    FOR EACH ROW EXECUTE FUNCTION trading.forbid_mutation();

REVOKE UPDATE, DELETE ON risk.limits_state_events FROM PUBLIC;

-- ────────────────────────────────────────────────────────────────────────────
-- risk.limits_state の dd_hard ラッチ執行(トリガ — 所有者にも効く)
-- ────────────────────────────────────────────────────────────────────────────
CREATE FUNCTION risk.guard_limits_state() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        -- dd_hard 保持中の行削除は禁止(DELETE→再INSERT(false) による迂回封鎖)。
        -- dd_hard=false の行の削除はラッチと無関係のため許す(解除済み後の整理等)。
        IF OLD.dd_hard THEN
            RAISE EXCEPTION
                'dd_hard 保持中の risk.limits_state 行は削除できない(先に委員会解除 — IPS §3.2)';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.dd_hard AND NOT NEW.dd_hard
       AND current_setting('ryza.dd_hard_release', true) IS DISTINCT FROM OLD.book_id THEN
        RAISE EXCEPTION
            'dd_hard の解除は委員会の明示操作(risk.state.release_dd_hard)のみ(IPS §3.2 復帰条項)';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER limits_state_guard
    BEFORE UPDATE OR DELETE ON risk.limits_state
    FOR EACH ROW EXECUTE FUNCTION risk.guard_limits_state();

REVOKE DELETE ON risk.limits_state FROM PUBLIC;

-- ────────────────────────────────────────────────────────────────────────────
-- market.instrument_classification: 銘柄マスタ由来の決定論分類(ゲート入力の正)
-- ────────────────────────────────────────────────────────────────────────────
-- instrument_id は market.instruments(SCD2)のため FK なし(0004・0014 と同じ)。
CREATE TABLE market.instrument_classification (
    instrument_id    bigint PRIMARY KEY,
    universe_tags    text[] NOT NULL,        -- マンデート universe 語彙(空=どのユニバースにも属さない)
    instrument_flags text[] NOT NULL,        -- prohibitions.instruments 語彙(空=確認済み該当なし)
    is_single_name   boolean,                -- NULL=分類不能(依存規則は fail-closed — 審査条件7)
    product          text NOT NULL,          -- ips.products 語彙(空文字=分類不能 → G-1 block)
    unit_size        numeric,                -- 日本個別株の1単元株数(単元例外判定)
    source           text NOT NULL,          -- rules:<version> | curated(分類の出所)
    as_of            timestamptz NOT NULL,   -- 分類を確定した時点
    run_id           bigint NOT NULL REFERENCES meta.runs (run_id)
);

-- ────────────────────────────────────────────────────────────────────────────
-- データカタログ用コメント
-- ────────────────────────────────────────────────────────────────────────────
COMMENT ON TABLE risk.limits_state_events IS
    'リスク状態変更の追記オンリー台帳。engine_update=日次エンジン更新、'
    'dd_hard_release=委員会の明示解除(actor・reason 必須)。';
COMMENT ON COLUMN risk.limits_state_events.dd_hard IS '変更後の実効値(ラッチ適用後)。';
COMMENT ON COLUMN risk.limits_state_events.metrics IS
    '測定値スナップショット(drawdown/peak_nav/nav/ewma_vol/es95 等 — 監査再現性)。';

COMMENT ON FUNCTION risk.guard_limits_state IS
    'dd_hard true→false は解除キー(GUC ryza.dd_hard_release=book_id)なしでは禁止。'
    '行 DELETE は無条件禁止。解除キーを立てるのは risk.state.release_dd_hard のみ。';

COMMENT ON TABLE market.instrument_classification IS
    '銘柄マスタ由来の決定論分類(ゲート G-1/G-2 入力の正)。行なし=未取得(fail-closed)、'
    '配列空=取得済み該当なし(T-014 審査条件7の残り半分への対応)。生成は決定論コードのみ。';
COMMENT ON COLUMN market.instrument_classification.universe_tags IS
    'マンデート universe との照合タグ(jp_equity_cash 等)。空配列=どのユニバースにも属さない。';
COMMENT ON COLUMN market.instrument_classification.instrument_flags IS
    '禁止商品フラグ(leveraged_etf 等)。空配列=確認済み該当なし(行なし=未確認と区別)。';
COMMENT ON COLUMN market.instrument_classification.source IS
    '分類の出所。rules:<version>=決定論ルール、curated=人手キュレーション(承認記録必須)。';
