-- 0035: ledger 追記オンリー表の TRUNCATE ガード + journal_lines 行レベル CHECK
--
-- 根拠: A-12 監査 F-4(Issue #119、裁定書 docs/reviews/a12/00-adjudication.md §3 L48
--       — pass1b-3 + pass1-3)。governance テーブルは 0021 で BEFORE TRUNCATE 文トリガ+
--       REVOKE TRUNCATE を得たが、ledger の追記オンリー表(journal_entries / journal_lines
--       — 0005 で `forbid_mutation` の行トリガが張られている 2 表)には TRUNCATE 防御が
--       無かった。行トリガ(BEFORE UPDATE OR DELETE)は TRUNCATE では発火しないのが
--       PostgreSQL の仕様であり、`TRUNCATE ledger.journal_entries CASCADE` の一撃で
--       仕訳と明細を全て消せた(監査 pass1b-3 の指摘)。0021 の流儀で塞ぐ。
--
--       あわせて `journal_lines` の「debit / credit 非負・同一行での両建て禁止」を
--       DB CHECK として置く。これまではアプリ層(src/ryza/ledger/posting.py L85-88)
--       だけの検証で、DB を直接叩く経路(psql / 別実装のジョブ)には防壁が無かった
--       (監査 pass1-3 の指摘)。
--
-- ── 追記オンリー標準(pass3b-1 の教訓 — 本 migration で確立する設計基準)──────
-- 追記オンリーを要求するテーブルは、**初回定義から**次の三点すべてを張る:
--   (1) 行トリガ `BEFORE UPDATE OR DELETE ... FOR EACH ROW`      … 通常経路の封鎖
--   (2) 文トリガ `BEFORE TRUNCATE       ... FOR EACH STATEMENT`   … TRUNCATE の封鎖
--   (3) `REVOKE UPDATE, DELETE, TRUNCATE ... FROM PUBLIC`         … 権限層の防波堤
-- 行トリガだけでは TRUNCATE を素通しし(PostgreSQL の仕様 — 文レベルのため行トリガは
-- 発火しない)、追記オンリーの主張は空手形になる。0005(ledger)・0013(governance の
-- minutes/stances)は (1) しか持たなかった穴を 0018 / 0021 が後追いで塞ぎ、今回の
-- 0035 は同じ穴を ledger 側で塞ぐ。**新しく追記オンリー表を作るときは (1)〜(3) を
-- 同じ migration に必ずまとめる**(0026 が market スキーマで初回定義から実践済み)。
--
-- ── REVOKE の位置づけ(A-12 pass4 所見2 の指摘)──────────────────────────────
-- `REVOKE ... FROM PUBLIC` はテーブル所有者ロール(現構成ではアプリと同一の ryza)
-- には効かず、実弾に至るまでロール分離を導入していない本プロジェクトでは実質 no-op
-- である。主防壁は文トリガ側(所有者にも効く)、REVOKE はロール分離後の統制と
-- ドキュメント上の意図表明として残す(0021 の流儀に同じ)。ロール分離は
-- ops/reminders.yaml の `governance-role-separation`(実弾移行前提条件)で扱う。
--
-- 冪等: 関数は CREATE OR REPLACE、トリガは CREATE OR REPLACE TRIGGER、CHECK 制約は
-- pg_constraint の存在チェック(DO ブロック — 0029・0030・0031 の流儀)。
-- 既存 migration は書き換えない(migrations は追記オンリーの保護領域 — 定款第5条)。

-- ════════════════════════════════════════════════════════════════════════════
-- 1. TRUNCATE ガード用の文トリガ関数(ledger スキーマ)
-- ════════════════════════════════════════════════════════════════════════════
-- 0021 の governance.forbid_truncate と同型。スキーマが違うため ledger 側で定義する
-- (関数はスキーマ修飾で名前解決される。ledger.forbid_mutation(0005)と対を成す)。
CREATE OR REPLACE FUNCTION ledger.forbid_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% の TRUNCATE は禁止(追記オンリーの仕訳証跡 — 訂正は reversal_of による逆仕訳のみ)',
        TG_TABLE_NAME;
