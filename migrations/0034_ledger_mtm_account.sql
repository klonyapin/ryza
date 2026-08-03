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

-- ── 2. 逆仕訳の実突合(免除条件の共通部品)─────────────────────────────────
-- 建玉勘定のガードは逆仕訳を免除するが、免除の判定は ``reversal_of IS NOT NULL``
-- という**フラグでは行わない**(独立審査 新-18)。``reversal_of`` は ``post_entry`` の
-- 公開引数であり、対象仕訳の内容とも帳簿とも突合されない — 審査の実測では、無関係な
-- entry_id を ``reversal_of`` に入れるだけで posted_by 検証を迂回して
-- ``Dr securities_mtm 3,000,000 / Cr capital`` が通り、次の締めが全額を洗い替えて
-- NAV 13,000,001→10,000,001・偽の未実現損 3,000,000 を立てた(新-14(a) の完全再現)。
--
-- そこで免除は「その明細が、対象仕訳の同じ明細を**打ち消しているか**」の実突合にする:
-- 同一帳簿・同一勘定・同一銘柄で借方と貸方が入れ替わった行が対象仕訳に存在すること。
-- ``posting.reverse_entry`` が作る逆仕訳はこれを厳密に満たす一方、金額や銘柄を差し替えた
-- 「逆仕訳を騙る記帳」は落ちる。免除の範囲が「既にある記帳の取り消し」ちょうどになる。
CREATE OR REPLACE FUNCTION ledger.reversal_mirrors_line(
    target_entry bigint, book text, account text, instrument bigint,
    d numeric, c numeric
) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1
        FROM ledger.journal_entries t
        JOIN ledger.journal_lines tl ON tl.entry_id = t.entry_id
        WHERE t.entry_id = target_entry
          AND t.book_id = book
          AND tl.book_id = book
          AND tl.account_id = account
          AND tl.instrument_id IS NOT DISTINCT FROM instrument
          AND tl.debit = c AND tl.credit = d
    );
$$;

COMMENT ON FUNCTION ledger.reversal_mirrors_line(bigint, text, text, bigint, numeric, numeric) IS
    '建玉勘定ガードの逆仕訳免除を実突合する(独立審査 新-18)。reversal_of のフラグでは'
    'なく「対象仕訳に借貸を入れ替えた同一勘定・同一銘柄・同額の明細があるか」を見る。';

-- ── 3. 評価調整勘定の書き込みガード ─────────────────────────────────────────
-- 評価調整勘定へ書けるのは締めジョブ(posted_by ∈ MTM_POSTED_BY)だけにする。
-- 読み取り時の述語(_util.mtm_book_value の旧判定子)を**書き込み時の拒否**に
-- 移すことで、「評価替えのつもりで別の値を書いた仕訳が、後で評価替えとして
-- 認識されない」状態そのものを作れなくする。
--
-- **これは防御であって境界ではない**(docs/design/11 §5.2-1・§7): posted_by は
-- post_entry の呼び出し側が決める列なので、値を騙る記帳は依然として可能である。
-- 分離は新-14 の攻撃を**塞がない** — 宛先が securities から securities_mtm へ
-- 移るだけであり、`Dr securities_mtm / Cr capital` を締めジョブ名で立てれば次の
-- 締めが同額を洗い替えて偽の未実現損を立てる(審査実測: NAV 13,000,000→10,000,000、
-- `unexplained_residue` は空)。**原価恒等式はこの偽装を検出しない** — 恒等式が覆うのは
-- 原価勘定側だけである。したがって Python 側の posted_by 検証
-- (posting.post_mark_to_market)も**外してはならない**。構造的に断つには DB ロール分離
-- (締め専用ロール + current_user を見るトリガ)が要るが、単一ロール前提のインフラ全体に
-- 波及するため本件では採らない。
--
-- 逆仕訳は許す(免除判定は §2 の実突合): 逆仕訳は訂正の唯一の手段(0005 は
-- UPDATE/DELETE を禁じる)であり、評価替えだけ訂正不能にすると是正経路が消える。
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
    IF parent_reversal IS NOT NULL
       AND ledger.reversal_mirrors_line(parent_reversal, NEW.book_id, NEW.account_id,
                                        NEW.instrument_id, NEW.debit, NEW.credit) THEN
        RETURN NEW;  -- 既存の評価替え明細を打ち消す逆仕訳(実突合済み)
    END IF;
    IF parent_posted_by IS DISTINCT FROM 'ledger.closing' THEN
        RAISE EXCEPTION
            '評価調整勘定へ書けるのは締めジョブか実突合済みの逆仕訳だけ: posted_by=% '
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
    '評価調整勘定(securities_mtm)の書き込みガード。締めジョブ由来か実突合済みの逆仕訳'
    'のみ許可し、instrument_id を必須にする。posted_by は呼び出し側が決める列なので'
    '防御であって境界ではない(docs/design/11-mtm-account-separation.md §7)。';

