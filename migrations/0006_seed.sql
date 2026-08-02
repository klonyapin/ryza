-- 0006_seed.sql
-- 帳簿・勘定科目の初期データと DEMO_FUND の開始仕訳。
-- 設計書 §5「勘定科目表（初期セット）」および §9「決定事項（2026-08-02 投資委員会決定）」に準拠。
--
-- 冪等性: マイグレーションランナーが meta.schema_migrations で適用済みを記録するため、
-- このファイルは一度だけ適用される。防御的に books/accounts は ON CONFLICT DO NOTHING。

-- ── 帳簿（LIVE_FUND はまだ作らない） ──
INSERT INTO ledger.books (book_id, book_type, base_ccy, is_real_money) VALUES
    ('DEMO_FUND', 'fund', 'JPY', false),
    ('OPS',       'ops',  'JPY', true)
ON CONFLICT (book_id) DO NOTHING;

-- ── 勘定科目（ファンド帳簿 = DEMO_FUND） ──
-- 設計書 §5「ファンド帳簿（DEMO_FUND / LIVE_FUND 共通）」
INSERT INTO ledger.accounts (book_id, account_id, name, category) VALUES
    ('DEMO_FUND', 'cash',                 '現金',               'asset'),
    ('DEMO_FUND', 'securities',           '有価証券',           'asset'),
    ('DEMO_FUND', 'receivable_unsettled', '未収入金',           'asset'),
    ('DEMO_FUND', 'accrued_income',       '未収配当・利息',     'asset'),
    ('DEMO_FUND', 'margin_deposit',       '差入証拠金',         'asset'),
    ('DEMO_FUND', 'payable_unsettled',    '未払金',             'liability'),
    ('DEMO_FUND', 'borrowings',           '借入金（信用）',     'liability'),
    ('DEMO_FUND', 'short_positions',      '空売り有価証券',     'liability'),
    ('DEMO_FUND', 'accrued_expense',      '未払費用',           'liability'),
    ('DEMO_FUND', 'capital',              '出資金',             'equity'),
    ('DEMO_FUND', 'retained',             '累積損益',           'equity'),
    ('DEMO_FUND', 'realized_pnl',         '実現損益',           'income'),
    ('DEMO_FUND', 'unrealized_pnl',       '未実現評価損益',     'income'),
    ('DEMO_FUND', 'dividend_income',      '配当',               'income'),
    ('DEMO_FUND', 'interest_income',      '利息',               'income'),
    ('DEMO_FUND', 'commission',           '売買手数料',         'expense'),
    ('DEMO_FUND', 'interest_expense',     '支払利息',           'expense'),
    ('DEMO_FUND', 'slippage_memo',        'スリッページ（参考勘定）', 'expense')
ON CONFLICT (book_id, account_id) DO NOTHING;

-- ── 勘定科目（運営帳簿 = OPS） ──
-- 設計書 §5「運営帳簿（OPS）」
INSERT INTO ledger.accounts (book_id, account_id, name, category) VALUES
    ('OPS', 'cash_bank',       '銀行預金',           'asset'),
    ('OPS', 'prepaid',         '前払費用',           'asset'),
    ('OPS', 'payable',         '未払金',             'liability'),
    ('OPS', 'accrued_expense', '未払費用',           'liability'),
    ('OPS', 'owner_capital',   '元入金',             'equity'),
    ('OPS', 'retained',        '累積損益',           'equity'),
    ('OPS', 'gcp_cost',        'GCP 費用（サービス別サブ）', 'expense'),
    ('OPS', 'llm_cost_fable',  'LLM 費用（Fable）',  'expense'),
    ('OPS', 'llm_cost_mid',    'LLM 費用（中位）',   'expense'),
    ('OPS', 'llm_cost_light',  'LLM 費用（軽量）',   'expense'),
    ('OPS', 'data_cost',       'データ費用',         'expense'),
    ('OPS', 'broker_fee',      'ブローカー手数料',   'expense'),
    ('OPS', 'misc',            '雑費',               'expense')
ON CONFLICT (book_id, account_id) DO NOTHING;

-- ── 開始仕訳（DEMO_FUND）: 借方 cash ¥1,000,000 / 貸方 capital ¥1,000,000 ──
-- 2026-08-02 投資委員会決定（設計書 §9）。証憑は kind='decision'、決定記録 JSON を
-- evidence.payload_ref にインライン保存し sha256 を刻む。
-- （備考: evidence テーブルには専用の jsonb payload 列がないため、決定記録 JSON は
--  「内部参照」として payload_ref に格納する。設計書 §5 の payload_ref の定義に沿う。）
DO $$
DECLARE
    v_run_id      bigint;
    v_evidence_id bigint;
    v_entry_id    bigint;
    v_payload     text := '{"committee":"investment_committee","decided_at":"2026-08-02",'
        || '"book":"DEMO_FUND","currency":"JPY","initial_capital":1000000,'
        || '"entry":{"debit":{"cash":1000000},"credit":{"capital":1000000}},'
        || '"basis":"docs/design/10-data-accounting.md §9 (2026-08-02 投資委員会決定)"}';
BEGIN
    INSERT INTO meta.runs (job_name, code_version, started_at, finished_at, status, params)
    VALUES ('seed.initial_capital', 'T-001',
            TIMESTAMPTZ '2026-08-02 00:00:00+09',
            TIMESTAMPTZ '2026-08-02 00:00:00+09',
            'success', '{"task":"T-001","migration":"0006_seed"}'::jsonb)
    RETURNING run_id INTO v_run_id;

    INSERT INTO ledger.evidence (kind, payload_ref, sha256, source, retrieved_at)
    VALUES ('decision', v_payload, sha256(convert_to(v_payload, 'UTF8')),
            'investment_committee', TIMESTAMPTZ '2026-08-02 00:00:00+09')
    RETURNING evidence_id INTO v_evidence_id;

    INSERT INTO ledger.journal_entries
        (book_id, entry_date, description, evidence_id, posted_by, run_id)
    VALUES ('DEMO_FUND', DATE '2026-08-02', 'デモファンド初期出資金 ¥1,000,000',
            v_evidence_id, 'seed.initial_capital', v_run_id)
    RETURNING entry_id INTO v_entry_id;

    INSERT INTO ledger.journal_lines
        (entry_id, line_no, book_id, account_id, debit, credit, currency)
    VALUES
        (v_entry_id, 1, 'DEMO_FUND', 'cash',    1000000, 0,       'JPY'),
        (v_entry_id, 2, 'DEMO_FUND', 'capital', 0,       1000000, 'JPY');
END $$;
