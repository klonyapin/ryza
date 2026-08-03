-- 0028_classification_asset_class.sql
-- 銘柄分類に IPS §8.1 資産クラス列を追加する(reminder: fm-asset-class-taxonomy-column)。
--
-- 採番の注意(0026 と同じ): **再採番は初回適用前に限る**。ランナー(`src/ryza/db/migrate.py`)は
-- ファイル名先頭 4 桁で適用済みを判定するため、適用済み環境で番号を変えると別 migration と
-- 見なされ ALTER TABLE が二重適用されて失敗する。
--
-- 問題: ゲート(`src/ryza/gate/compliance.py` G-4/G-5/G-F)が要求する `asset_class`
-- (equity_jp / equity_us 等 — IPS §8.1)は 0015 / 0026 の分類表に列が無く、
-- `src/ryza/fm/base.py` の `ips_asset_class`(銘柄マスタの asset_class×venue の対応表)が
-- 読出しのたびに導出していた。分類の正が **market.instrument_classification** と
-- **fm/base.py の対応表**の2箇所に分かれており、
--   (1) FM 側の対応表を変えるだけでゲート入力の資産クラスが変わる(分類の変更が
--       決定論分類ジョブ・その履歴・承認手続を通らない)
--   (2) 過去 as_of のリプレイで「当時の資産クラス」を再現できない(0026 が universe_tags 等に
--       ついて是正した look-ahead が、資産クラスだけ残っていた)
-- という2つの穴があった。列を分類表に持たせ、`src/ryza/risk/classify.py` が決定論で埋め、
-- FM は**読むだけ**にする。
--
-- 設計上の判断:
--   1. **NULL 許容**(NOT NULL にしない)。ルールで確定できない銘柄(ETF・先物・債券等 —
--      0026 と同じ理由)は NULL であり、これは「分類不能」を意味する。読出し側は NULL の
--      候補を落とす(fail-closed)。NOT NULL にすると curated 供給まで分類行そのものを
--      作れなくなり、universe_tags だけ確定している銘柄を表せない
--   2. **語彙は CHECK で固定**(IPS §8.1 の9クラス)。curated 供給は人手入力の口であり、
--      typo(`equity_JP`)はゲート G-F の「タクソノミーに無い」block として実行時に初めて
--      露見する。書込時に落とす方が原因に近い。config/ips.yaml との一致は
--      `tests/risk/test_classify.py` が三者(DB CHECK・classify.py の定数・config/ips.yaml)
--      比較で固定する(規約ではなくテストで決着 — 議論規約4)
--   3. **履歴表(0026)にも同じ列を持たせる**。0026 の判断2「履歴行だけで当時の分類を完全に
--      再構成できる」を維持する。資産クラスだけ現在値表にしか無ければ、リプレイは結局
--      現在の対応表を使うことになり look-ahead が残る
--   4. **バックフィルは現行の導出ロジック(fm/base.ips_asset_class)を SQL で再現**する。
--      履歴行は **その行の as_of 時点で有効だった銘柄マスタ(SCD2)の版**から導出する —
--      読出し側が今まで行っていたこと(`i.valid_from <= as_of < i.valid_to` の版に対して
--      対応表を引く)と同じ結果にするためであり、それ以上の復元は主張しない。
--      その as_of に有効な版が無い行は NULL のまま(fail-closed)
--   5. **履歴表の追記オンリー・トリガをこの migration の中だけ外す**。0026 は
--      「過去の分類を真に取り込む必要が生じたときは、トリガを明示的に外す監査つき
--      migration で行う」と定めており、本 migration がその手続きである。
--   6. **再構成した行はデータで識別できるようにする**(審査 C-23)。0026 は再構成行を
--      `backfilled=true` で識別可能にしたが、本 migration は**通常運転で記録された履歴行
--      (`backfilled=false`)の asset_class も現行ルールで再構成して書き込む**ため、
--      その値が「当時そう分類していた」のか「0028 が再構成した」のかを行から区別できない。
--      `asset_class_backfill_note` にマーカーを同時に書き、区別をコメント(規約)ではなく
--      データに置く。適用件数は本 migration 末尾で RAISE NOTICE すると同時に、
--      `SELECT count(*) WHERE asset_class_backfill_note IS NOT NULL` でいつでも再導出できる
--
-- ── 追記オンリー・トリガの一時解除の前例としての条件(審査 C-22)────────────────
-- 本 migration は「追記オンリー表のガードを外して既存行を書き換える」最初の例であり、
-- 以後これが複製される。複製してよいのは次の4条件を**すべて**満たす場合に限る:
--
--   (1) **名前指定**: `DISABLE TRIGGER USER` で表の全ユーザトリガを落とさない。解除は
--       必要な1本(ここでは `..._no_mutation`)を名前で指定する。`USER` 指定は
--       no_truncate と stamp_recorded_at まで落とし、「SET は派生列だけ」という主張を
--       SQL の字面から検証できなくする
--   (2) **自己検査**: 末尾に「当該表の全トリガが tgenabled='O'」を検査する DO ブロックを
--       置き、違反なら EXCEPTION で migration 自体を失敗させる。ランナー
--       (`src/ryza/db/migrate.py` は1ファイル=1トランザクション)なら途中失敗でも
--       巻き戻るが、`psql -f` 相当(文ごと autocommit)では解除が 'D' のまま残留する
--       (審査 C-22 の実測)。経路に依存しない担保が要る
--   (3) **対象の限定**: UPDATE の SET は本 migration が追加した列のみ、WHERE はその列が
--       未設定の行のみ。既存列の値は1つも変えない
--   (4) **件数記録**: 何行を再構成したかを RAISE NOTICE で残し、かつ後からデータで
--       再導出できる形(マーカー列)にする
--
-- 保護領域(定款第5条): スキーマ変更のため独立役員審査+みなし承認手続の対象。

