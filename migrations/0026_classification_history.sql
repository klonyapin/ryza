-- 0026_classification_history.sql
-- 銘柄分類の point-in-time 履歴化(独立役員審査 T-017 C-4 の是正 — E6 の前提)。
--
-- 採番の注意(設計リード宛): 本ファイルは 0025 までを既定として 0026 を仮置きする。
-- 統合時に番号が衝突する場合は再採番してよい(内容は番号に依存しない)。ただし
-- **ファイル名の `classification_history` は変えないこと** — アプリ層
-- (`src/ryza/risk/classify.py` の ``history_coverage_since``)は
-- `meta.schema_migrations.filename` をこの語で引いて「履歴の記録開始時点」を決める。
-- 名前を変えるとカバレッジが不明になり、E6 の但し書きが恒久的に外れなくなる
-- (安全側に倒れるが、テスト `tests/risk/test_classify.py` が検出する)。
--
-- 問題(審査 C-4): `market.instrument_classification`(0015)は instrument_id 主キーの
-- **上書き型**で、分類の履歴を持たない。過去 as_of のリプレイでは「当時の分類」を
-- 再現できず、
--   (1) 最新分類の as_of が判断時点より新しいとユニバースが**静かに**空になる
--   (2) 分類が後から付いた銘柄を過去に遡って候補にできる(look-ahead — 不変原則4違反)
-- のいずれかが起きる。**この状態では E6(point-in-time ユニバース)の達成を主張できない**。
--
-- 設計上の判断:
--   1. **SCD2 化ではなく追記オンリー履歴表+現在値キャッシュ**にする。SCD2
--      (instrument_id, valid_from / valid_to)は区間を閉じるために既存行の UPDATE を
--      要し、0005 以降この projectが標準としてきた「追記オンリー(UPDATE/DELETE 禁止の
--      トリガ+REVOKE)」を分類だけ緩めることになる。履歴は本表に追記し、0015 の
--      現行表は**現在値キャッシュ**(ゲートの高速経路)として維持する。両者は
--      `src/ryza/risk/classify.py` の同一トランザクションで更新される
--   2. **列は分類の全内容を持つ**。instrument_id / as_of / run_id だけでは過去時点の
--      `OrderProposal` を組み直せない(product・unit_size・flags もゲート入力)。
--      現在値表と同じ列を持たせ、履歴行だけで当時の分類を完全に再構成できるようにする
--   3. **run_id は NOT NULL + FK**(0013 が定める「全表 run_id 必須」— 審査 C-15 が
--      0023 で指摘した自前基準の未達を繰り返さない)。バックフィル行は現在値表の
--      run_id を引き継ぐため制約を満たす
--   4. **既存行はバックフィルする**が、それで過去が復元できたと主張はしない。
--      移行前の改訂履歴は物理的に存在しないため、**カバレッジ開始時点 = 本 migration の
--      applied_at** とし、それより前の as_of のリプレイには「E6 未達」の但し書きを
--      アプリ層が自動で付ける(`risk.classify.classification_pit_status`)。
--      バックフィル行は `backfilled = true` で識別できる(再構成 vs 実時刻の記録)
--   5. **同一 (instrument_id, as_of) の重複は許す**(UNIQUE を張らない)。同じ知識時点に
--      対する後日の訂正は正当な追記であり、読出しは `as_of DESC, history_id DESC` で
--      決定論に解決する(後に書かれた行が勝つ — market.bars の as_of と同じ流儀)
--
-- 保護領域(定款第5条): スキーマ変更のため独立役員審査+みなし承認手続の対象。

-- ────────────────────────────────────────────────────────────────────────────
-- market.instrument_classification_history: 分類の追記オンリー履歴(PIT の正)
-- ────────────────────────────────────────────────────────────────────────────
-- instrument_id は market.instruments(SCD2)のため FK なし(0004・0014・0015 と同じ)。
CREATE TABLE market.instrument_classification_history (
    history_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    instrument_id    bigint NOT NULL,
    universe_tags    text[] NOT NULL,        -- マンデート universe 語彙(空=どこにも属さない)
    instrument_flags text[] NOT NULL,        -- prohibitions.instruments 語彙(空=該当なし)
    is_single_name   boolean,                -- NULL=分類不能(依存規則は fail-closed)
    product          text NOT NULL,          -- ips.products 語彙
    unit_size        numeric,                -- 日本個別株の1単元株数
    source           text NOT NULL,          -- rules:<version> | curated
    as_of            timestamptz NOT NULL,   -- **この分類が有効になった知識時点**
    run_id           bigint NOT NULL REFERENCES meta.runs (run_id),
    backfilled       boolean NOT NULL DEFAULT false,  -- 本 migration による再構成行
    created_at       timestamptz NOT NULL DEFAULT now()  -- 実際に記録された時刻
);

