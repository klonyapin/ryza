-- 0034: 評価替え(MTM)を独立勘定 securities_mtm へ分離する
--
-- 根拠: ops/reminders.yaml `mtm-separate-account`(独立審査 残渣修正審査記録 新-14 の
--       構造的根治)。判断の全文は docs/design/11-mtm-account-separation.md。
--
-- 採番: 起草時は 0030 だったが、設計リードの採番裁定(2026-08-04)により 0034 へ改番した
--       (0030/0031 は schema-smalls、0032/0033 は icon-rehost が使う)。改番できるのは
--       本 migration が**どの環境にも未適用**だからである(本番 DB の
--       meta.schema_migrations の最新は 0015 — 実測)。適用済みの番号は動かせない。
--
-- ── 何を分けるのか ──────────────────────────────────────────────────────────
-- `securities` は性質の異なる 2 つの量を足し込んでいた: 約定が積む**取得原価**
-- (post_fill)と、締めが積む**評価調整**(post_mark_to_market の delta 累計 =
-- 時価 − 取得原価)である。全売却後に残る「残渣」は後者だけなので、締めはそれを
-- `_util.mtm_book_value` で**推定**していた — 判定子は evidence.kind と
-- journal_entries.posted_by という、どちらも仕訳の自由記入列の連言である。
--
-- 本 migration 以降、取得原価は `securities`、評価調整は `securities_mtm` に入る。
-- 残渣の同定は勘定残高そのものになり推定が消える。NAV は両勘定とも category='asset'
-- なので値としては不変(statements.book_totals は category 駆動)。
--
-- ── 本命の利得: 検査可能な恒等式 ────────────────────────────────────────────
-- 分離の価値は推定の除去より、**別々の量を別々の勘定に置いたことで独立に突合できる**
-- ことにある。分離後は各銘柄について
--     securities 残高(as_of) = replay_position の取得原価(as_of)
-- が恒等式になり、締めが毎日検査できる(ledger.closing の unexplained_residue)。
-- 分離前はこの式が書けない — securities が原価 + 評価調整なので、突合には評価調整を
-- 推定で差し引く必要があり、推定子が汚染されていれば恒等式も一緒に汚染される
-- (新-14 の実測がその形)。この恒等式は新-15(説明不能な残渣を誰も見ていない)を
-- 評価替えの経路を一切参照せずに覆う。
--
-- ── 移行コストがゼロである根拠(実測)──────────────────────────────────────
-- 2026-08-04 時点の本番 DB: journal_entries 2 行(出資仕訳のみ)/ securities 明細
-- 0 行 / nav_snapshots 0 行。よって過去残高の振替仕訳は不要であり、振替が水位を
-- 進めることによる全営業日の再締め(closing.reclose_stale)も発生しない。
-- journal_entries / journal_lines は追記オンリー(0005 の forbid_mutation)なので、
-- 履歴が積まれた後の分離は再分類仕訳を打つしかなく、必ず水位検出に引っかかる。
-- **この変更は今しかコスト無しで入れられない。**
--
-- ── 索引 ────────────────────────────────────────────────────────────────────
-- 0027 の journal_lines_book_account_instrument_idx (book_id, account_id,
-- instrument_id) は勘定 ID を第2列に持つため、新勘定の照会にもそのまま効く。
-- 索引の追加・変更は無い。
--
-- 冪等: INSERT ... ON CONFLICT DO NOTHING / DROP TRIGGER IF EXISTS + CREATE。

-- ── 1. 勘定の追加(ファンド帳簿すべて。将来の LIVE_FUND もここに乗る)──────────
INSERT INTO ledger.accounts (book_id, account_id, name, category)
SELECT b.book_id, 'securities_mtm', '有価証券評価調整', 'asset'
  FROM ledger.books b
 WHERE b.book_type = 'fund'
ON CONFLICT (book_id, account_id) DO NOTHING;

-- ── 2. 書き込みガード ───────────────────────────────────────────────────────
-- 評価調整勘定へ書けるのは締めジョブ(posted_by ∈ MTM_POSTED_BY)だけにする。
-- 読み取り時の述語(_util.mtm_book_value の旧判定子)を**書き込み時の拒否**に
-- 移すことで、「評価替えのつもりで別の値を書いた仕訳が、後で評価替えとして
-- 認識されない」状態そのものを作れなくする。
--
-- **これは防御であって境界ではない**(docs/design/11 §5.2-1・§7): posted_by は
-- post_entry の呼び出し側が決める列なので、値を騙る記帳は依然として可能である。
-- 分離は新-14 の攻撃を**塞がない** — 宛先が securities から securities_mtm へ
-- 移るだけであり、`Dr securities_mtm / Cr capital` を締めジョブ名で立てれば次の
-- 締めが同額を洗い替えて偽の未実現損を立てる。したがって Python 側の posted_by
-- 検証(posting.post_mark_to_market)も**外してはならない**。構造的に断つには
-- DB ロール分離(締め専用ロール + current_user を見るトリガ)が要るが、単一ロール
-- 前提のインフラ全体に波及するため本件では採らない。
--
-- 逆仕訳(reversal_of IS NOT NULL)は許す: 逆仕訳は訂正の唯一の手段(0005 は
-- UPDATE/DELETE を禁じる)であり、評価替えだけ訂正不能にすると是正経路が消える。
-- 逆仕訳は貸方に同額を立てるので残高は自然に相殺し、残渣の同定を汚さない。
--
-- instrument_id 必須: 評価調整は銘柄ごとの残渣同定に使う。銘柄の無い評価調整は
-- どの建玉の調整か分からず、洗い替えの対象から永久に漏れる。
CREATE OR REPLACE FUNCTION ledger.check_mtm_line() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    parent_posted_by text;
    parent_reversal  bigint;
BEGIN
    IF NEW.account_id <> 'securities_mtm' THEN
        RETURN NEW;
    END IF;
    IF NEW.instrument_id IS NULL THEN
        RAISE EXCEPTION
            '評価調整勘定の明細には instrument_id が必須(銘柄ごとの残渣同定に使う)';
    END IF;
    SELECT posted_by, reversal_of INTO parent_posted_by, parent_reversal
      FROM ledger.journal_entries WHERE entry_id = NEW.entry_id;
    IF parent_reversal IS NULL AND parent_posted_by IS DISTINCT FROM 'ledger.closing' THEN
        RAISE EXCEPTION
            '評価調整勘定へ書けるのは締めジョブだけ: posted_by=% '
            '(_util.MTM_POSTED_BY と一致させること)', parent_posted_by;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS journal_lines_mtm_guard ON ledger.journal_lines;
CREATE TRIGGER journal_lines_mtm_guard
    BEFORE INSERT ON ledger.journal_lines
    FOR EACH ROW EXECUTE FUNCTION ledger.check_mtm_line();

COMMENT ON FUNCTION ledger.check_mtm_line() IS
    '評価調整勘定(securities_mtm)の書き込みガード。締めジョブ由来か逆仕訳のみ許可し、'
    'instrument_id を必須にする。posted_by は呼び出し側が決める列なので防御であって'
    '境界ではない(docs/design/11-mtm-account-separation.md §7)。';
