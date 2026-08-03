-- 0020: キャラクターアイコンの実行時上書き(代表指示 2026-08-03)
--
-- 目的: 代表が `config/org.yaml` を編集し PR を通さなくても、ダッシュボードから
-- キャラクターのアイコンを差し替えられるようにする。台帳(org.yaml)は**引き続き正**で、
-- 本テーブルはその上に重ねる「実行時の上書き層」である(ローダ src/ryza/org.py の
-- effective_members が YAML → DB の順にマージする)。
--
-- FK を張らない理由: member_id の正は `config/org.yaml`(ファイル)であり DB に
-- メンバー表が無い。整合はローダ側で突合し、台帳に無い member_id の上書き行は
-- **黙って無視**する(YAML から消えたキャラの残骸が表示に混ざらない)。
--
-- ────────────────────────────────────────────────────────────────────────────
-- 履歴方式の選択: 「現在値テーブル + 追記オンリーの別ログ表」(方式 B)
-- ────────────────────────────────────────────────────────────────────────────
-- 上書きの本義は「今どのアイコンか」を1行で引けることなので、現在値は PK=member_id の
-- 1行に保ち UPDATE で上書きする。ただし UPDATE は履歴を消すため、変更は別の追記表
-- (org_icon_override_log)へ必ず1行残す。
--
-- 検討した代替(方式 A: 履歴表だけを持ち、最新行を毎回 window 関数で引く)を採らないのは:
--   1. 読取が全経路(Bot の配送ごと・ダッシュボードの描画ごと)で走るため、
--      「PK 1行の SELECT」で済む形の方が単純かつ速い(即反映のため**キャッシュしない**)
--   2. 削除(初期値に戻す)を「現在値が無い」で表現でき、状態の解釈が一意になる
--      (方式 A では最新行が tombstone かを毎回判定する必要がある)
--   3. ops.flags / ops.flag_events(0007)・ops.trading_state /
--      governance.killswitch_events(0012)と同じ既存の流儀に揃う
-- 方式 B の弱点は「現在値とログが乖離しうる」こと。書込ヘルパ
-- (org.set_icon_override / clear_icon_override)は同一トランザクションで両方に書き、
-- ログ側にだけ書く経路・現在値だけ書く経路をコードに作らないことで担保する。

