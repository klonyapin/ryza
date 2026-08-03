-- 0012_killswitch_modes.sql
-- Kill Switch 多段化(IPS v1.3 §5・Issue #24)。/kill(凍結)・/winddown(計画的現金化)・
-- /flatten(緊急清算)の3モードを状態機械として永続化する。
--
-- 設計:
--   1. ops.trading_state … 現在状態のシングルトン行(単一 boolean PK で1行を強制)。
--      state ∈ normal | frozen | winding_down | flattening | flattened
--   2. governance.killswitch_events … 追記オンリーの監査証跡。要求(request)と
--      遷移(transition)の両方を記録する(/flatten の2段階確認は request→transition の2行)
--   3. 既存 ops.flags.kill_switch は「state <> 'normal'」の派生ミラーとして維持
--      (全発注経路・日次ジョブの is_engaged 参照を壊さない)。直接書き込み禁止
--   4. governance.decisions.kind に 'frozen_exception_trade' を追加
--      (凍結中の例外的取引を #承認 で1件ずつユーザー承認する経路)
--
-- 保護領域(CLAUDE.md 不変原則6): このスキーマと対応コード(src/ryza/bot/killswitch.py)の
-- 変更は二重確認・監査対象。清算経路(winddown/flatten)は LLM を経由しない決定論コードのみ。

-- ────────────────────────────────────────────────────────────────────────────
-- ops.trading_state: 取引状態の現在値(シングルトン)
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE ops.trading_state (
    singleton  boolean PRIMARY KEY DEFAULT true CHECK (singleton),  -- 常に1行
    state      text NOT NULL
               CHECK (state IN ('normal', 'frozen', 'winding_down', 'flattening', 'flattened')),
    reason     text,
    updated_by text NOT NULL,               -- Discord ユーザー ID または 'system:<source>'
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- 既存 Kill Switch フラグからのバックフィル(有効なら凍結相当で引き継ぐ)。
INSERT INTO ops.trading_state (state, reason, updated_by)
SELECT CASE WHEN f.enabled THEN 'frozen' ELSE 'normal' END,
       f.reason,
       f.updated_by
FROM ops.flags f
WHERE f.name = 'kill_switch'
ON CONFLICT (singleton) DO NOTHING;

-- ────────────────────────────────────────────────────────────────────────────
-- governance.killswitch_events: 状態機械の監査証跡(追記オンリー)
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE governance.killswitch_events (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type   text NOT NULL CHECK (event_type IN ('request', 'transition')),
    command      text NOT NULL
                 CHECK (command IN ('kill', 'winddown', 'flatten', 'resume',
                                    'liquidation_complete')),
    from_state   text NOT NULL,
    to_state     text NOT NULL,              -- request の場合は「要求先」の状態
    actor        text NOT NULL,              -- Discord ユーザー ID または 'system:<source>'
    reason       text,
    confirmed    boolean NOT NULL DEFAULT false,  -- 2段階確認の2段目まで完了したか
    hook_engaged boolean,                    -- 執行フックを呼んだか(NULL=フック不要の遷移)
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX killswitch_events_created_idx ON governance.killswitch_events (created_at);

-- ────────────────────────────────────────────────────────────────────────────
-- governance.decisions: 凍結中の例外的取引の承認種別を追加
-- ────────────────────────────────────────────────────────────────────────────
ALTER TABLE governance.decisions DROP CONSTRAINT decisions_kind_check;
ALTER TABLE governance.decisions ADD CONSTRAINT decisions_kind_check
    CHECK (kind IN ('pr', 'strategy_promotion', 'breaker_resume', 'budget',
                    'frozen_exception_trade', 'other'));

-- ────────────────────────────────────────────────────────────────────────────
-- データカタログ用コメント
-- ────────────────────────────────────────────────────────────────────────────
COMMENT ON TABLE ops.trading_state IS
    'Kill Switch 多段状態機械の現在値(IPS v1.3 §5)。全発注経路は normal 以外で新規発注禁止。';
COMMENT ON COLUMN ops.trading_state.state IS
    'normal=通常 | frozen=/kill 凍結 | winding_down=/winddown 段階的現金化中 | '
    'flattening=/flatten 緊急清算中 | flattened=清算完了(現金)。復帰は /resume のみ。';
COMMENT ON TABLE governance.killswitch_events IS
    'Kill Switch 操作の追記オンリー監査証跡。要求(request)と遷移(transition)を記録。';
COMMENT ON COLUMN governance.killswitch_events.hook_engaged IS
    '執行フック(ブローカーアダプタ)を呼んだか。false=執行層未接続で状態遷移のみ。';
COMMENT ON COLUMN ops.flags.enabled IS
    'true=有効。kill_switch は ops.trading_state の派生ミラー(state<>normal)。直接書き込み禁止。';
