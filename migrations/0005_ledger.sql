-- 0005_ledger.sql
-- ledger スキーマ: 3帳簿・証憑・照合・予算。設計書 §5 に完全準拠。
-- 会計エンジンのみ書き込み（他は SELECT）。
--
-- 最重要の整合性制約（受け入れ基準・テスト対象）:
--   1. journal_entries.evidence_id は NOT NULL（証憑必須）
--   2. 仕訳単位で Σdebit = Σcredit を CONSTRAINT TRIGGER（DEFERRABLE INITIALLY DEFERRED）で強制
--   3. journal_lines.book_id が親 entry の book_id と不一致なら挿入拒否（帳簿混合の物理的禁止）
--   4. journal_entries / journal_lines への UPDATE・DELETE を禁止（REVOKE + トリガ）
--      訂正は reversal_of による逆仕訳のみ

CREATE SCHEMA IF NOT EXISTS ledger;

CREATE TABLE ledger.books (
    book_id       text PRIMARY KEY,           -- 'DEMO_FUND' | 'LIVE_FUND' | 'OPS'
    book_type     text NOT NULL CHECK (book_type IN ('fund','ops')),
    base_ccy      text NOT NULL,
    is_real_money boolean NOT NULL            -- DEMO_FUND=false, LIVE_FUND/OPS=true
);

CREATE TABLE ledger.accounts (
    account_id text NOT NULL,                 -- 'cash', 'securities_equity', 'margin_deposit', ...
    book_id    text NOT NULL REFERENCES ledger.books,
    name       text NOT NULL,
    category   text NOT NULL CHECK (category IN
               ('asset','liability','equity','income','expense')),
    PRIMARY KEY (book_id, account_id)
);

CREATE TABLE ledger.evidence (
    evidence_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind         text NOT NULL,     -- broker_fill|broker_statement|gcp_billing|llm_usage|invoice|price_snapshot
    payload_ref  text NOT NULL,     -- 証憑ストア（GCS）URI または内部参照
    sha256       bytea NOT NULL,
    source       text NOT NULL,
    retrieved_at timestamptz NOT NULL
);

CREATE TABLE ledger.journal_entries (
    entry_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id     text NOT NULL REFERENCES ledger.books,
    entry_date  date NOT NULL,                -- 約定日ベース
    description text NOT NULL,
    evidence_id bigint NOT NULL REFERENCES ledger.evidence,  -- ★証憑必須（NOT NULL）
    posted_by   text NOT NULL,                -- 生成ジョブ名
    reversal_of bigint REFERENCES ledger.journal_entries(entry_id),  -- 訂正は逆仕訳
    run_id      bigint NOT NULL
);

CREATE TABLE ledger.journal_lines (
    entry_id      bigint NOT NULL REFERENCES ledger.journal_entries,
    line_no       int NOT NULL,
    book_id       text NOT NULL,              -- entry と一致（トリガで強制=帳簿混合の物理的禁止）
    account_id    text NOT NULL,
    debit         numeric NOT NULL DEFAULT 0,
    credit        numeric NOT NULL DEFAULT 0,
    currency      text NOT NULL,
    instrument_id bigint,                     -- ファンド帳簿のみ
    strategy_tag  text,                       -- E4 配賦用（OPS 帳簿の費用行に必須）
    dept_tag      text,                       -- 部門別コスト集計用
    PRIMARY KEY (entry_id, line_no),
    FOREIGN KEY (book_id, account_id) REFERENCES ledger.accounts
);

CREATE TABLE ledger.nav_snapshots (
    book_id   text NOT NULL, snap_date date NOT NULL,
    nav       numeric NOT NULL,
    status    text NOT NULL CHECK (status IN ('provisional','confirmed')),
    detail    jsonb NOT NULL,                 -- 資産構成・評価根拠（price_snapshot evidence）
    PRIMARY KEY (book_id, snap_date)
);

CREATE TABLE ledger.reconciliations (
    recon_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id     text NOT NULL, recon_date date NOT NULL,
    broker      text NOT NULL,
    item        text NOT NULL,                -- cash|position:<instrument>|valuation
    ours        numeric NOT NULL, theirs numeric NOT NULL,
    status      text NOT NULL CHECK (status IN ('matched','break_open','break_resolved')),
    resolution  text,                         -- ブレイク解消の説明（監査 A-2 対象）
    evidence_id bigint NOT NULL REFERENCES ledger.evidence
);

CREATE TABLE ledger.budgets (
    budget_month date NOT NULL,
    book_id      text NOT NULL DEFAULT 'OPS',
    category     text NOT NULL,               -- gcp|llm_fable|llm_mid|llm_light|data|other
    amount       numeric NOT NULL,
    basis        text NOT NULL,               -- 見積根拠
    approved_by  text, approved_at timestamptz,   -- Discord 承認の記録
    PRIMARY KEY (budget_month, book_id, category)
);

-- ────────────────────────────────────────────────────────────────────────────
-- 整合性制約（トリガ）
-- ────────────────────────────────────────────────────────────────────────────

