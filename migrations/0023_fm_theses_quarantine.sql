-- 0023_fm_theses_quarantine.sql
-- FM 提案の検疫(独立役員審査 T-017 C-3 の是正)。
--
-- 採番の注意(設計リード宛): 本ファイルは 0021 / 0022 が並行ブランチで開発中の状態で
-- 書かれている。統合時に番号が衝突する場合は設計リードの指示で再採番する(内容は
-- 番号に依存しない — 新設テーブル1つと関数1つのみ)。
--
-- 問題: `src/ryza/fm/ben.py` の着任プロンプトは trading.fm_theses の thesis_md /
-- invalidation_md を再注入する。外部文書(docs.documents)経由で注入された指示文が
-- 提案テキストに混入すると、fm_theses は**追記オンリー**のため撤去できず、注入は
-- 直近 N 件のウィンドウが流れるまで(低回転の Ben で最大10週間)プロンプトに残る。
--
-- 是正の二本立て:
--   (a) 構文と system 指示による境界化(`src/ryza/research/prompting.py` のフェンス)
--   (b) **本テーブル** — 汚染が判明した thesis を再注入の対象から外す
--
-- 設計上の判断:
--   1. **追記で表現する**。fm_theses 自体は書き換えない(判断の履歴は不変)。検疫は
--      別表への追記であり、governance.stances の retraction(0013)と同型の考え方を
--      「参照する側が除外する」形で実装する。読出し側は
--      `src/ryza/fm/theses.py` の recent_theses / open_theses_by_instrument が除外する
--   2. **検疫表自身も追記オンリー**(UPDATE/DELETE/TRUNCATE 禁止)。検疫の解除行を
--      設けないのは意図的である — 「汚染済み」の判定を後から消せる経路を作ると、
--      DB 権限を得た攻撃者が汚染 thesis を再びプロンプトへ戻せる(fail-closed)。
--      誤検疫の救済は、同じ内容を**新しい thesis として改めて記録する**ことで行う
--   3. **登録は当面手動**(SQL または `ryza.fm.theses.quarantine_thesis`)。自動検出
--      (命令形の検出など)は誤検知で判断履歴を静かに欠落させるため、人手の判断を
--      経路に残す。将来自動化するときは検出器の出力を reason に残す
--   4. **thesis_id は FK**。存在しない提案の検疫行は作らせない(証跡の完全性)
--
-- 保護領域(定款第5条): スキーマ変更のため独立役員審査+みなし承認手続の対象。

CREATE TABLE trading.fm_theses_quarantine (
    quarantine_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    thesis_id     bigint NOT NULL REFERENCES trading.fm_theses (thesis_id),
    reason        text NOT NULL CHECK (length(btrim(reason)) > 0),
    quarantined_by text NOT NULL CHECK (length(btrim(quarantined_by)) > 0),
    run_id        bigint REFERENCES meta.runs (run_id),  -- 手動 SQL 登録では NULL
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- 同じ thesis を二重に検疫しても意味は変わらないが、証跡としては1行で足りる。
CREATE UNIQUE INDEX fm_theses_quarantine_thesis_idx
    ON trading.fm_theses_quarantine (thesis_id);

CREATE TRIGGER fm_theses_quarantine_no_mutation
    BEFORE UPDATE OR DELETE ON trading.fm_theses_quarantine
    FOR EACH ROW EXECUTE FUNCTION trading.forbid_mutation();

-- TRUNCATE は行トリガを迂回する(0015・0018 と同基準)。
CREATE TRIGGER fm_theses_quarantine_no_truncate
    BEFORE TRUNCATE ON trading.fm_theses_quarantine
    FOR EACH STATEMENT EXECUTE FUNCTION trading.forbid_truncate();

REVOKE UPDATE, DELETE, TRUNCATE ON trading.fm_theses_quarantine FROM PUBLIC;

COMMENT ON TABLE trading.fm_theses_quarantine IS
    '検疫された FM 提案(追記オンリー)。ここに thesis_id がある提案は着任プロンプトへ'
    '再注入しない(プロンプト汚染の封じ込め — 独立役員審査 T-017 C-3)。'
    '解除行は設けない — 誤検疫の救済は新しい thesis の記録で行う。';
COMMENT ON COLUMN trading.fm_theses_quarantine.reason IS
    '検疫の理由(どの経路で汚染が混入したか)。空不可。';
COMMENT ON COLUMN trading.fm_theses_quarantine.quarantined_by IS
    '検疫の実施主体(人名・役職キー・ジョブ名)。空不可。';
COMMENT ON COLUMN trading.fm_theses_quarantine.run_id IS
    '検疫を記録した Run(手動 SQL では NULL)。';
