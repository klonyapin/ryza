-- 0007_press.sql
-- 報道部+Discord Bot 基盤(T-006)のスキーマ。30-press-discord.md §5/§7 に準拠。
--
-- 追加スキーマ:
--   press       … 投稿キュー(outbox)と予兆速報の的中台帳(predictions)
--   governance  … 承認記録(decisions・最小形)。議事録(minutes/stances)は 05-governance の別タスクで拡張
--   ops         … 運用フラグ(flags = Kill Switch 等)とその状態遷移監査(flag_events)
--
-- 冪等・整合性の要:
--   1. press.outbox.sent_at が配送の冪等キー(NULL=未送)。Bot は sent_at IS NULL のみ配送し、
--      配送成功で now() を刻む(条件付き UPDATE により二重送信を物理的に防ぐ)
--   2. governance.decisions は proposal_ref にユニーク制約(1提案=1決定。承認ボタンの二度押しを弾く)
--   3. ops.flags は現在値、ops.flag_events は追記オンリーの遷移履歴(Kill Switch の監査証跡)

-- ────────────────────────────────────────────────────────────────────────────
-- press: 投稿キューと予兆台帳
-- ────────────────────────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS press;

CREATE TABLE press.outbox (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel         text NOT NULL,          -- press|approval|ops|dev(2026-08-03 4チャンネル統合)
    embed_json      jsonb NOT NULL,         -- Discord embed(色・免責フッター込み)
    urgent          boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    sent_at         timestamptz,            -- NULL=未送。配送成功で now()(冪等キー)
    sent_message_id text,                   -- 配送後の Discord メッセージ ID(監査・スレッド起点)
    run_id          bigint NOT NULL         -- 投入したジョブ実行(リネージ)
);
CREATE INDEX outbox_pending_idx ON press.outbox (created_at) WHERE sent_at IS NULL;

CREATE TABLE press.predictions (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id   bigint NOT NULL,            -- 元の速報②(press.outbox.id 等)
    claim       text NOT NULL,
    confidence  numeric NOT NULL CHECK (confidence >= 0 AND confidence <= 1),  -- 0-1
    verify_by   timestamptz NOT NULL,       -- 検証期限
    outcome     text NOT NULL DEFAULT 'pending'
                CHECK (outcome IN ('pending', 'hit', 'miss', 'void')),
    verified_at timestamptz
);
CREATE INDEX predictions_due_idx ON press.predictions (verify_by) WHERE outcome = 'pending';

-- ────────────────────────────────────────────────────────────────────────────
-- governance: 承認記録(最小形)
-- ────────────────────────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS governance;

CREATE TABLE governance.decisions (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    proposal_ref  text NOT NULL,            -- 提案の一意参照(1提案=1決定)
    kind          text NOT NULL             -- pr|strategy_promotion|breaker_resume|budget|other
                  CHECK (kind IN ('pr', 'strategy_promotion', 'breaker_resume', 'budget', 'other')),
    decision      text NOT NULL             -- approve|reject|question
                  CHECK (decision IN ('approve', 'reject', 'question')),
    decided_by    text NOT NULL,            -- Discord ユーザー ID(オーナー検証済み)
    note          text,
    channel_msg_id text,                    -- 承認 embed のメッセージ ID
    decided_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (proposal_ref)                   -- 二度押し・二重記録の防止(冪等)
);