-- ────────────────────────────────────────────────────────────────────────────
-- 列の追加(現在値キャッシュ・追記オンリー履歴の双方)
-- ────────────────────────────────────────────────────────────────────────────
ALTER TABLE market.instrument_classification
    ADD COLUMN asset_class text;
ALTER TABLE market.instrument_classification_history
    ADD COLUMN asset_class text;

-- 再構成の痕跡(判断6)。NULL = 分類確定と同時に記録された値、非 NULL = 0028 が現行
-- ルールで再構成した値。履歴表は追記オンリーで行が後から変わらないため、このマーカーは
-- 恒久的に正しい。現在値キャッシュには置かない — 次回の分類で上書きされる表であり、
-- 監査の記録は履歴表が持つ(マーカーを置くと上書き後に陳腐化して嘘になる)。
ALTER TABLE market.instrument_classification_history
    ADD COLUMN asset_class_backfill_note text;

-- IPS §8.1 のタクソノミー(config/ips.yaml asset_class_taxonomy.classes と同一集合)。
-- NULL は「分類不能」であって語彙違反ではないため許す(判断1)。
ALTER TABLE market.instrument_classification
    ADD CONSTRAINT instrument_classification_asset_class_vocab CHECK (
        asset_class IS NULL OR asset_class IN (
            'equity_jp', 'equity_us', 'equity_other', 'bond', 'fx',
            'crypto', 'commodity_futures', 'rates', 'cash'
        )
    );
ALTER TABLE market.instrument_classification_history
    ADD CONSTRAINT classification_history_asset_class_vocab CHECK (
        asset_class IS NULL OR asset_class IN (
            'equity_jp', 'equity_us', 'equity_other', 'bond', 'fx',
            'crypto', 'commodity_futures', 'rates', 'cash'
        )
    );

-- ────────────────────────────────────────────────────────────────────────────
-- バックフィル(判断4)— 現行の導出ロジックを SQL で再現する
-- ────────────────────────────────────────────────────────────────────────────
-- 対応表の内容は `src/ryza/fm/base.py` の `ips_asset_class` と同一:
--   equity × TSE                → equity_jp
--   equity × (NYSE | NASDAQ)    → equity_us
--   fx × 任意                   → fx
--   それ以外                    → NULL(分類不能 = 候補から落ちる)
-- 移行後、`ips_asset_class` は削除する(分類の正を1箇所にする — 本 migration の目的)。
--
-- 下の 2 つのマーカーに挟まれた区間は、`tests/risk/test_classify.py` が**実物として
-- 抽出して実行**する(導出ロジックの回帰。SCD2 で venue が変わった銘柄を含む)。
-- マーカーを消す・区間の外にバックフィルを書くとテストが空振りするので動かさないこと。
-- >>> BACKFILL BEGIN

-- 現在値キャッシュ: 現行版(valid_to IS NULL)の銘柄マスタから導出する。
UPDATE market.instrument_classification c
SET asset_class = d.ips_class
FROM (
    SELECT DISTINCT ON (i.instrument_id)
           i.instrument_id,
           CASE
               WHEN i.asset_class = 'equity' AND i.venue = 'TSE' THEN 'equity_jp'
               WHEN i.asset_class = 'equity' AND i.venue IN ('NYSE', 'NASDAQ')
                   THEN 'equity_us'
               WHEN i.asset_class = 'fx' THEN 'fx'
           END AS ips_class
    FROM market.instruments i
    WHERE i.valid_to IS NULL
    ORDER BY i.instrument_id, i.valid_from DESC
) d
WHERE d.instrument_id = c.instrument_id
  AND d.ips_class IS NOT NULL
  AND c.asset_class IS NULL;

-- 履歴表: 追記オンリー・トリガを**この migration の中だけ**外す(判断5)。
-- 解除は **UPDATE を拒むトリガ1本のみ**を名前で指定する(前例条件1)。
-- TRUNCATE 封鎖と created_at の固定は落とさない。
ALTER TABLE market.instrument_classification_history
    DISABLE TRIGGER instrument_classification_history_no_mutation;

