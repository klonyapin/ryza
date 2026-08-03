-- 0031: meta.runs.status に CHECK を置いて語彙を凍結する
--
-- 根拠: ops/reminders.yaml `meta-runs-status-check`(独立役員審査 0027 中-4 /
--       docs/reviews/0027-indexes-independent-review.md)。
--
-- ── 何を直すのか ──────────────────────────────────────────────────────────
-- 0027 が追加した部分索引 `meta.runs (run_id DESC) WHERE status = 'running'` は、
-- **述語が守られていない自由文字列に依存している**。`meta.runs.status` に CHECK は無く
-- (0001)、`running|success|failed` という語彙の根拠は列コメントだけだった。したがって
-- `'starting'` / `'retrying'` のような値を後から足す、あるいは `fetch_running_runs` の述語を
-- `status IN (...)` へ広げると、**エラーも警告も出ないまま索引が使われなくなる**。
-- テストは索引の存在と定義しか見ておらず(EXPLAIN のプラン検証はフレーク源なので
-- テスト化していない)、CI の DB は空に近いので EXPLAIN でも捕まらない。
-- 比較対象の `press.outbox` の `outbox_pending_idx`(0007)は述語が `sent_at IS NULL` という
-- **構造的条件**でドリフトしえない。ここを同じ強度に引き上げるのが本 migration である。
--
-- ── なぜ (a) CHECK であって (b) 述語の構造化ではないのか ────────────────────
-- reminders は2案を併記していた: (a) status に CHECK を置いて語彙を凍結する、
-- (b) 索引の述語を `finished_at IS NULL` という構造的条件に置き換える。(a) を採る:
--   - (b) は「実行中」の定義を status と finished_at の**2 箇所**に散らす。両者は
--     provenance/runs.py の `finish()` が同時に書くので現状は一致するが、一致を保証する
--     制約はどこにも無い。索引の沈黙劣化を、二重定義の沈黙不整合に置き換えるだけになる
--   - (b) は `fetch_running_runs` 側の述語も同時に書き換える必要があり、書き換えを
--     怠れば索引は結局使われない —— 直したい失敗モードがそのまま残る
--   - (a) は語彙を DB 側の**1 箇所**に固定する。索引の述語も dashboard の述語も、
--     この語彙の部分集合であることが以後は構造的に保証される
--
-- ── 語彙の確認(既存行と全書き手)──────────────────────────────────────────
-- 書き手(status に値を入れるコードの全部):
--   - src/ryza/provenance/runs.py `start_run`  … INSERT ... status = 'running'(リテラル)
--   - src/ryza/provenance/runs.py `Run.finish` … UPDATE ... status = %s。渡るのは
--     `finish()` 既定の 'success' と、`run()` コンテキストの例外経路が渡す 'failed'。
--     呼び出し側の全リテラルも 'success' / 'failed' のみ(a18・bot.main・fm.theses・
--     governance.decisions/notices・jobs.daily・preprocess.runner・risk.classify/daily)
--   - src/ryza/ledger/_util.py `create_run` … INSERT。引数 status の既定は 'success' で、
--     明示指定している呼び出し側は無い
--   - migrations/0006_seed.sql / 0011_demo_capital_increase.sql … いずれも 'success'
-- 既存行(2026-08-04 時点): 運用 DB は success / failed のみ、テスト DB は success のみ。
-- `partial` 等の第4の値はコードにもデータにも存在しない。したがって
-- `running|success|failed` の3値で凍結して既存行を落とさない(CHECK は追加時に全行を
-- 検証するので、違反行があれば本 migration 自体が失敗して気付ける —— NOT VALID は
-- 使わない。「入れたのに効いていない」状態を作らないため)。
--
-- ── 語彙を増やしたくなったときの手順(この列を触る人への申し送り)────────────
-- 1. 本 CHECK(`runs_status_check`)を改定する migration を書く。migrations は保護領域
--    (定款第5条 area: schema)なので、独立役員審査 → #承認 通知 → みなし承認の手続を通す
-- 2. **同じ migration で `meta.runs_running_idx`(0027 の索引4)の述語を見直す。**
--    新しい値が「実行中」を意味するなら、述語を `status IN (...)` へ広げたうえで索引を
--    作り直す(部分索引の述語はクエリ側の述語に含意されないと使われない)
-- 3. `dashboard.queries.fetch_running_runs` の `WHERE status = 'running'` を揃える
-- 4. `ryza.provenance.runs.RUN_STATUSES` に新しい値を足す(writer 側の早期検証)
-- この4点は必ず同時に動かす。1 だけを動かすと、0027 が警告していた沈黙劣化がそのまま
-- 再現する(語彙は増えたのに索引とクエリが古い語彙のまま)。

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'runs_status_check'
    ) THEN
        ALTER TABLE meta.runs ADD CONSTRAINT runs_status_check
            CHECK (status IN ('running', 'success', 'failed'));
    END IF;
END
$$;

-- ════════════════════════════════════════════════════════════════════════════
-- データカタログ用コメント(0027 の「述語がドリフトしうる」注記を実態へ更新する)
-- ════════════════════════════════════════════════════════════════════════════
-- stale なカタログを残さない(0019 C-7 / 0021 の教訓)。0027 の索引コメントは
-- 「CHECK 未設定なので語彙変更で無言に使われなくなる」と述べていたが、本 migration で
-- 語彙は凍結された。残る責務は「語彙を変えるときは索引とクエリを同時に見直す」ことである。
COMMENT ON INDEX meta.runs_running_idx IS
    '実行中ジョブ一覧(fetch_running_runs)用の部分索引。終了で自動的に索引から外れる。'
    '述語が依存する status の語彙は 0031 の CHECK(runs_status_check)で凍結済み —— '
    '語彙に無い値は INSERT / UPDATE の時点で拒否されるため、0027 が警告していた'
    '「知らないうちに述語から外れた値が増える」経路は塞がれている。'
    '語彙を増やす変更をするときは、CHECK の改定と本索引の述語、'
    'dashboard.queries.fetch_running_runs の述語を必ず同時に見直す(0031 の手順)。';

COMMENT ON COLUMN meta.runs.status IS
    'running|success|failed(0031 の CHECK runs_status_check で凍結)。'
    'running は start_run が書き、finish が success / failed へ遷移させる'
    '(src/ryza/provenance/runs.py が唯一の遷移元)。'
    '**この語彙は meta.runs_running_idx(0027)の部分索引述語と '
    'dashboard.queries.fetch_running_runs の述語に埋め込まれている。**'
    '値を増やすときは CHECK・索引・クエリ・provenance.runs.RUN_STATUSES を同時に改める。';
