-- 0030: governance.decision_vetoes に否認の出所を表す origin 列を追加する
--
-- 根拠: ops/reminders.yaml `veto-origin-column`(独立役員審査 0021 C-8 / #承認 ボタン
--       配線の再審査 重要-5)。0021 のコメントが「継続検討」としていた列を、判断材料が
--       確定したので「足す」方向で確定させる。
--
-- ── なぜ足すのか(判断材料が確定した経緯)──────────────────────────────────
-- 0021 は decision_vetoes.run_id を NULL 可とした。理由は「否認は代表の作為(意思表示)で
-- ありジョブの生成物ではないため、生成元 Run という概念が本来的に無い」であり、審査 C-8 は
-- この結論を妥当と認めた。継続検討とされたのは、当時は否認を記録する経路が何種類できるか
-- 未確定だったからである(reminders の判断材料①)。
--
-- その後 #承認 の否認ボタン配線が入り、記録経路は Discord の3つに確定した:
--   (1) #承認 のみなし承認通知に付く否認ボタン → VetoModal → notices.apply_veto
--   (2) スラッシュコマンド /veto                → notices.apply_veto
--   (3) スラッシュコマンド /unveto(撤回)      → notices.withdraw_veto
-- いずれも run_id を記録するようになったため、「run_id が NULL = Run が無い」と
-- 「埋め忘れ」の区別は当面つく(審査 重要-5 後段)。それでも origin を足すのは、
-- **run_id では経路を識別できない**からである:
--   - (1) と (2) は同じ ``bot.governance.veto`` という job_name で Run を開く。
--     meta.runs を辿っても「ボタンで押されたのか、コマンドで打たれたのか」は分からない
--   - 将来 CLI・ジョブから否認が書かれるようになれば、run_id の有無だけが手がかりになるが、
--     それは「Run を開いたかどうか」であって「どの経路か」ではない
-- そして経路の事後識別は、この統制系では飾りではない: **オーナー検証は呼び出し側が供給した
-- 2 引数(vetoed_by と owner_ids)の比較でしかなく**(審査 重要-5 前段)、DB は「本当に代表が
-- 押したか」を独立に知り得ない。せめて「どの経路から書かれたか」が行に残っていれば、
-- 想定外の経路(例: 配線したつもりのないジョブ)からの否認を事後に検出できる。これが
-- reminders の判断材料②(監査が出所別の集計を必要とするか)に対する答えでもある。
--
-- ── 語彙 ──────────────────────────────────────────────────────────────────
--   'discord_button'  … #承認 の否認ボタン(+理由モーダル)
--   'discord_command' … /veto ・ /unveto
--   'cli'             … 人手のスクリプト・保守作業からの直接呼び出し
--   'job'             … 自動ジョブ内からの記録
-- 現時点で書き手があるのは discord_button と discord_command の2つだけである。cli / job を
-- 先に語彙へ入れるのは、**後から値を足す変更が CHECK の改定(= 保護領域の migration)を
-- 要する**ためで、想定済みの経路まで手続きの対象にすると「とりあえず既存の値で書いておく」
-- 誘因が生まれる(それは列の目的そのものを壊す)。逆に、ここに無い経路が現れたときは
-- CHECK 違反で**必ず止まる** —— 黙って未知の出所が混ざることはない。
--
-- ── 既存行のバックフィル ────────────────────────────────────────────────────
-- 既存行は 'discord_button' で埋める。**これは推定であり観測ではない。** 本列が無い時期に
-- 書かれた行について、ボタン経路と /veto 経路を事後に区別する情報はどこにも無い
-- (上記のとおり job_name が同一)。確実に言えるのは「記録経路は Discord のみであり、
-- CLI・ジョブ由来ではない」ことだけで、その中で既定の経路(通知に付くボタン)を採った。
-- 別値('legacy' 等)を足して区別する案は採らない —— 語彙に「出所不明」を常設すると、
-- 新規経路が origin を埋めずにそこへ流れ込む逃げ道になる。
--
-- ── 追記オンリー原則との関係(0021 の decision_vetoes_no_mutation)──────────
-- decision_vetoes は行トリガで UPDATE / DELETE を拒否するため、**UPDATE によるバックフィルは
-- できない**。ALTER TABLE ADD COLUMN は DDL であり DML の行トリガを発火しないので、
-- `ADD COLUMN ... NOT NULL DEFAULT 'discord_button'` で既存行を埋め、**直後に DROP DEFAULT
-- する**。DEFAULT を残すと、origin を渡し忘れた将来の経路が黙って 'discord_button' として
-- 記録され、この列の存在意義(経路の一次識別)がエラーも警告も無いまま失われる。
-- DEFAULT を落とせば、渡し忘れは NOT NULL 違反として即座に落ちる。
--
-- 冪等: 列の存在を information_schema で見て 1 度だけ追加 / 制約は pg_constraint を見て追加 /
--       CREATE OR REPLACE VIEW。

-- ════════════════════════════════════════════════════════════════════════════
-- 1. origin 列(NOT NULL・バックフィル後に DEFAULT を落とす)
-- ════════════════════════════════════════════════════════════════════════════
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'governance'
          AND table_name = 'decision_vetoes'
          AND column_name = 'origin'
    ) THEN
        ALTER TABLE governance.decision_vetoes
            ADD COLUMN origin text NOT NULL DEFAULT 'discord_button';
        -- 既存行のバックフィルはここで完了している。以後の INSERT には必ず
        -- 呼び出し側が origin を渡す(渡し忘れは NOT NULL 違反で落ちる)。
        ALTER TABLE governance.decision_vetoes ALTER COLUMN origin DROP DEFAULT;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'decision_vetoes_origin_check'
    ) THEN
        ALTER TABLE governance.decision_vetoes ADD CONSTRAINT decision_vetoes_origin_check
            CHECK (origin IN ('discord_button', 'discord_command', 'cli', 'job'));
    END IF;