-- ────────────────────────────────────────────────────────────────────────────
-- ops: 運用フラグ(Kill Switch)と遷移履歴
-- ────────────────────────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE ops.flags (
    name       text PRIMARY KEY,            -- 'kill_switch' 等
    enabled    boolean NOT NULL,
    reason     text,
    updated_by text NOT NULL,               -- Discord ユーザー ID
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ops.flag_events (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       text NOT NULL,
    enabled    boolean NOT NULL,            -- 遷移後の値
    reason     text,
    actor      text NOT NULL,               -- Discord ユーザー ID
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX flag_events_name_idx ON ops.flag_events (name, created_at);

-- Discord チャンネル解決(§1 改訂: 4チャンネルをカテゴリ配下に ensure し name→id を記録)。
-- Bot が起動時に指定カテゴリの子チャンネルを走査し、論理名(press|approval|ops|dev)ごとに
-- 既存を再利用するか自動作成し、解決結果をここに upsert する(手動リネームに追従)。
CREATE TABLE ops.discord_channels (
    logical      text PRIMARY KEY,          -- press|approval|ops|dev
    channel_name text NOT NULL,             -- 報道|承認|運営|dev(Discord 上の表示名)
    channel_id   text NOT NULL,             -- 解決/作成した Discord チャンネル ID
    category_id  text NOT NULL,             -- 所属カテゴリ ID
    resolved_at  timestamptz NOT NULL DEFAULT now()
);

-- ────────────────────────────────────────────────────────────────────────────
-- データカタログ用コメント
-- ────────────────────────────────────────────────────────────────────────────
COMMENT ON SCHEMA press IS '報道部の投稿キューと予兆速報の的中台帳。';
COMMENT ON TABLE press.outbox IS '投稿キュー。Bot が sent_at IS NULL をポーリング配送し既送管理(§5 通知配送)。';
COMMENT ON COLUMN press.outbox.channel IS '配送先の論理チャンネル(morning|flash|approval|daily|audit|mgmt)。';
COMMENT ON COLUMN press.outbox.embed_json IS 'Discord embed(色・免責フッター込み)。';
COMMENT ON COLUMN press.outbox.urgent IS '緊急フラグ(速報は即時配送・赤 embed)。';
COMMENT ON COLUMN press.outbox.sent_at IS 'NULL=未送。配送成功で now()。二重送信防止の冪等キー。';
COMMENT ON COLUMN press.outbox.sent_message_id IS '配送後の Discord メッセージ ID。';
COMMENT ON COLUMN press.outbox.run_id IS '投入したジョブ実行(meta.runs)。';

COMMENT ON TABLE press.predictions IS '速報②(予兆)の的中台帳。期限到来で自動判定し月次品質指標に(§3)。';
COMMENT ON COLUMN press.predictions.confidence IS '確度 0-1。';
COMMENT ON COLUMN press.predictions.verify_by IS '検証期限。';
COMMENT ON COLUMN press.predictions.outcome IS 'pending|hit|miss|void。';

COMMENT ON SCHEMA governance IS 'ガバナンス。承認記録(decisions・最小形)。議事録は 05-governance の別タスクで拡張。';
COMMENT ON TABLE governance.decisions IS '承認フローの決定記録。押下者のオーナー検証済み(§5 承認 UI)。';
COMMENT ON COLUMN governance.decisions.proposal_ref IS '提案の一意参照。UNIQUE で 1提案=1決定を強制(二度押し防止)。';
COMMENT ON COLUMN governance.decisions.kind IS 'pr|strategy_promotion|breaker_resume|budget|other。';
COMMENT ON COLUMN governance.decisions.decision IS 'approve|reject|question。';
COMMENT ON COLUMN governance.decisions.decided_by IS '押下した Discord ユーザー ID(オーナー)。';

COMMENT ON SCHEMA ops IS '運用フラグと遷移監査。Kill Switch は全発注経路が参照。';
COMMENT ON TABLE ops.flags IS '運用フラグの現在値(kill_switch 等)。全発注経路が参照。';
COMMENT ON COLUMN ops.flags.enabled IS 'true=有効(Kill Switch なら発注停止)。';
COMMENT ON TABLE ops.flag_events IS 'フラグ遷移の追記オンリー監査証跡。';

COMMENT ON TABLE ops.discord_channels IS 'Discord チャンネルの論理名→ID 解決結果(Bot が起動時 ensure して記録)。';
COMMENT ON COLUMN ops.discord_channels.logical IS 'outbox.channel の値(press|approval|ops|dev)。';
COMMENT ON COLUMN ops.discord_channels.channel_name IS 'Discord 上の表示名(報道|承認|運営|dev)。';
COMMENT ON COLUMN ops.discord_channels.channel_id IS '解決/作成したチャンネル ID。';
COMMENT ON COLUMN ops.discord_channels.category_id IS '所属カテゴリ ID。';
