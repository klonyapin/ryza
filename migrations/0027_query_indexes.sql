-- 0027_query_indexes.sql
-- ダッシュボード・会計締めの実クエリを支える索引（reminders db-indexes-dashboard、
-- 独立役員審査 T-018 中-10 / risk-navflow-rollforward 再々審査 新-5）。
--
-- **採否は EXPLAIN ANALYZE の実測だけで決めた。** 「効きそう」だけの索引は入れていない
-- （下の「入れなかったもの」を参照）。測定は本番と同じ PostgreSQL 17.10 上の使い捨て DB に
-- 合成データを投入して行った。仕訳の entry_id は entry_date と単調に相関させてある
-- （実運用の追記順。corr=0.9996）。素性が違うと索引の効き方が変わるため。約定仕訳には
-- broker_fill 証憑を 1 件ずつ付けてある（replay_position は payload_ref の JSON を行ごとに
-- 読むので、証憑を共有させると実態と違う軽さになる）。
--
--   規模A（3年運用相当）: journal_entries 31,413 / journal_lines 62,826 /
--                          meta.runs 23,552 / nav_snapshots 785
--   規模B（規模A の 10 倍 = 高頻度運用の 3 年、または通常運用の 30 年相当）:
--                          journal_entries 314,013 / journal_lines 628,026 /
--                          meta.runs 235,503 / nav_snapshots 785
--
-- **索引は追記の副作用を持つ。** 実測した書込の増分（規模B・10,000 行の一括 INSERT を
-- 5 回打った中央値。索引の作成／削除を挟んで「なし→あり」を 3 往復し、ドリフトを排除した。
-- 既存のトリガ（貸借一致・帳簿混合禁止）は両条件に等しく乗っている）:
--   ledger.journal_lines    162〜166 ms → 178〜198 ms（+約 1.5 µs/行、+10%）
--   ledger.journal_entries  60.0〜60.5 ms → 69.3〜70.8 ms（+約 1.0 µs/行、+16%）
--   meta.runs               13.8〜14.8 ms → 18.5〜19.4 ms（+約 0.5 µs/行、+33%）
-- 実運用の 1 日あたり書込量（仕訳明細 70 行・仕訳 35 本・実行記録 30 件）に換算すると
-- それぞれ +0.10 / +0.035 / +0.015 ms/日 である。下で削る読み取り時間（1 回あたり数十〜
-- 数百 ms、しかもダッシュボード表示と日次締めのループで何度も呼ばれる）に対して無視できる。
-- 索引サイズは規模B で journal_lines_book_account_instrument_idx 5.4MB（表 62MB の 8.6%）、
-- journal_entries_book_date_idx 2.4MB（表 28MB の 8.6%）、runs_started_at_idx 5.8MB
-- （表 74MB の 7.9%）、runs_running_idx 16kB。
--
-- 全て CREATE INDEX IF NOT EXISTS。適用は冪等（tests/test_migrations.py が固定）。
-- 索引は宣言的な高速化手段であり、クエリの結果を一切変えない。会計の整合性制約
-- （貸借一致・帳簿混合禁止・追記オンリー）には触れない。

-- ── 1. ledger.journal_lines (book_id, account_id, instrument_id) ──────────────
-- 支える実クエリ（どちらも日次締めのループ内で銘柄ごとに回る）:
--   ryza.ledger._util.held_instruments
--     … WHERE book_id = %s AND account_id = 'securities' AND instrument_id IS NOT NULL
--   ryza.ledger._util.securities_book_value
--     … WHERE jl.book_id = %s AND jl.account_id = 'securities' AND jl.instrument_id = %s
--
-- 列順の根拠: 両クエリとも book_id と account_id を等値で与え、後者はさらに
-- instrument_id を等値で与える。第3列まで入れる理由は実測にある。
--
-- **2列版 (book_id, account_id) が負ける理由は規模依存であり、無条件ではない**
-- （独立役員審査 中-1 の是正。無条件命題として書くと、規模A しか見ていない将来の読者が
-- 「2列で足りる」と誤読して列を落とす経路になる）:
--   規模A … 2列版も**選ばれる**（Bitmap Index Scan）。securities_book_value は
--           4.00 → 2.19 ms（1.8x）改善する（審査再現値）。ただし 3 列版のほうが速い
--   規模B … 2列版は**選ばれなくなる**（39.7 / 37.1 ms = 逐次走査のまま・改善ゼロ。
--           審査再現値 39.3 / 36.5 ms とほぼ一致）。securities はファンド帳簿の明細の
--           約半数を占めるため、表が大きくなるほど 2 列の選択度では逐次走査に負ける
-- 3 列版はどちらの規模でも選ばれ、securities_book_value は索引走査、held_instruments は
-- Index Only Scan（DISTINCT instrument_id が索引だけで解ける）になる。**列を落とす変更は
-- 規模B 以降で無言に索引不使用へ退化する**ため、tests/test_migrations.py が列順を固定する。
--
-- 実測（中央値、7 回。規模A / 規模B）:
--   held_instruments       6.51 → 2.30 ms（2.8x） / 35.8 → 26.8 ms（1.3x）
--   securities_book_value  8.50 → 4.08 ms（2.1x） / 36.6 →  2.9 ms（12.6x）
CREATE INDEX IF NOT EXISTS journal_lines_book_account_instrument_idx
    ON ledger.journal_lines (book_id, account_id, instrument_id);