END;
$$;

COMMENT ON FUNCTION ledger.forbid_truncate() IS
    'ledger 追記オンリー表の TRUNCATE を禁止する文トリガ関数(0035)。'
    '行トリガ(ledger.forbid_mutation)は TRUNCATE を素通しするため文トリガで塞ぐ'
    '(0021 governance.forbid_truncate と同型)。訂正は reversal_of による逆仕訳のみ。';

-- ════════════════════════════════════════════════════════════════════════════
-- 2. TRUNCATE ガードの適用(journal_entries / journal_lines)
-- ════════════════════════════════════════════════════════════════════════════
-- 対象は 0005 で `*_no_mutation` の行トリガが張られている 2 表に限る。
-- ledger.evidence / reconciliations は追記オンリー化されていないため対象外
-- (0005 は evidence / reconciliations に UPDATE / DELETE ガードを置いていない)。
CREATE OR REPLACE TRIGGER journal_entries_no_truncate
    BEFORE TRUNCATE ON ledger.journal_entries
    FOR EACH STATEMENT EXECUTE FUNCTION ledger.forbid_truncate();

CREATE OR REPLACE TRIGGER journal_lines_no_truncate
    BEFORE TRUNCATE ON ledger.journal_lines
    FOR EACH STATEMENT EXECUTE FUNCTION ledger.forbid_truncate();

REVOKE TRUNCATE ON ledger.journal_entries FROM PUBLIC;
REVOKE TRUNCATE ON ledger.journal_lines   FROM PUBLIC;

-- ════════════════════════════════════════════════════════════════════════════
-- 3. journal_lines 行レベル CHECK(非負・両建て禁止)
-- ════════════════════════════════════════════════════════════════════════════
-- アプリ層(src/ryza/ledger/posting.py の post_entry)が既にこの条件を強制しており、
-- 既存行はいずれも制約を満たす(監査 pass1b 検査4 で確認済み)。したがって NOT VALID は
-- 使わず、制約追加時に全行検証させる(違反があれば本 migration 自体が失敗して気付ける
-- — 0031 の runs_status_check と同じ判断)。
-- 制約名は既存流儀(0028 `classification_history_asset_class_vocab` /
-- 0026 `classification_history_as_of_not_future` の「表名 + 述語の説明」)に合わせる。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'journal_lines_amounts_check'
    ) THEN
        ALTER TABLE ledger.journal_lines ADD CONSTRAINT journal_lines_amounts_check
            CHECK (debit >= 0 AND credit >= 0 AND (debit = 0 OR credit = 0));
    END IF;
END
$$;

COMMENT ON CONSTRAINT journal_lines_amounts_check ON ledger.journal_lines IS
    '借方・貸方は非負、かつ同一行で両建て禁止(片側のみ非零)。'
    'アプリ層(posting.post_entry L85-88)と等価の検査を DB 側にも置く — DB を直接叩く'
    '経路(psql・別実装のジョブ)への防壁(A-12 pass1-3 / Issue #119)。';

-- ════════════════════════════════════════════════════════════════════════════
-- カタログ更新(stale なコメントを残さない — 0019 C-7 / 0021 の教訓)
-- ════════════════════════════════════════════════════════════════════════════
COMMENT ON TABLE ledger.journal_entries IS
    '仕訳ヘッダ。証憑必須・追記オンリー(UPDATE/DELETE は行トリガ・TRUNCATE は文トリガ '
    '0035 で禁止)。訂正は reversal_of による逆仕訳。';
COMMENT ON TABLE ledger.journal_lines IS
    '仕訳明細。book_id は親と一致必須、Σdebit=Σcredit(DEFERRED)。'
    '追記オンリー(UPDATE/DELETE 行トリガ+TRUNCATE 文トリガ 0035)。'
    '各行は debit / credit 非負かつ両建て禁止(journal_lines_amounts_check 0035)。';