-- (3) journal_lines.book_id は親 entry の book_id と一致必須。不一致は挿入時に拒否。
CREATE FUNCTION ledger.check_line_book() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    parent_book text;
BEGIN
    SELECT book_id INTO parent_book
      FROM ledger.journal_entries
     WHERE entry_id = NEW.entry_id;
    IF parent_book IS NULL THEN
        RAISE EXCEPTION 'journal_lines.entry_id % に対応する仕訳が存在しない', NEW.entry_id;
    END IF;
    IF NEW.book_id <> parent_book THEN
        RAISE EXCEPTION
            '帳簿混合の禁止: line.book_id=% は entry.book_id=% と不一致',
            NEW.book_id, parent_book;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER journal_lines_book_match
    BEFORE INSERT ON ledger.journal_lines
    FOR EACH ROW EXECUTE FUNCTION ledger.check_line_book();

-- (2) 仕訳単位で Σdebit = Σcredit。DEFERRABLE INITIALLY DEFERRED でコミット時に検証。
CREATE FUNCTION ledger.check_entry_balance() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    total_debit  numeric;
    total_credit numeric;
    target_entry bigint;
BEGIN
    target_entry := COALESCE(NEW.entry_id, OLD.entry_id);
    SELECT COALESCE(sum(debit), 0), COALESCE(sum(credit), 0)
      INTO total_debit, total_credit
      FROM ledger.journal_lines
     WHERE entry_id = target_entry;
    IF total_debit <> total_credit THEN
        RAISE EXCEPTION
            '貸借不一致: 仕訳 % は debit=% credit=%', target_entry, total_debit, total_credit;
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER journal_lines_balanced
    AFTER INSERT ON ledger.journal_lines
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger.check_entry_balance();

-- (4) journal_entries / journal_lines への UPDATE・DELETE を禁止（追記オンリー）。
--     訂正は reversal_of による逆仕訳のみ。REVOKE に加えテーブル所有者にも効くトリガで強制。
CREATE FUNCTION ledger.forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        '% は % では禁止（追記オンリー）。訂正は reversal_of による逆仕訳のみ',
        TG_OP, TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER journal_entries_no_mutation
    BEFORE UPDATE OR DELETE ON ledger.journal_entries
    FOR EACH ROW EXECUTE FUNCTION ledger.forbid_mutation();

CREATE TRIGGER journal_lines_no_mutation
    BEFORE UPDATE OR DELETE ON ledger.journal_lines
    FOR EACH ROW EXECUTE FUNCTION ledger.forbid_mutation();

REVOKE UPDATE, DELETE ON ledger.journal_entries FROM PUBLIC;
REVOKE UPDATE, DELETE ON ledger.journal_lines FROM PUBLIC;

-- ────────────────────────────────────────────────────────────────────────────
-- データカタログの源泉となるコメント
-- ────────────────────────────────────────────────────────────────────────────
COMMENT ON SCHEMA ledger IS '3帳簿・証憑・照合・予算。会計エンジンのみ書き込み（他は SELECT）。';

COMMENT ON TABLE ledger.books IS '帳簿マスタ（DEMO_FUND / LIVE_FUND / OPS）。';
COMMENT ON COLUMN ledger.books.book_id IS '帳簿 ID。';
COMMENT ON COLUMN ledger.books.book_type IS 'fund|ops。';
COMMENT ON COLUMN ledger.books.base_ccy IS '基準通貨。';
COMMENT ON COLUMN ledger.books.is_real_money IS '実マネーか（DEMO_FUND=false）。';

COMMENT ON TABLE ledger.accounts IS '勘定科目表（帳簿別）。';
COMMENT ON COLUMN ledger.accounts.account_id IS '勘定科目 ID（cash, securities ...）。';
COMMENT ON COLUMN ledger.accounts.book_id IS '所属帳簿。';
COMMENT ON COLUMN ledger.accounts.name IS '科目名。';
COMMENT ON COLUMN ledger.accounts.category IS 'asset|liability|equity|income|expense。';

COMMENT ON TABLE ledger.evidence IS '証憑（GCS 証憑ストアへの参照とハッシュ）。';
COMMENT ON COLUMN ledger.evidence.evidence_id IS '証憑の一意 ID。';
COMMENT ON COLUMN ledger.evidence.kind IS 'broker_fill|broker_statement|gcp_billing|llm_usage|invoice|price_snapshot。';
COMMENT ON COLUMN ledger.evidence.payload_ref IS 'GCS URI または内部参照。';
COMMENT ON COLUMN ledger.evidence.sha256 IS '原本の SHA256（改竄検知、監査 A-1）。';
COMMENT ON COLUMN ledger.evidence.source IS '取得元。';
COMMENT ON COLUMN ledger.evidence.retrieved_at IS '取得時刻。';

