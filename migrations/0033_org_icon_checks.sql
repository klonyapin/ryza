-- 0033: アイコン URL の定期再検証(独立役員審査 0020 C-7 の恒久是正)
--       ops/reminders.yaml icon-rehost-storage の**代替案**として実装する。
--
-- ────────────────────────────────────────────────────────────────────────────
-- なぜ「再ホスト」ではなく「再検証」なのか(結論と根拠の所在)
-- ────────────────────────────────────────────────────────────────────────────
-- リマインダー icon-rehost-storage は「保存時に画像を自前ストレージへ複製する」ことを
-- 恒久是正としつつ、**着手前に再配布の法的懸念を整理し、整理の結果『再ホストしない』と
-- なった場合は定期再検証ジョブを実装する**ことを明示的に許容していた。整理の結論は
-- 「再ホストしない」である。根拠は docs/research/icon-hosting-legal.md(全文)。要旨:
--   * 台帳(config/org.yaml)のアイコンは第三者が著作権を持つ二次創作画像の外部 URL である
--   * Discord の embed アイコンは **Discord 側がサーバから取得する**ため、再ホスト構成は
--     必ず「誰でも取得できる URL」を伴う。それは送信可能化(著作権法23条1項)であり、
--     私的使用の複製(30条1項)は公衆送信を正当化しない(49条1項1号で目的外使用)
--   * 非公開ストレージに留める構成なら公衆送信は避けられるが、その場合 Discord の
--     embed アイコンには使えず 0020 の目的を果たさない
--   * 現行のホットリンクは自らのサーバに複製を作らないため、この論点自体が生じない
-- したがって C-7(保存後に URL 先の画像が差し替えられても気づけない)は**防止**ではなく
-- **検知**で扱う。本マイグレーションはその検知に必要な最小の状態を置く。
--
-- ────────────────────────────────────────────────────────────────────────────
-- 履歴方式: 0020 と同じ「現在値表 + 追記オンリーの別ログ表」(方式 B)
-- ────────────────────────────────────────────────────────────────────────────
-- 毎回の検査結果を全部積むと 9 メンバー × 毎日で単調に膨らむ一方、読み手が知りたいのは
-- 「今の指紋」と「変わった瞬間」の2つしかない。現在値は PK=member_id の1行で持ち、
-- 変化・失敗だけを追記表に残す。0020(org_icon_overrides / org_icon_override_log)と
-- 同じ形にすることで、運用者が覚える流儀を増やさない。

CREATE TABLE ops.org_icon_checks (
    member_id       text PRIMARY KEY,        -- config/org.yaml の members[].id
    icon_url        text NOT NULL,           -- 検査対象の**実効** URL(台帳 or 0020 の上書き)
    -- 指紋(HEAD 応答ヘッダ)。**本文は取得しない** — 検証のために任意の外部 URL から
    -- 大きなデータを引く経路を作ると SSRF の増幅・DoS の的になる(org.check_icon_url の
    -- 既存判断に揃える)。ヘッダは配信元が名乗る値であり、誠実な配信元の差し替えしか
    -- 捕まえられない(限界は docs/research/icon-hosting-legal.md §残存リスク)。
    content_type    text,
    content_length  bigint,
    etag            text,
    last_modified   text,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),  -- この指紋を最初に観測した時刻
    last_checked_at timestamptz NOT NULL DEFAULT now(),  -- 最後に検査した時刻(成否問わず)
    last_error      text                                 -- 直近の失敗理由(成功で NULL に戻す)
);

CREATE TABLE ops.org_icon_check_events (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    member_id   text NOT NULL,
    -- changed = 指紋が変わった(すり替えの疑い)/ error = 到達不能・画像でない
    -- / cleared = 失敗が解消した(error の終端。沈黙で復旧を表現しない)
    event       text NOT NULL CHECK (event IN ('changed', 'error', 'cleared')),
    icon_url    text NOT NULL,
    before_json jsonb,                       -- 変化前の指紋(changed のみ。初回観測は NULL)
    after_json  jsonb,                       -- 変化後の指紋(changed / cleared)
    detail      text,                        -- error の理由など
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX org_icon_check_events_member_idx
    ON ops.org_icon_check_events (member_id, created_at);

-- 追記オンリーの強制(0020 C-3 で確立した流儀)。すり替えの検知記録は証跡であり、
-- 検知された側が消せてはならない。関数 ops.forbid_mutation / ops.forbid_truncate は
-- 0020 で作成済みのため再利用する(重複定義しない)。
CREATE TRIGGER org_icon_check_events_no_mutation
    BEFORE UPDATE OR DELETE ON ops.org_icon_check_events
    FOR EACH ROW EXECUTE FUNCTION ops.forbid_mutation();
REVOKE UPDATE, DELETE ON ops.org_icon_check_events FROM PUBLIC;

-- TRUNCATE は行トリガを迂回する(0015 で実証・0018 が標準化)。
CREATE TRIGGER org_icon_check_events_no_truncate
    BEFORE TRUNCATE ON ops.org_icon_check_events
    FOR EACH STATEMENT EXECUTE FUNCTION ops.forbid_truncate();
REVOKE TRUNCATE ON ops.org_icon_check_events FROM PUBLIC;

-- 現在値表は可変(指紋は更新される)。ただし「全部消す」操作は履歴を残さないため、
-- 0020 が org_icon_overrides に対して行ったのと同じ理由で TRUNCATE だけ封鎖する。
CREATE TRIGGER org_icon_checks_no_truncate
    BEFORE TRUNCATE ON ops.org_icon_checks
    FOR EACH STATEMENT EXECUTE FUNCTION ops.forbid_truncate();
REVOKE TRUNCATE ON ops.org_icon_checks FROM PUBLIC;

COMMENT ON TABLE ops.org_icon_checks IS
    'アイコン URL の指紋(HEAD 応答ヘッダ)の現在値。ホットリンク先の差し替えを検知するための'
    '基準値で、変化は org_icon_check_events に残し #運営 へ通知する(0033・0020 C-7 の検知側是正)。';
COMMENT ON COLUMN ops.org_icon_checks.icon_url IS
    '検査した実効 URL(台帳 config/org.yaml、または 0020 の上書きが勝った後の値)。';
COMMENT ON COLUMN ops.org_icon_checks.last_error IS
    '直近の検査失敗理由。成功すると NULL に戻り、その遷移は cleared イベントとして残る。';
COMMENT ON TABLE ops.org_icon_check_events IS
    'アイコン URL の指紋変化・検査失敗の追記オンリー台帳(0033)。';
