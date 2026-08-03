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
--      migration で行う」と定めており、本 migration がその手続きである。外すのは
--      **既存行に新しい派生列を埋める**ためだけで、既存列の値は1つも変えない
--      (UPDATE の SET は asset_class のみ・WHERE は asset_class IS NULL)。
--      同一トランザクション内で ENABLE に戻し、有効なままであることは
--      `tests/test_migrations.py` の追記オンリー回帰が別途固定する
--
-- 保護領域(定款第5条): スキーマ変更のため独立役員審査+みなし承認手続の対象。

-- ────────────────────────────────────────────────────────────────────────────
-- 列の追加(現在値キャッシュ・追記オンリー履歴の双方)
-- ────────────────────────────────────────────────────────────────────────────
ALTER TABLE market.instrument_classification
    ADD COLUMN asset_class text;
ALTER TABLE market.instrument_classification_history
    ADD COLUMN asset_class text;

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
-- 外している間に走るのは直下の UPDATE 1 文のみで、SET は追加した派生列に限る。
ALTER TABLE market.instrument_classification_history DISABLE TRIGGER USER;

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
)
WHERE h.asset_class IS NULL;

ALTER TABLE market.instrument_classification_history ENABLE TRIGGER USER;

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
COMMENT ON CONSTRAINT instrument_classification_asset_class_vocab
    ON market.instrument_classification IS
    'IPS §8.1 タクソノミー(config/ips.yaml asset_class_taxonomy.classes)の語彙固定。'
    'curated 供給の typo をゲート実行時ではなく書込時に落とす。';
COMMENT ON CONSTRAINT classification_history_asset_class_vocab
    ON market.instrument_classification_history IS
    'IPS §8.1 タクソノミーの語彙固定(現在値表と同一集合)。';