COMMENT ON TABLE ledger.journal_entries IS '仕訳ヘッダ。証憑必須・追記オンリー。訂正は逆仕訳。';
COMMENT ON COLUMN ledger.journal_entries.entry_id IS '仕訳の一意 ID。';
COMMENT ON COLUMN ledger.journal_entries.book_id IS '帳簿 ID。';
COMMENT ON COLUMN ledger.journal_entries.entry_date IS '仕訳日（約定日ベース）。';
COMMENT ON COLUMN ledger.journal_entries.description IS '摘要。';
COMMENT ON COLUMN ledger.journal_entries.evidence_id IS '証憑 ID（NOT NULL＝証憑必須）。';
COMMENT ON COLUMN ledger.journal_entries.posted_by IS '記帳したジョブ名。';
COMMENT ON COLUMN ledger.journal_entries.reversal_of IS '逆仕訳の対象 entry_id（訂正）。';
COMMENT ON COLUMN ledger.journal_entries.run_id IS '生成ジョブ実行（リネージ）。';

COMMENT ON TABLE ledger.journal_lines IS '仕訳明細。book_id は親と一致必須、Σdebit=Σcredit。';
COMMENT ON COLUMN ledger.journal_lines.entry_id IS '親仕訳 ID。';
COMMENT ON COLUMN ledger.journal_lines.line_no IS '明細行番号。';
COMMENT ON COLUMN ledger.journal_lines.book_id IS '帳簿 ID（親 entry と一致必須）。';
COMMENT ON COLUMN ledger.journal_lines.account_id IS '勘定科目 ID。';
COMMENT ON COLUMN ledger.journal_lines.debit IS '借方金額。';
COMMENT ON COLUMN ledger.journal_lines.credit IS '貸方金額。';
COMMENT ON COLUMN ledger.journal_lines.currency IS '通貨。';
COMMENT ON COLUMN ledger.journal_lines.instrument_id IS '銘柄 ID（ファンド帳簿のみ）。';
COMMENT ON COLUMN ledger.journal_lines.strategy_tag IS 'E4 配賦用（OPS 費用行に必須）。';
COMMENT ON COLUMN ledger.journal_lines.dept_tag IS '部門別コスト集計用。';

COMMENT ON TABLE ledger.nav_snapshots IS 'NAV スナップショット（provisional/confirmed）。';
COMMENT ON COLUMN ledger.nav_snapshots.book_id IS '帳簿 ID。';
COMMENT ON COLUMN ledger.nav_snapshots.snap_date IS '評価日。';
COMMENT ON COLUMN ledger.nav_snapshots.nav IS 'NAV。';
COMMENT ON COLUMN ledger.nav_snapshots.status IS 'provisional|confirmed。';
COMMENT ON COLUMN ledger.nav_snapshots.detail IS '資産構成・評価根拠（price_snapshot 証憑）。';

COMMENT ON TABLE ledger.reconciliations IS 'ブローカー照合（ブレイク管理、監査 A-2）。';
COMMENT ON COLUMN ledger.reconciliations.recon_id IS '照合の一意 ID。';
COMMENT ON COLUMN ledger.reconciliations.book_id IS '帳簿 ID。';
COMMENT ON COLUMN ledger.reconciliations.recon_date IS '照合日。';
COMMENT ON COLUMN ledger.reconciliations.broker IS 'ブローカー。';
COMMENT ON COLUMN ledger.reconciliations.item IS 'cash|position:<instrument>|valuation。';
COMMENT ON COLUMN ledger.reconciliations.ours IS '当方値。';
COMMENT ON COLUMN ledger.reconciliations.theirs IS '相手方値。';
COMMENT ON COLUMN ledger.reconciliations.status IS 'matched|break_open|break_resolved。';
COMMENT ON COLUMN ledger.reconciliations.resolution IS 'ブレイク解消の説明（監査 A-2）。';
COMMENT ON COLUMN ledger.reconciliations.evidence_id IS '証憑 ID。';

COMMENT ON TABLE ledger.budgets IS '予算（OPS 帳簿、Discord 承認記録）。';
COMMENT ON COLUMN ledger.budgets.budget_month IS '予算対象月。';
COMMENT ON COLUMN ledger.budgets.book_id IS '帳簿 ID（既定 OPS）。';
COMMENT ON COLUMN ledger.budgets.category IS 'gcp|llm_fable|llm_mid|llm_light|data|other。';
COMMENT ON COLUMN ledger.budgets.amount IS '予算額。';
COMMENT ON COLUMN ledger.budgets.basis IS '見積根拠。';
COMMENT ON COLUMN ledger.budgets.approved_by IS '承認者（Discord）。';
COMMENT ON COLUMN ledger.budgets.approved_at IS '承認時刻。';

COMMENT ON FUNCTION ledger.check_line_book() IS '明細 book_id と親仕訳 book_id の一致を強制（帳簿混合禁止）。';
COMMENT ON FUNCTION ledger.check_entry_balance() IS '仕訳単位の貸借一致（Σdebit=Σcredit）をコミット時に検証。';
COMMENT ON FUNCTION ledger.forbid_mutation() IS 'journal_entries/lines の UPDATE・DELETE を禁止（追記オンリー）。';