-- 「as_of 時点で最新の分類」を銘柄ごとに引く経路(DISTINCT ON)。
CREATE INDEX instrument_classification_history_pit_idx
    ON market.instrument_classification_history (instrument_id, as_of DESC, history_id DESC);
-- universe_tags の GIN は張らない: タグ照合は「as_of 時点で最新の行」を選んだ**後**に
-- 効かせる(先に絞ると、タグが後から付いた銘柄を過去に混ぜる = look-ahead になる)。

-- ────────────────────────────────────────────────────────────────────────────
-- 追記オンリーの強制(0005 / 0014 / 0018 / 0023 と同基準)
-- ────────────────────────────────────────────────────────────────────────────
-- market スキーマにはまだ mutation ガードが無いため、ここで定義する。
CREATE FUNCTION market.forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        '% は追記オンリー(UPDATE/DELETE 禁止)。分類の訂正は新しい行の追記で行う',
        TG_TABLE_NAME;
END;
$$;

-- TRUNCATE は行トリガを迂回する(0015 の審査で実証済み)。文トリガで塞ぐ。
CREATE FUNCTION market.forbid_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        '% の TRUNCATE は禁止(point-in-time 分類の証跡)。訂正は追記で行う',
        TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER instrument_classification_history_no_mutation
    BEFORE UPDATE OR DELETE ON market.instrument_classification_history
    FOR EACH ROW EXECUTE FUNCTION market.forbid_mutation();

CREATE TRIGGER instrument_classification_history_no_truncate
    BEFORE TRUNCATE ON market.instrument_classification_history
    FOR EACH STATEMENT EXECUTE FUNCTION market.forbid_truncate();

REVOKE UPDATE, DELETE, TRUNCATE ON market.instrument_classification_history FROM PUBLIC;

-- ────────────────────────────────────────────────────────────────────────────
-- 既存の現在値行をバックフィル(判断4)
-- ────────────────────────────────────────────────────────────────────────────
-- 移行前の改訂は復元できない。ここで写せるのは「移行時点の現在値」だけであり、
-- それを過去に適用すると look-ahead になり得る。したがって as_of < 本 migration の
-- applied_at のリプレイは E6 未達として表示し続ける(アプリ層が自動判定)。
INSERT INTO market.instrument_classification_history
    (instrument_id, universe_tags, instrument_flags, is_single_name, product,
     unit_size, source, as_of, run_id, backfilled)
SELECT instrument_id, universe_tags, instrument_flags, is_single_name, product,
       unit_size, source, as_of, run_id, true
FROM market.instrument_classification;

-- ────────────────────────────────────────────────────────────────────────────
-- データカタログ用コメント
-- ────────────────────────────────────────────────────────────────────────────
COMMENT ON TABLE market.instrument_classification_history IS
    '銘柄分類の追記オンリー履歴(point-in-time の正 — E6)。'
    'リプレイ・バックテストは本表から「as_of 時点で最新の分類」を引く。'
    'market.instrument_classification は同内容の現在値キャッシュ(通常運転の高速経路)。';
COMMENT ON COLUMN market.instrument_classification_history.as_of IS
    'この分類が有効になった知識時点。読出しは as_of <= 判断時点 の最新行(同着は history_id 降順)。';
COMMENT ON COLUMN market.instrument_classification_history.backfilled IS
    'true=0026 が現在値表から再構成した行(移行前の改訂履歴は存在しない)。'
    'false=分類確定と同時に記録された行。E6 を主張できるのは本 migration の '
    'applied_at 以降の as_of のみ。';
COMMENT ON COLUMN market.instrument_classification_history.created_at IS
    '実際に記録された時刻。backfilled 行では as_of より大きく乖離する。';
COMMENT ON COLUMN market.instrument_classification_history.run_id IS
    '分類を書いたジョブ実行(0013 の全表 run_id 必須)。バックフィル行は現在値表から継承。';

COMMENT ON FUNCTION market.forbid_mutation IS
    'market の追記オンリー表の UPDATE・DELETE を禁止(0005 ledger.forbid_mutation と同型)。';
COMMENT ON FUNCTION market.forbid_truncate IS
    'TRUNCATE は行トリガを迂回するため文トリガで封鎖する(0015 / 0018 と同基準)。';

COMMENT ON TABLE market.instrument_classification IS
    '銘柄分類の**現在値キャッシュ**(ゲート G-1/G-2 入力の高速経路)。'
    '行なし=未取得(fail-closed)、配列空=取得済み該当なし。生成は決定論コードのみ。'
    'point-in-time の正は market.instrument_classification_history(0026)。';
