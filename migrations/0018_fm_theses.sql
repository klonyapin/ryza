-- 0018_fm_theses.sql
-- FM エージェント第一陣(T-017)の提案記録。
--
-- 採番(設計リード裁定 2026-08-03): 指示書の `0016_fm_theses.sql` は 0016(nav_daily)・
-- 0017(discord_webhooks)と衝突するため **0018** とする。
--
-- 新設: trading.fm_theses(追記オンリー)= FM の「この銘柄をこう見る/この論点が崩れたら
-- 降りる」の永続記憶。governance.stances(役員の主張・懸念)と同じ思想で、FM 別・新しい順に
-- 次回セッションへ注入する(T-017 指示書7)。
-- 追加: trading.orders.thesis_id — 注文が「どの論拠から出たか」を辿るリンク(指示書2)。
--
-- 設計上の判断:
--   1. **追記オンリー**(0014 の trading.forbid_mutation を再利用)。判断の履歴は書き換えない。
--      訂正は新しい thesis を追記する(ledger・gate_log と同じ流儀)
--   2. **evidence_refs は必須**: NOT NULL かつ空配列禁止(CHECK)。中身の point-in-time 検証
--      (as_of 以前の docs/reports/bars/indicators のみ)はアプリ層 `src/ryza/fm/theses.py`
--      が行う — 参照先が複数スキーマに分散し SQL の CHECK では表現できないため(不変原則4)
--   3. **invalidation_md は必須**: NOT NULL かつ空文字禁止。「この論点が崩れたら降りる」の
--      明示を全提案の義務とする(40-fund-managers.md §制約1 — ペルソナ型エージェントの
--      事前分布への固執 = Alpha Illusion §3.3 への対策)
--   4. **direction は buy|close|short**。第一陣(Ben/Jim)は **long-only**(ledger が空売りの
--      記帳に未対応 — execution/runner.py の設計リード裁定)。short は将来の陣容のために語彙
--      としてのみ残し、生成側(src/ryza/fm/)が出さないことをテストで固定する
--   5. **ゲート判定は本表に書き戻さない**(追記オンリーのため)。判定結果は
--      orders.thesis_id → orders.status / compliance.gate_log を辿って参照する
--      (block された案を次回プロンプトに載せる学習材料 — 指示書6)
--   6. **origin の排他**: rule_id(決定論シグナル)と model(LLM)はどちらか一方のみ。
--      「この提案は誰が作ったか」を後から曖昧にしない(不変原則1・リネージ)
--
-- 保護領域(定款第5条): 本スキーマと対応コード(src/ryza/fm/)の変更は独立役員審査+
-- みなし承認手続の対象。

