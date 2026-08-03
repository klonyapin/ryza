-- 0024: 開発室(代表 ⇄ 設計リードの非同期連絡窓口)— 代表指示 2026-08-03
--
-- **採番の注意**: 0021〜0023 は並行開発中の別ブランチが使う予定で予約されている。
-- 本ファイルは 0024 を仮置きしたもので、マージ順によっては番号の付け替えが要る
-- (``meta.schema_migrations`` は version 文字列で冪等判定するため、**適用済みの
-- 環境で番号だけを変えると二重適用になる**。付け替えるなら未適用のうちに行うこと)。
--
-- 目的: 代表がブラウザ(ダッシュボード)から設計リード(Claude Code セッション)へ
-- 開発の連絡を送れるようにする。従来の経路は代表が Discord に書き、設計リードが
-- ``ryza.bridge_send`` で返す片道ブリッジしかなく、**代表側の発信を機械可読に残す場所が
-- 無かった**(Discord のメッセージ履歴はリポジトリからも DB からも参照できない)。
--
-- ────────────────────────────────────────────────────────────────────────────
-- press.outbox を流用しない理由
-- ────────────────────────────────────────────────────────────────────────────
-- press.outbox は「システム → Discord」の一方向配送キューで、送信者(誰の発言か)と
-- スレッド(会話の順序)の概念を持たない。開発室は**双方向のスレッド**であり、
-- ダッシュボードは会話として時系列に読み出す。両者を同じ表に混ぜると、outbox の
-- 配送状態(sent_at)と会話の中継状態(relayed_at)が同じ列を奪い合う。
-- ただし**Discord への実配送は outbox に載せる**(中継は本表 → outbox の enqueue)。
-- 配送の冪等・リトライ・キャラクター表示を二重に実装しないため。
--
-- ────────────────────────────────────────────────────────────────────────────
-- 追記オンリー(0020 の流儀)+ relayed_at だけを例外にする
-- ────────────────────────────────────────────────────────────────────────────
-- 代表の指示と設計リードの回答は開発の意思決定の証跡であり、後から書き換えられては
-- ならない(不変原則3)。訂正は追記で行う。一方 relayed_at は「Discord へ中継済みか」
-- という**中継の冪等制御に必要な唯一の可変状態**で、これだけを UPDATE 可能にする。
-- 0020 が確立した「権限ではなくテーブル自身の性質として強制する」方針に従い、
-- 列の許可はトリガで判定する(REVOKE はテーブル所有ロール ryza には効かないため、
-- 権限だけでは Bot・手動 psql からの改竄を防げない)。

CREATE TABLE ops.dev_chat (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- 発言者は2者のみ。Discord のオーナー検証や IAP と違い、ここは「誰の発言として
    -- 表示・中継するか」の宣言であり、認証ではない(認証は IAP / DB ロールが持つ)。
    sender     text NOT NULL CHECK (sender IN ('representative', 'design_lead')),
    body       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    -- 代表の発言を Discord のブリッジチャンネルへ中継した時刻。NULL = 未中継。
    -- 設計リードの発言は中継対象外(セッション側が直接書くため)で常に NULL。
    relayed_at timestamptz
);

-- 中継ループ(5秒間隔)が引くのは「未中継の代表発言」だけ。行数が増えても走査量が
-- 一定になるよう部分索引を張る。
CREATE INDEX dev_chat_unrelayed_idx ON ops.dev_chat (id)
    WHERE relayed_at IS NULL AND sender = 'representative';

-- スレッド表示(時系列・直近 N 件)の索引。
CREATE INDEX dev_chat_created_idx ON ops.dev_chat (created_at);