-- ── 4. 原価勘定の書き込みガード(独立審査 新-20)────────────────────────────
-- 原価恒等式の視界は ``held_instruments``(instrument_id 付き明細)に限られる。
-- 審査の実測では ``Dr securities 2,000,000 / Cr capital``(instrument_id NULL)が
-- ``held_instruments()==[]`` により恒等式に一度も掛からず、NAV 12,000,000 が**無言で
-- 恒久残存**した。検出器を足しても「検出器の視界の外」は残るので、視界の外に置けなく
-- することで塞ぐ。
--
-- 条件は 2 つ。(a) instrument_id 必須 — 銘柄の無い建玉は恒等式にも洗い替えにも掛からない。
-- (b) 親仕訳の証憑 kind が ``POSITION_EVIDENCE_KINDS``(broker_fill / in_kind_contribution)
-- であること、または実突合済みの逆仕訳であること。
--
-- **なぜ posted_by ではなく evidence.kind で切るのか**: 原価勘定の書き手は約定
-- (post_fill)・現物拠出・締めの未記帳約定取り込みと複数あり、posted_by は呼び出し側ごとに
-- 違う(ledger.posting / ledger.closing / 執行系)。許可リストにすると広すぎるか正当な
-- 呼び出しを壊すかのどちらかになる。一方 kind は ``replay_position`` が数量を再生する
-- ときに見る値そのものなので、**ガードと恒等式が同じ定義を共有する**ことになり、
-- 「原価勘定に載る行は必ず数量再生の対象」という不変式が構造で保たれる。
--
-- **残る限界(黙って強い保証に見せない)**: kind も証憑の自由記入列であり、数量つき
-- payload を自分で書いた ``broker_fill`` を騙ることはできる。ただしそのとき建玉は
-- **申告どおり再生され**、恒等式は「申告と帳簿の内部整合」として成立してしまう
-- (docs/design/11 §7・独立審査 新-21)。本ガードが塞ぐのは「黙って NAV に混ざる」経路で
-- あって「嘘を申告する」経路ではない。後者は運用統制(拠出 API の posted_by 制限と
-- #運営 通知)と監査の領分である。
CREATE OR REPLACE FUNCTION ledger.check_cost_line() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    parent_reversal bigint;
    parent_kind     text;
BEGIN
    IF NEW.account_id <> 'securities' THEN
        RETURN NEW;
    END IF;
    IF NEW.instrument_id IS NULL THEN
        RAISE EXCEPTION
            '原価勘定の明細には instrument_id が必須'
            '(銘柄の無い建玉は原価恒等式にも洗い替えにも掛からない)';
    END IF;
    SELECT je.reversal_of, e.kind INTO parent_reversal, parent_kind
      FROM ledger.journal_entries je
      JOIN ledger.evidence e ON e.evidence_id = je.evidence_id
     WHERE je.entry_id = NEW.entry_id;
    IF parent_reversal IS NOT NULL
       AND ledger.reversal_mirrors_line(parent_reversal, NEW.book_id, NEW.account_id,
                                        NEW.instrument_id, NEW.debit, NEW.credit) THEN
        RETURN NEW;  -- 既存の建玉明細を打ち消す逆仕訳(実突合済み)
    END IF;
    IF parent_kind IS DISTINCT FROM 'broker_fill'
       AND parent_kind IS DISTINCT FROM 'in_kind_contribution' THEN
        RAISE EXCEPTION
            '原価勘定へ書けるのは数量を再生できる証憑だけ: evidence.kind=% '
            '(_util.POSITION_EVIDENCE_KINDS と一致させること。現物拠出は '
            'posting.post_in_kind_contribution を使う)', parent_kind;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS journal_lines_cost_guard ON ledger.journal_lines;
CREATE TRIGGER journal_lines_cost_guard
    BEFORE INSERT ON ledger.journal_lines
    FOR EACH ROW EXECUTE FUNCTION ledger.check_cost_line();

COMMENT ON FUNCTION ledger.check_cost_line() IS
    '原価勘定(securities)の書き込みガード。instrument_id を必須にし、数量を再生できる'
    '証憑(POSITION_EVIDENCE_KINDS)か実突合済みの逆仕訳だけを許す。「原価勘定に載る行は'
    '必ず数量再生の対象」という不変式を構造で保つ(独立審査 新-20)。';