DO $backfill$
DECLARE
    reconstructed bigint;
BEGIN
    UPDATE market.instrument_classification_history h
    SET asset_class = (
            SELECT CASE
                       WHEN i.asset_class = 'equity' AND i.venue = 'TSE' THEN 'equity_jp'
                       WHEN i.asset_class = 'equity' AND i.venue IN ('NYSE', 'NASDAQ')
                           THEN 'equity_us'
                       WHEN i.asset_class = 'fx' THEN 'fx'
                   END
            FROM market.instruments i
            WHERE i.instrument_id = h.instrument_id
              AND i.valid_from <= h.as_of
              AND (i.valid_to IS NULL OR i.valid_to > h.as_of)
            ORDER BY i.valid_from DESC
            LIMIT 1
        ),
        -- 再構成の痕跡(前例条件4・審査 C-23)。値が入らなかった行にも印を残すのは、
        -- 「NULL のまま = 0028 が見て分類できなかった」と「0028 以後に書かれた行」を
        -- 区別するため。
        asset_class_backfill_note =
            'asset_class は 0028 が現行ルール(equity×TSE→equity_jp / '
            '×NYSE,NASDAQ→equity_us / fx→fx)で再構成(2026-08-04)。'
            '当時記録された値ではない'
    WHERE h.asset_class IS NULL;
    GET DIAGNOSTICS reconstructed = ROW_COUNT;
    RAISE NOTICE '0028: 履歴行 % 件の asset_class を再構成しました'
        '(内訳は SELECT asset_class, count(*) ... WHERE asset_class_backfill_note IS NOT NULL)',
        reconstructed;
END
$backfill$;

ALTER TABLE market.instrument_classification_history
    ENABLE TRIGGER instrument_classification_history_no_mutation;

-- ────────────────────────────────────────────────────────────────────────────
-- 自己検査(前例条件2・審査 C-22)
-- ────────────────────────────────────────────────────────────────────────────
-- 当該表のユーザトリガが1本でも無効('D'/'R'/'A')なら migration を失敗させる。
-- ランナー経路(1ファイル=1トランザクション)では途中失敗も巻き戻るが、psql の
-- 文ごと autocommit では解除が残留し得る(審査 C-22 の実測)。経路に依存せず
-- 「解除したまま終わる」ことを不可能にするのがこの検査の役割である。
DO $guard$
DECLARE
    disabled text;
BEGIN
    SELECT string_agg(tgname, ', ' ORDER BY tgname) INTO disabled
    FROM pg_trigger
    WHERE tgrelid = 'market.instrument_classification_history'::regclass
      AND NOT tgisinternal
      AND tgenabled <> 'O';
    IF disabled IS NOT NULL THEN
        RAISE EXCEPTION
            '0028: 追記オンリー・ガードが無効のまま残っています: %。'
            '一時解除は同一 migration 内で必ず ENABLE に戻すこと', disabled;
    END IF;
END
$guard$;
-- <<< BACKFILL END

-- ────────────────────────────────────────────────────────────────────────────
-- データカタログ用コメント
-- ────────────────────────────────────────────────────────────────────────────
COMMENT ON COLUMN market.instrument_classification.asset_class IS
    'IPS §8.1 資産クラス(ゲート G-4/G-5/G-F の入力)。NULL=分類不能で候補から落ちる '
    '(fail-closed)。書くのは src/ryza/risk/classify.py のみ・FM は読むだけ。';
COMMENT ON COLUMN market.instrument_classification_history.asset_class IS
    'IPS §8.1 資産クラスの point-in-time 値。0028 のバックフィル行は「その as_of 時点で '
    '有効だった銘柄マスタの版」から現行の導出ルールで再構成したもので、'
    '移行前の資産クラス改訂そのものは復元していない(E6 の主張範囲は 0026 と同じ)。';
COMMENT ON COLUMN market.instrument_classification_history.asset_class_backfill_note IS
    'NULL=分類確定と同時に記録された asset_class。非 NULL=0028 が現行ルールで再構成した値'
    '(当時の値ではない — 審査 C-23)。0026 の backfilled と違い、通常運転で記録された行'
    '(backfilled=false)にも付き得る。再構成件数はこの列から再導出できる。';
COMMENT ON CONSTRAINT instrument_classification_asset_class_vocab
    ON market.instrument_classification IS
    'IPS §8.1 タクソノミー(config/ips.yaml asset_class_taxonomy.classes)の語彙固定。'
    'curated 供給の typo をゲート実行時ではなく書込時に落とす。';
COMMENT ON CONSTRAINT classification_history_asset_class_vocab
    ON market.instrument_classification_history IS
    'IPS §8.1 タクソノミーの語彙固定(現在値表と同一集合)。';