COMMENT ON INDEX ledger.journal_lines_book_account_instrument_idx IS
    '日次締めの建玉照会（_util.held_instruments / securities_book_value）用。'
    'instrument_id まで含めるのは、2 列版が規模B（明細 60 万行規模）でプランナに'
    '選ばれなくなるため（規模A では 2 列でも選ばれる — 実測）。';

-- ── 2. ledger.journal_entries (book_id, entry_date) ───────────────────────────
-- 支える実クエリ:
--   ryza.ledger._util.replay_position(as_of=...)
--     … WHERE je.book_id = %s AND e.kind = 'broker_fill' AND je.reversal_of IS NULL
--         AND je.entry_date <= %s（+ reversal_of の NOT EXISTS）
--   再締めの評価替え（closing._reapply_mtm）が**銘柄ごとに**呼ぶ経路であり、
--   建玉数 × stale 日数 で回数が伸びる。
--
-- **この索引は独立役員審査が挙げたもの（stale 検出の高速化）とは別の理由で採った。**
-- 審査が想定した効果は実測で否定された — closing._STALE_SNAPSHOTS_SQL は前後で
-- 規模A 778.6 → 766.6 ms・規模B 8,371.1 → 8,184.6 ms（どちらも誤差）でプランも変わらない。
-- 理由は下の「効かない用途」に書く。採用の根拠は replay_position 側の実測で、
-- こちらは索引が実際に使われる（Bitmap Index Scan journal_entries_book_date_idx）。
--
-- 実測（規模B・中央値、7 回）:
--   replay_position(as_of=2023-09-01 = 系列の早い日) 40.9 → 28.3 ms（1.44x）
--   replay_position(as_of=2024-08-04 = 系列の中央)   72.7 → 66〜68 ms（1.06〜1.10x）
-- 効き幅が as_of で変わるのは範囲述語の選択度そのもので、**古い日ほど効く**。再締めが
-- 相手にするのは古いスナップショット日なので、効く側が運用上の主戦場である。
--
-- 効かない用途（この索引に期待してはいけないこと）:
--   closing._STALE_SNAPSHOTS_SQL の水位比較サブクエリ
--       SELECT max(je.entry_id) ... WHERE book_id = %s AND entry_date <= snap.snap_date
--   はプランナが「主キーの逆走査 + フィルタ + LIMIT 1」と見積もり（見積りコスト 0.43）、
--   どんな索引走査よりも安いと判断するため、この索引も INCLUDE (entry_id) 版も 3 列版
--   (book_id, entry_date, entry_id) も選ばれない。実際には古い snap_date ほど新しい仕訳を
--   読み飛ばす距離が伸びるので、コストは スナップショット数 × 仕訳数 で伸びる
--   （索引なしで 規模A 778.6 ms / 規模B 8,371.1 ms = 行数 10 倍で 10.7 倍）。
--   索引はこの構造を変えられない。
--   正しい是正は再々審査 新-5 が併記していた**枝刈り**（全スナップショットの
--   stored_watermark 最大値より後ろの entry_id を持つ最古の entry_date を 1 回求め、その日
--   以降のスナップショットだけを候補にする。事前クエリの実測 0.57 ms）であり、
--   ledger/closing.py は会計エンジン（保護領域）で本 migration の範囲外のため、
--   reminders に reclose-stale-pruning として別項目で登録した。
CREATE INDEX IF NOT EXISTS journal_entries_book_date_idx
    ON ledger.journal_entries (book_id, entry_date);

COMMENT ON INDEX ledger.journal_entries_book_date_idx IS
    '再締めの as_of 建玉復元（_util.replay_position）用。'
    'closing の stale 検出（max(entry_id) の相関サブクエリ）には効かない — '
    'そちらは枝刈りが是正であることが実測で確定している。';

-- ── 3. meta.runs (started_at) ─────────────────────────────────────────────────
-- 支える実クエリ:
--   dashboard.queries.fetch_cost_summary … WHERE started_at >= date_trunc('month', ...)
--   dashboard.queries.fetch_cost_daily   … WHERE started_at >= now() - N days
--   ryza.bot.daily の実行件数集計          … WHERE started_at::date = %s
--
-- 列順の根拠: いずれも started_at 単独の範囲述語。降順指定は不要 — 単一列 B-tree は
-- 逆走査できるので ORDER BY started_at DESC もこの索引で足りる。
--
-- 実測（中央値、7 回。規模A / 規模B）:
--   fetch_cost_summary  7.84 → 0.019 ms（413x） / 49.1 → 0.087 ms（564x。Index Scan）
--   fetch_cost_daily    4.14 → 0.752 ms（5.5x） / 40.2 → 13.0  ms（3.1x。Bitmap Index Scan）
--   bot.daily の件数     1.98 → 1.54  ms（1.3x） / 30.7 → 19.3  ms（1.6x）。
--     started_at::date は sargable でないため索引条件にはならないが、Index Only Scan に
--     なって表への往復が消える分だけ速くなる。
CREATE INDEX IF NOT EXISTS runs_started_at_idx ON meta.runs (started_at);