CREATE TABLE ops.org_icon_overrides (
    member_id  text PRIMARY KEY,            -- config/org.yaml の members[].id
    icon_url   text NOT NULL,               -- https の画像 URL(検証は org.check_icon_url)
    updated_by text NOT NULL,               -- 'representative' 等(IAP が代表1名に限定)
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- 上書きは Discord / Web の表示にそのまま出るため、https 以外を DB 層でも拒む
-- (アプリ側 org.check_icon_url が主たる検証。ここは最後の防壁で、混入経路を塞ぐ)。
ALTER TABLE ops.org_icon_overrides ADD CONSTRAINT org_icon_overrides_https_check
    CHECK (icon_url LIKE 'https://%');

CREATE TABLE ops.org_icon_override_log (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    member_id  text NOT NULL,
    action     text NOT NULL CHECK (action IN ('set', 'reset')),
    icon_url   text,                        -- set=新しい URL / reset=NULL(初期値へ復帰)
    actor      text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    -- action と icon_url の対応を強制する(reset に URL が残る/set が空、を防ぐ)。
    CONSTRAINT org_icon_override_log_action_url_check
        CHECK ((action = 'set') = (icon_url IS NOT NULL))
);
CREATE INDEX org_icon_override_log_member_idx
    ON ops.org_icon_override_log (member_id, created_at);

-- ────────────────────────────────────────────────────────────────────────────
-- 履歴表の追記オンリー強制(独立役員審査 0020 C-3)
-- ────────────────────────────────────────────────────────────────────────────
-- 当初は「ryza_boardroom に INSERT しか GRANT しない」だけで追記オンリーを表現して
-- いたが、それはロール分離が効く経路(ダッシュボード)にしか効かない。テーブル所有
-- ロール(ryza)で動くジョブ・Bot・手動の psql は履歴を UPDATE / DELETE できてしまい、
-- 「現在値の履歴が必ず残る」という 0020 の担保が所有ロールに対しては空手形になる。
-- 0013(governance.minutes / minute_resolutions)が確立した「トリガ + REVOKE」の
-- 先例に揃え、権限ではなくテーブル自身の性質として追記オンリーを強制する。
CREATE FUNCTION ops.forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        '% は % では禁止(追記オンリー)。アイコン上書きの変更履歴は証跡であり、訂正も追記で行う',
        TG_OP, TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER org_icon_override_log_no_mutation
    BEFORE UPDATE OR DELETE ON ops.org_icon_override_log
    FOR EACH ROW EXECUTE FUNCTION ops.forbid_mutation();

REVOKE UPDATE, DELETE ON ops.org_icon_override_log FROM PUBLIC;

-- **TRUNCATE は行トリガを迂回する**(0015 で実証・0018 が標準化)。行トリガだけでは
-- `TRUNCATE ops.org_icon_override_log` の一撃で履歴が全て消えるため、0018 の
-- forbid_truncate と同型の文トリガ + REVOKE TRUNCATE で塞ぐ。
CREATE FUNCTION ops.forbid_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% の TRUNCATE は禁止(追記オンリーの監査証跡)。訂正は追記で行う',
        TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER org_icon_override_log_no_truncate
    BEFORE TRUNCATE ON ops.org_icon_override_log
    FOR EACH STATEMENT EXECUTE FUNCTION ops.forbid_truncate();

REVOKE TRUNCATE ON ops.org_icon_override_log FROM PUBLIC;

-- 現在値表(ops.org_icon_overrides)には同じ制約を掛けない。上書き・削除が本義の
-- 可変表であり、その変更履歴は上のログ表が持つ。ただし TRUNCATE は「上書きを全部
-- 消す」操作でありログを残さないため、0018 が trading.orders(可変表)に対して
-- 行ったのと同じ理由で封鎖する。
CREATE TRIGGER org_icon_overrides_no_truncate
    BEFORE TRUNCATE ON ops.org_icon_overrides
    FOR EACH STATEMENT EXECUTE FUNCTION ops.forbid_truncate();

REVOKE TRUNCATE ON ops.org_icon_overrides FROM PUBLIC;

-- ────────────────────────────────────────────────────────────────────────────
-- 権限に関する注記(0017 との違い)
-- ────────────────────────────────────────────────────────────────────────────
-- 0017 の ops.discord_webhooks は「URL を知る者が誰でも投稿できる」秘密のため、
-- 読取ロール ryza_dashboard から明示的に REVOKE している。本テーブルは秘密を
-- 持たない(公開画像の URL のみ)ので、その除外リストには**入れない** — 組織ページの
-- 表示に必要な読取である。書込は役員室と同じ ryza_boardroom ロールに限定し、
-- GRANT は ops/deploy-dashboard.sh のロール SQL 側で与える(保護領域 deploy_path)。

COMMENT ON TABLE ops.org_icon_overrides IS
    'キャラクターアイコンの実行時上書き(現在値)。config/org.yaml が正で、本表はその上に'
    '重ねる層。行が無ければ台帳の icon_url を使う(0020)。';
COMMENT ON COLUMN ops.org_icon_overrides.member_id IS
    'config/org.yaml の members[].id。FK は張れないためローダ側で突合し、台帳に無い id は無視。';
COMMENT ON COLUMN ops.org_icon_overrides.icon_url IS
    'https の画像 URL。Content-Type image/* の実アクセス検証を通ったものだけを書く。';
COMMENT ON TABLE ops.org_icon_override_log IS
    'アイコン上書きの変更履歴(追記オンリー)。現在値表の UPDATE/DELETE で消える履歴を残す(0020)。';
COMMENT ON COLUMN ops.org_icon_override_log.action IS
    'set=上書き設定/更新、reset=上書き削除(台帳の初期値へ復帰)。';
