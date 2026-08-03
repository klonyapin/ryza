-- 0011_demo_capital_increase.sql
-- DEMO_FUND 追加出資 ¥9,000,000(初期 ¥1,000,000 → 合計 ¥10,000,000)。
-- 2026-08-03 投資委員会決定: 実弾は少額から始めるが、実験資金(デモ)は
-- マルチポッド運用・政策ミックス・単元制約が機能する規模にする(80-ips.md §1)。
-- 戦略の採用判定は従来どおり E8 スイープ(¥10万〜100万)の実弾想定規模で行い、
-- 本増額で薄まったコスト比(E4)を採用判定に使わない。
-- 適用済みの 0006 は書き換えず、追加出資の仕訳で増額する(追記オンリー原則)。

DO $$
DECLARE
    v_run_id      bigint;
    v_evidence_id bigint;
    v_entry_id    bigint;
    v_payload     text := '{"committee":"investment_committee","decided_at":"2026-08-03",'
        || '"book":"DEMO_FUND","currency":"JPY","additional_capital":9000000,'
        || '"total_capital_after":10000000,'
        || '"entry":{"debit":{"cash":9000000},"credit":{"capital":9000000}},'
        || '"basis":"80-ips.md §1 (2026-08-03 投資委員会決定: 実験資金の増額。'
        || '採用判定は E8 実弾想定規模のまま)"}';
BEGIN
    INSERT INTO meta.runs (job_name, code_version, started_at, finished_at, status, params)
    VALUES ('seed.capital_increase', 'T-011-post',
            TIMESTAMPTZ '2026-08-03 00:00:00+09',
            TIMESTAMPTZ '2026-08-03 00:00:00+09',
            'success', '{"migration":"0011_demo_capital_increase"}'::jsonb)
    RETURNING run_id INTO v_run_id;

    INSERT INTO ledger.evidence (kind, payload_ref, sha256, source, retrieved_at)
    VALUES ('decision', v_payload, sha256(convert_to(v_payload, 'UTF8')),
            'investment_committee', TIMESTAMPTZ '2026-08-03 00:00:00+09')
    RETURNING evidence_id INTO v_evidence_id;

    INSERT INTO ledger.journal_entries
        (book_id, entry_date, description, evidence_id, posted_by, run_id)
    VALUES ('DEMO_FUND', DATE '2026-08-03', 'デモファンド追加出資 ¥9,000,000(実験資金の増額)',
            v_evidence_id, 'seed.capital_increase', v_run_id)
    RETURNING entry_id INTO v_entry_id;

    INSERT INTO ledger.journal_lines
        (entry_id, line_no, book_id, account_id, debit, credit, currency)
    VALUES
        (v_entry_id, 1, 'DEMO_FUND', 'cash',    9000000, 0,       'JPY'),
        (v_entry_id, 2, 'DEMO_FUND', 'capital', 0,       9000000, 'JPY');
END $$;