COMMENT ON INDEX meta.runs_started_at_idx IS
    'コスト集計（fetch_cost_summary / fetch_cost_daily）と日次件数の期間絞り込み用。';

-- ── 4. meta.runs (run_id DESC) WHERE status = 'running' ───────────────────────
-- 支える実クエリ:
--   dashboard.queries.fetch_running_runs … WHERE status = 'running' ORDER BY run_id DESC
--
-- 部分索引にする根拠: 実行中の行は常に数件しかなく、終了時に status が success/failed へ
-- 変わると索引から自動的に外れる。索引本体は 16kB のまま増えない（追記される行の大半は
-- 索引に入らないので書込増分もほぼゼロ）。
--
-- **この索引の述語は語彙に依存しており、沈黙して劣化しうる**（独立役員審査 中-4）。
-- `meta.runs.status` に CHECK 制約は無く（0001）、`running|success|failed` という語彙の
-- 根拠は列コメントだけである。したがって:
--   - `'starting'` / `'retrying'` のような値を後から足す
--   - `fetch_running_runs` の述語を `status IN (...)` に広げる
-- のいずれをやっても、**エラーも警告も出ないまま**この索引は使われなくなる。存在だけを
-- 見るテストでは検出できない（EXPLAIN のプラン検証はフレークするのでテスト化していない）。
-- **status の語彙を変えるときは、この索引と fetch_running_runs の述語を必ず一緒に見直す。**
--
-- press.outbox の outbox_pending_idx（0007）と同型に見えるが**同じではない**: あちらの
-- 述語は `sent_at IS NULL` という構造的条件で、列が存在する限りドリフトしえない。ここは
-- 自由文字列との比較である。恒久的な担保は status に CHECK を置いて語彙を凍結すること
-- （`finished_at IS NULL` という構造的な同値条件も meta.runs にはある）。別 migration の
-- 案件として ops/reminders.yaml の meta-runs-status-check に登録した。
--
-- 実測（中央値、7 回。規模A / 規模B）:
--   fetch_running_runs  2.14 → 0.004 ms（535x） / 28.3 → 0.005 ms（5,660x）
CREATE INDEX IF NOT EXISTS runs_running_idx ON meta.runs (run_id DESC)
    WHERE status = 'running';

COMMENT ON INDEX meta.runs_running_idx IS
    '実行中ジョブ一覧（fetch_running_runs）用の部分索引。終了で自動的に索引から外れる。'
    '述語は status の自由文字列に依存する（CHECK 未設定）— 語彙を変えると無言で'
    '使われなくなるため、status の語彙変更時は本索引と fetch_running_runs を同時に見直す。';

-- ── 入れなかったもの（根拠のない索引を作らないため、実測結果を記録して残す）────────
--
-- 注意: **stale 検出（closing._STALE_SNAPSHOTS_SQL）が索引で直らないという否定的結果は
-- ここには無い**。その索引 (book_id, entry_date) は別の用途（replay_position）で採用済み
-- なので、記述は上の「索引2」の「効かない用途」節にある（独立役員審査 中-3: 参照が
-- 解決できないと不変原則3 のリネージが空手形になる）。
--
-- (A) meta.runs (job_name, run_id DESC)
--     job_name を等値で絞るのは dashboard.queries.fetch_latest_daily_run
--     （job_name = 'jobs.daily'）ただ 1 箇所。jobs.daily は毎日走るので主キーの逆走査が
--     数十行で当たり、索引の有無で 0.007 ms のまま変わらなかった（規模B）。改善が出るのは
--     「長期間走っていないジョブ名を引く」退化ケースだけで（19.7 ms → 0.011 ms）、その
--     20 ms は運用上の問題にならない。一方で索引は 335,503 行に対し 13MB（started_at 索引の
--     2.2 倍）と本セットで最大になる。必要になったら別 migration で足せばよい — 索引を
--     後から足すのは可逆だが、要らない索引を常設して書込に乗せ続けるのは実質不可逆である。
--
-- (B) ryza.risk.navflow.NAV_FLOW_SQL 向けの索引
--     出資フローの抽出は journal_lines を accounts と結合して category='equity' の科目に
--     絞るが、プランナは「どの account_id が equity か」を結合前に知れないため
--     journal_lines の行数を平均選択度で見積もる（実測 13 行に対し見積り 6,107 行）。
--     結果としてどの索引を置いてもハッシュ結合＋逐次走査が選ばれる（規模B: 前後とも
--     58〜61 ms、プラン不変）。結合順を強制すれば 2.4x 速くなる（規模A: 10.5 → 4.4 ms）ので、
--     是正は索引ではなくクエリの書き換えである。src/ryza/risk/ は保護領域のため本
--     migration の範囲外。dashboard 側は @st.cache_data(ttl=60) が既に効いている。