-- ────────────────────────────────────────────────────────────────────────────
-- trading.fm_theses: FM の提案・論拠(追記オンリー)
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE trading.fm_theses (
    thesis_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fm              text NOT NULL,              -- pod(ben/jim/stan/peter)
    book_id         text NOT NULL REFERENCES ledger.books,
    instrument_id   bigint NOT NULL,            -- market.instruments(SCD2 のため FK なし)
    direction       text NOT NULL CHECK (direction IN ('buy', 'close', 'short')),
    thesis_md       text NOT NULL CHECK (length(btrim(thesis_md)) > 0),
    evidence_refs   jsonb NOT NULL CHECK (
                        jsonb_typeof(evidence_refs) = 'array'
                        AND jsonb_array_length(evidence_refs) > 0
                    ),                          -- point-in-time 検証はアプリ層(判断2)
    invalidation_md text NOT NULL CHECK (length(btrim(invalidation_md)) > 0),
    producer        text NOT NULL,              -- 生成ジョブ(fm.jim.daily / fm.ben.weekly)
    rule_id         text,                       -- 決定論シグナルの規則 ID(Jim)
    model           text,                       -- LLM のモデル名(Ben)
    as_of           timestamptz NOT NULL,       -- 判断の知識時点(point-in-time の基準)
    run_id          bigint NOT NULL REFERENCES meta.runs (run_id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT thesis_origin_exclusive CHECK (num_nonnulls(rule_id, model) = 1)
);
CREATE INDEX fm_theses_fm_idx ON trading.fm_theses (fm, thesis_id DESC);
CREATE INDEX fm_theses_instrument_idx ON trading.fm_theses (instrument_id, thesis_id DESC);

CREATE TRIGGER fm_theses_no_mutation
    BEFORE UPDATE OR DELETE ON trading.fm_theses
    FOR EACH ROW EXECUTE FUNCTION trading.forbid_mutation();

REVOKE UPDATE, DELETE ON trading.fm_theses FROM PUBLIC;

-- ────────────────────────────────────────────────────────────────────────────
-- TRUNCATE の封鎖(独立役員審査 2026-08-03 C-2)
-- ────────────────────────────────────────────────────────────────────────────
-- **TRUNCATE は行トリガを迂回する**(0015 の独立役員審査で実証済み)。0015 は文トリガ+
-- REVOKE TRUNCATE を追記オンリー表の標準としたが、0014(trading/compliance)はこの標準の
-- 前に書かれており文トリガを持たない。後発の 0018 が同じ穴を再導入しないだけでなく、
-- 0014 の既存の穴もここで塞ぐ(審査指摘)。`TRUNCATE trading.fm_theses CASCADE` は
-- FK でつながる trading.orders まで巻き込むため、封鎖は両表に必要。
CREATE FUNCTION trading.forbid_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        '% の TRUNCATE は禁止(追記オンリーの監査証跡・発注経路の証跡)。訂正は追記で行う',
        TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER fm_theses_no_truncate
    BEFORE TRUNCATE ON trading.fm_theses
    FOR EACH STATEMENT EXECUTE FUNCTION trading.forbid_truncate();

-- 0014 の表(ゲート判定ログ・約定・冪等台帳・注文)も同じ基準に揃える。
CREATE TRIGGER gate_log_no_truncate
    BEFORE TRUNCATE ON compliance.gate_log
    FOR EACH STATEMENT EXECUTE FUNCTION trading.forbid_truncate();

CREATE TRIGGER executions_no_truncate
    BEFORE TRUNCATE ON trading.executions
    FOR EACH STATEMENT EXECUTE FUNCTION trading.forbid_truncate();

CREATE TRIGGER position_applies_no_truncate
    BEFORE TRUNCATE ON trading.position_applies
    FOR EACH STATEMENT EXECUTE FUNCTION trading.forbid_truncate();

-- trading.orders は行の UPDATE(状態遷移)を許す可変表だが、TRUNCATE は
-- gate_log_id / thesis_id の証跡ごと発注履歴を消すため封鎖する(A-3 ゲート迂回検知の前提)。
CREATE TRIGGER orders_no_truncate
    BEFORE TRUNCATE ON trading.orders
    FOR EACH STATEMENT EXECUTE FUNCTION trading.forbid_truncate();

REVOKE TRUNCATE ON trading.fm_theses FROM PUBLIC;
REVOKE TRUNCATE ON trading.orders FROM PUBLIC;
REVOKE TRUNCATE ON trading.executions FROM PUBLIC;
REVOKE TRUNCATE ON trading.position_applies FROM PUBLIC;
REVOKE TRUNCATE ON compliance.gate_log FROM PUBLIC;

-- ────────────────────────────────────────────────────────────────────────────
-- trading.orders.thesis_id: 注文案 → 論拠のリンク
-- ────────────────────────────────────────────────────────────────────────────
-- NULL 許容: ゲート(T-014)は FM 以外の経路(委員会の例外取引・リバランス等)からも
-- 呼ばれ得るため、thesis を NOT NULL にはしない。FM 経路が thesis_id を必ず埋めることは
-- アプリ層(src/ryza/fm/base.py)とテストで担保する。
ALTER TABLE trading.orders
    ADD COLUMN thesis_id bigint REFERENCES trading.fm_theses (thesis_id);
CREATE INDEX orders_thesis_idx ON trading.orders (thesis_id);

-- ────────────────────────────────────────────────────────────────────────────
-- データカタログ用コメント
-- ────────────────────────────────────────────────────────────────────────────
COMMENT ON TABLE trading.fm_theses IS
    'FM の提案と論拠(追記オンリー)。全提案に evidence_refs と invalidation を要求する。'
    'ゲート判定は書き戻さず orders.thesis_id 経由で辿る。';
COMMENT ON COLUMN trading.fm_theses.direction IS
    'buy|close|short。第一陣(Ben/Jim)は long-only のため buy|close のみ生成する。';
COMMENT ON COLUMN trading.fm_theses.evidence_refs IS
    '証憑参照の配列(空不可)。[{kind: document|research_report|bar|indicator, ...}]。'
    'as_of 以前の証憑のみ(point-in-time — 不変原則4。検証は src/ryza/fm/theses.py)。';
COMMENT ON COLUMN trading.fm_theses.invalidation_md IS
    '反証条件(この論点が崩れたら降りる)。空不可 — 40-fund-managers.md §制約1。';
COMMENT ON COLUMN trading.fm_theses.producer IS '生成ジョブ(fm.jim.daily / fm.ben.weekly)。';
COMMENT ON COLUMN trading.fm_theses.rule_id IS
    '決定論シグナルの規則 ID(Jim)。model と排他 — どちらか一方のみ。';
COMMENT ON COLUMN trading.fm_theses.model IS
    'LLM のモデル名(Ben)。rule_id と排他 — どちらか一方のみ。';
COMMENT ON COLUMN trading.fm_theses.as_of IS
    '判断の知識時点。evidence_refs はこの時点以前のものに限る(point-in-time)。';

COMMENT ON COLUMN trading.orders.thesis_id IS
    '注文の論拠(trading.fm_theses)。FM 経路は必須、それ以外の経路(例外取引等)は NULL。';

COMMENT ON FUNCTION trading.forbid_truncate IS
    'TRUNCATE は行トリガを迂回するため文トリガで封鎖する(0015 と同基準)。'
    'trading の追記オンリー表と trading.orders(発注証跡)に適用。';