-- ────────────────────────────────────────────────────────────────────────────
-- 追記オンリーの強制(0020 C-3 と同型。ただし relayed_at のみ UPDATE を許す)
-- ────────────────────────────────────────────────────────────────────────────
-- ops.forbid_mutation(0020)をそのまま使えないのは、それが UPDATE を一律で拒むため。
-- relayed_at の一方向遷移(NULL → 時刻)だけを通す専用ガードを置く。
--   * DELETE は常に拒否
--   * relayed_at 以外の列が変わる UPDATE は拒否(本文・発言者・時刻の事後改竄を塞ぐ)
--   * relayed_at を NULL へ戻す/中継済みを別時刻へ書き換える UPDATE も拒否
--     (再中継で Discord に同じ連絡が二度流れる経路を DB 層で消す)
CREATE FUNCTION ops.dev_chat_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'ops.dev_chat の DELETE は禁止(追記オンリー)。開発室の連絡は意思決定の証跡であり、訂正も追記で行う';
    END IF;
    IF NEW.id         IS DISTINCT FROM OLD.id
    OR NEW.sender     IS DISTINCT FROM OLD.sender
    OR NEW.body       IS DISTINCT FROM OLD.body
    OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION
            'ops.dev_chat で UPDATE できるのは relayed_at のみ(追記オンリー)。本文・発言者・投稿時刻は書き換えられない';
    END IF;
    IF OLD.relayed_at IS NOT NULL THEN
        RAISE EXCEPTION
            'ops.dev_chat.relayed_at は一度だけ設定できる(既に % で中継済み)。再中継は二重配送になる', OLD.relayed_at;
    END IF;
    IF NEW.relayed_at IS NULL THEN
        RAISE EXCEPTION
            'ops.dev_chat.relayed_at を NULL へ戻すことはできない(中継の冪等が壊れる)';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER dev_chat_append_only
    BEFORE UPDATE OR DELETE ON ops.dev_chat
    FOR EACH ROW EXECUTE FUNCTION ops.dev_chat_guard();

REVOKE UPDATE, DELETE ON ops.dev_chat FROM PUBLIC;

-- **TRUNCATE は行トリガを迂回する**(0015 で実証・0018 が標準化・0020 が ops に導入)。
-- 一撃でスレッド全体が消えないよう文トリガ+REVOKE で塞ぐ。
CREATE TRIGGER dev_chat_no_truncate
    BEFORE TRUNCATE ON ops.dev_chat
    FOR EACH STATEMENT EXECUTE FUNCTION ops.forbid_truncate();

REVOKE TRUNCATE ON ops.dev_chat FROM PUBLIC;

-- ────────────────────────────────────────────────────────────────────────────
-- 権限(列レベル)
-- ────────────────────────────────────────────────────────────────────────────
-- ダッシュボード(役員室ロール ryza_boardroom)への SELECT/INSERT は
-- ops/deploy-dashboard.sh のロール SQL で与える(保護領域 deploy_path。0020 と同じ分担 —
-- ロールはデプロイスクリプトが所有し、マイグレーションはロールの存在を前提にしない)。
--
-- Bot は現在テーブル所有ロール ryza で動くため、中継の UPDATE に追加の GRANT は要らない
-- (所有者は REVOKE ... FROM PUBLIC の影響を受けない。改竄の防止はトリガが担う)。
-- 将来 Bot を専用ロールへ分離したとき(ops/reminders.yaml: db-role-separation-webhook-url)
-- 必要になるのは **relayed_at だけの列レベル UPDATE** である。分離後に付け忘れて
-- 中継が黙って止まらないよう、ロールが既にあれば今ここで与えておく。
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ryza_bot') THEN
        EXECUTE 'GRANT SELECT, UPDATE (relayed_at) ON ops.dev_chat TO ryza_bot';
    END IF;
END;
$$;

COMMENT ON TABLE ops.dev_chat IS
    '開発室 — 代表(ダッシュボード)と設計リード(Claude Code セッション)の非同期連絡スレッド。'
    '追記オンリーで、可変なのは relayed_at のみ(0024)。';
COMMENT ON COLUMN ops.dev_chat.sender IS
    'representative=代表(ダッシュボードの投稿フォーム)/ design_lead=設計リード'
    '(python -m ryza.governance.devchat --reply)。';
COMMENT ON COLUMN ops.dev_chat.relayed_at IS
    '代表発言を Discord のブリッジチャンネルへ中継(press.outbox へ enqueue)した時刻。'
    'NULL は未中継。設計リードの発言は中継しないため常に NULL。';
COMMENT ON FUNCTION ops.dev_chat_guard IS
    'ops.dev_chat の追記オンリー強制。DELETE を拒み、UPDATE は relayed_at の NULL → 時刻の一方向遷移のみ許す(0024)。';
