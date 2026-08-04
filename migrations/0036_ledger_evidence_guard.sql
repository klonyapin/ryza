-- 0036: ledger.evidence の UPDATE/TRUNCATE 封鎖(F-4 フォローアップ・Issue #128)
--
-- 根拠: 設計書 §4 は証憑の不変保存を要求するが、0005 の `forbid_mutation` 行トリガと
--       0035 の TRUNCATE ガードは仕訳 2 表(journal_entries / journal_lines)しか対象に
--       していなかった。ledger.evidence には UPDATE/TRUNCATE ガードが無く、参照済みの
--       証憑行の `payload_ref` / `sha256` を UPDATE で書き換えれば、追記オンリーの仕訳
--       証跡が指す**証憑そのものを無音で差し替え**られた(Issue #128)。改竄検知(A-1)の
--       前提となる sha256 の不変性は、UPDATE 封鎖で DB レベルに確立させる必要がある。
--
-- ── 是正の射程(裁定 — Issue #128 選択肢1の強化)────────────────────────────
-- **UPDATE と TRUNCATE を封鎖し、行 DELETE は封鎖しない**。理由:
--   1. UPDATE が本丸: 参照済み証憑の改変は UPDATE でのみ可能(DELETE は FK
--      journal_entries.evidence_id / reconciliations.evidence_id が既に拒否する)。
--   2. 行 DELETE は FK が要所を守っている: 消せるのは**どの仕訳・照合からも参照されない
--      行**(取込されたが記帳に至らなかった証憑)のみ。この経路は tests/conftest.py の
--      clear_residual(_CLEAR_EVIDENCE_SQL)が残留データ隔離に使う。行トリガは rollback と
--      無関係に発火するため、DELETE 封鎖はテスト隔離戦略の再設計(Issue #23 テスト専用
--      DB 化)とセットでなければ導入できない。
--   3. したがって「完全追記オンリー化」は Issue #23 以降に送り、判断を失伝させないため
--      ops/reminders.yaml の `ledger-evidence-full-append-only` に登録する。
--
-- 本 migration の前提として、アプリコード側に evidence の UPDATE/DELETE 経路は存在しない
-- (リポジトリ全域を確認済み — 唯一の DELETE は conftest の未参照行掃除)。したがって
-- アプリ側の修正は不要で、DB 側だけを閉じる。
--
-- 冪等: 関数は CREATE OR REPLACE、トリガは CREATE OR REPLACE TRIGGER(0035 の流儀)。
-- 既存 migration は書き換えない(migrations は追記オンリーの保護領域 — 定款第5条)。

-- ════════════════════════════════════════════════════════════════════════════
-- 1. UPDATE 行トリガ(evidence 専用の関数 — 意味論に合わせた文言)
-- ════════════════════════════════════════════════════════════════════════════
-- 0005 の ledger.forbid_mutation は「訂正は reversal_of による逆仕訳のみ」と述べており
-- 仕訳向けの文言のため流用しない。evidence の訂正手続は「新しい evidence 行を起こし、
-- それを参照する逆仕訳を建てる」であり、証憑行そのものを書き換える経路は存在しない。
CREATE OR REPLACE FUNCTION ledger.forbid_evidence_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'ledger.evidence の % は禁止(証憑は不変)。訂正は新しい evidence 行を起こし、'
        'それを参照する逆仕訳で参照し直すこと',
        TG_OP;
END;
$$;

COMMENT ON FUNCTION ledger.forbid_evidence_update() IS
    'ledger.evidence の UPDATE を禁止する行トリガ関数(0036)。'
    'sha256 / payload_ref の不変性を DB レベルで担保する — 改竄検知(A-1)の前提。'
    '訂正は新しい evidence 行 + 逆仕訳(0005 の reversal_of)で行う。';

CREATE OR REPLACE TRIGGER evidence_no_update
    BEFORE UPDATE ON ledger.evidence
    FOR EACH ROW EXECUTE FUNCTION ledger.forbid_evidence_update();

-- ════════════════════════════════════════════════════════════════════════════
-- 2. TRUNCATE 文トリガ(0035 の ledger.forbid_truncate を流用)
-- ════════════════════════════════════════════════════════════════════════════
-- ledger.forbid_truncate は 0035 で「追記オンリーの仕訳証跡」向けに定義されているが、
-- メッセージは表名を動的に埋め、TRUNCATE の禁止という統制の性質は同じ。関数を新設せず
-- 流用して統制表面を一本化する。
CREATE OR REPLACE TRIGGER evidence_no_truncate
    BEFORE TRUNCATE ON ledger.evidence
    FOR EACH STATEMENT EXECUTE FUNCTION ledger.forbid_truncate();

-- ════════════════════════════════════════════════════════════════════════════
-- 3. REVOKE(所有者ロールには no-op — ロール分離後の統制とドキュメント上の意図表明)
-- ════════════════════════════════════════════════════════════════════════════
-- 0035 と同じく、`REVOKE ... FROM PUBLIC` は現構成(単一ロール ryza が所有者を兼ねる)では
-- 実質 no-op である。主防壁はトリガ側(所有者にも効く)、REVOKE はロール分離導入後の統制と
-- 意図表明として置く。**DELETE は REVOKE しない**: 未参照行の削除は conftest 経路として
-- 残す正当な運用であり、UPDATE / TRUNCATE の意図的封鎖と DELETE の意図的許容を権限層でも
-- 一致させる(意図の可読性)。
REVOKE UPDATE, TRUNCATE ON ledger.evidence FROM PUBLIC;

-- ════════════════════════════════════════════════════════════════════════════
-- カタログ更新(コメントを実態に合わせる — 0021 / 0035 の教訓)
-- ════════════════════════════════════════════════════════════════════════════
-- 不変性の範囲を stale なコメントで曖昧にしない。DELETE を封鎖しない理由も明記して
-- 将来の読者が「ガード漏れ」と誤認しないようにする(Issue #23 / #128 参照)。
COMMENT ON TABLE ledger.evidence IS
    '証憑(GCS 証憑ストアへの参照とハッシュ)。不変性: UPDATE / TRUNCATE は 0036 のトリガ '
    'で禁止(sha256 / payload_ref の書き換え不可 — 改竄検知 A-1 の前提)。'
    '参照済み行の DELETE は journal_entries.evidence_id / reconciliations.evidence_id の '
    'FK が拒否する。未参照行の DELETE は許容(取込ジョブが記帳に至らなかった証憑の掃除 — '
    'tests/conftest.py の残留データ隔離が依存する経路)。'
    '完全追記オンリー化(未参照行の DELETE 封鎖)は Issue #23(テスト専用 DB 化)完了後に '
    '再評価する(ops/reminders.yaml: ledger-evidence-full-append-only)。';
