-- 0017: Webhook 方式の発信者表示配送(代表指示 2026-08-03)
--
-- Bot の通常投稿は名前・アイコンを投稿ごとに変えられないため、チャンネルごとに
-- webhook 'ryza-org' を ensure し、username=「名前(役職)」/ avatar_url を設定して
-- 投稿する(src/ryza/bot/webhooks.py)。本テーブルはその解決結果の記録
-- (ops.discord_channels と同じ流儀 — Bot が起動時に upsert)。
-- 権限不足で確保できないチャンネルは行が無く、配送は Bot 投稿へフォールバックする。
--
-- 秘密の取り扱い(独立役員審査 2026-08-03 の条件): webhook URL は URL を知る者が
-- 誰でも当該チャンネルへ投稿できるため、Bot トークンより露出面が広い秘密である。
-- 現構成は単一 DB ロール(ryza)でアプリ全体が動いており、同一ロール内での完全な
-- 読取制限は現構成では不可能。完全な最小権限化(配送コンポーネントだけが読める
-- ロール分離)は実弾移行前提の DB ロール分離で行う(ops/reminders.yaml 登録済み)。
-- デモ期間は単一利用者 VM・非公開 DB のため残余リスクを受容する。
-- ログ・例外への URL 混入はアプリ側でマスクする(webhooks.mask_url)。

CREATE TABLE ops.discord_webhooks (
    logical     text PRIMARY KEY,           -- press|approval|ops|dev(outbox.channel の値)
    webhook_id  text NOT NULL,              -- Discord webhook ID
    webhook_url text NOT NULL,              -- 実行 URL(トークン込み — DB は非公開運用)
    resolved_at timestamptz NOT NULL DEFAULT now()
);

-- 0005 流儀: PUBLIC からの一切のアクセスを剥奪(将来のロール追加時に既定で見えない)。
REVOKE ALL ON ops.discord_webhooks FROM PUBLIC;

COMMENT ON TABLE ops.discord_webhooks IS
    'チャンネル別 webhook(ryza-org)の解決結果。発信者キャラクター表示の配送に使う(0017)。';
COMMENT ON COLUMN ops.discord_webhooks.logical IS 'outbox.channel の値(press|approval|ops|dev)。';
COMMENT ON COLUMN ops.discord_webhooks.webhook_id IS 'Discord webhook ID。';
COMMENT ON COLUMN ops.discord_webhooks.webhook_url IS '実行 URL(トークン込み)。';