END
$$;

-- ════════════════════════════════════════════════════════════════════════════
-- 2. 現決定 view に出所を通す
-- ════════════════════════════════════════════════════════════════════════════
-- 承認記録を読むコードは governance.decisions ではなく本 view を読むのが 0021 以来の標準で
-- あり、出所だけ decision_vetoes を直読させると「view を読めば足りる」という前提が崩れる
-- (0021 C-5 / 0029 と同じ理由)。CREATE OR REPLACE VIEW は既存列の順序・型を変えられない
-- ため、末尾に追加する。値は最新の否認系行(latest)のもの —— 撤回で否認が解けている場合は
-- 「撤回を書いた経路」を返す。これは意図した挙動で、is_vetoed=false のときに知りたいのは
-- 「誰がどこから撤回したか」だからである。
CREATE OR REPLACE VIEW governance.current_decisions AS
SELECT
    d.id                                   AS decision_id,
    d.proposal_ref,
    d.kind,
    d.decision                             AS recorded_decision,
    CASE
        WHEN latest.veto_id IS NULL OR latest.kind = 'withdrawal' THEN d.decision
        ELSE 'vetoed'
    END                                    AS effective_decision,
    (latest.veto_id IS NOT NULL AND latest.kind <> 'withdrawal') AS is_vetoed,
    d.decided_by,
    d.note,
    d.channel_msg_id,
    d.decided_at,
    latest.veto_id,
    latest.kind                            AS veto_kind,
    latest.vetoed_by,
    latest.reason                          AS veto_reason,
    latest.vetoed_at,
    resolved.revert_commit,
    resolved.derived_effects_ref,
    d.reviewed_sha,
    d.review_ref,
    latest.origin                          AS veto_origin
FROM governance.decisions d
LEFT JOIN LATERAL (
    SELECT vv.veto_id, vv.kind, vv.vetoed_by, vv.reason, vv.vetoed_at, vv.origin
    FROM governance.decision_vetoes vv
    WHERE vv.decision_id = d.id
    ORDER BY vv.veto_id DESC
    LIMIT 1
) latest ON true
LEFT JOIN LATERAL (
    SELECT
        (SELECT vc.revert_commit
         FROM governance.decision_vetoes vc
         WHERE vc.decision_id = d.id AND vc.revert_commit IS NOT NULL
           AND vc.veto_id > w.since
         ORDER BY vc.veto_id DESC LIMIT 1) AS revert_commit,
        (SELECT vd.derived_effects_ref
         FROM governance.decision_vetoes vd
         WHERE vd.decision_id = d.id AND vd.derived_effects_ref IS NOT NULL
           AND vd.veto_id > w.since
         ORDER BY vd.veto_id DESC LIMIT 1) AS derived_effects_ref
    FROM (
        -- 直近の撤回。撤回が無ければ 0(= 全行が対象)。
        SELECT coalesce(max(vw.veto_id), 0) AS since
        FROM governance.decision_vetoes vw
        WHERE vw.decision_id = d.id AND vw.kind = 'withdrawal'
    ) w
) resolved ON true;

-- ════════════════════════════════════════════════════════════════════════════
-- データカタログ用コメント
-- ════════════════════════════════════════════════════════════════════════════
COMMENT ON COLUMN governance.decision_vetoes.origin IS
    '否認を記録した経路。discord_button(#承認 の否認ボタン+理由モーダル)| '
    'discord_command(/veto ・ /unveto)| cli(人手の直接呼び出し)| job(自動ジョブ)。'
    'run_id では経路を識別できない(ボタンと /veto は同じ job_name で Run を開く)ため、'
    '経路の一次識別はこの列が担う。オーナー検証は呼び出し側供給の 2 引数比較でしかなく、'
    'DB は「本当に代表が押したか」を独立に知り得ない —— せめて出所が残っていれば'
    '想定外の経路からの否認を事後に検出できる(0021 C-8 / 重要-5)。'
    '0030 より前に書かれた既存行は discord_button でバックフィルした(推定 —— 当時の'
    '記録経路は Discord のみで、ボタンと /veto を事後に区別する情報が存在しない)。';

COMMENT ON COLUMN governance.decision_vetoes.run_id IS
    '記録したジョブ実行(meta.runs)。否認は代表の作為でありジョブの生成物ではないため NULL 可'
    '(0013 の NOT NULL はジョブ産出物ゆえ)。**経路の識別には使えない** —— 出所は origin 列'
    '(0030)が持つ。run_id が答えるのは「どの実行の中で書かれたか」だけである。';

COMMENT ON VIEW governance.current_decisions IS
    '現決定(承認記録 + 否認履歴の合成)。effective_decision は decisions.decision の4値 + '
    '''vetoed''。承認記録を読むコードは decisions ではなく本 view を読む'
    '(否認された決定を承認済みとして扱わないため)。'
    'revert_commit / derived_effects_ref は列単位で最新の非 NULL 値(直近の撤回より後)。'
    'veto_origin は最新の否認系行の出所(0030。撤回済みなら撤回を書いた経路)。';
