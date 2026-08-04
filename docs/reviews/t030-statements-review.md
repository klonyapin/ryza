---
review: t030-jquants-statements
reviewed_sha: 4e5b7bf3bd5a02872f61a3d27862f9961f2d1e1d
reviewer: 独立役員(プロンプト分離)
review_date: 2026-08-04
verdict: approve
---

# T-030 審査意見書: J-Quants 財務諸表の daily 配線+バックフィル CLI(PR #148)

**アーギュメント**: 本 PR は指示書 T-030 の §1〜§5 を全項目満たし(daily 後段配線・実効日共用・失敗分離・平日限定バックフィル・冪等・sleep 根拠コメント・`daily.py`/既存関数の不変更)、テストは値レベルの実質的アサーションを持ちローカル共有 DB で 26/26 通過・CI green であるため **approve** とする — ただし statements 失敗が stdout の 1 行にしか現れず機械可読な監視面(daily ジョブの per_source status・meta.runs status)では success のままになる観測性の弱さが 1 件(中)あり、フォローアップ起票を推奨する。

## 検証方法

- worktree `/tmp/t030-review`(detached 4e5b7bf)で `git diff origin/main...HEAD` を精査。変更は指示書 §3 の 3 ファイルのみ(jquants.py +141/-11、test_jquants.py +156/-1、タスク文書新規)。スコープ外変更なし
- テスト実行(共有 DB・1回): `tests/ingest/test_jquants.py` — **26 passed, 89.56s**。失敗なし(Issue #142 の既知失敗一覧を参照する必要なし)
- CI: PR #148 の required check `test` は pass(GitHub Actions run 30913029032)

## 指示書との合致(§1〜§5)

| 要求 | 判定 | 根拠 |
|---|---|---|
| §1 日足の後段で statements 取得 | 合致 | run_daily 内で bars 取込(jquants.py:335-339)の後に fetch_statements → ingest_statements(jquants.py:340-346) |
| §1 実効日 = effective_quote_date 共用・--lag-days 共用 | 合致 | main が丸め済み quote_date を run_daily に渡し(jquants.py:470-481)、statements も同じ quote_date で叩く(jquants.py:343)。テストが URL の date パラメータで直接検証(test_jquants.py:371-395) |
| §1 DailyResult.statements+実行サマリ | 合致 | フィールド追加(jquants.py:310)、main の print に DailyResult 全体が載る(jquants.py:482)。成功時 `{'written','total'}`/失敗時 `{'error':型名+メッセージ}` の判別可能な形 |
| §1 失敗分離(日足を巻き添えにしない) | 合致 | try/except で捕捉し error 記録(jquants.py:342-346)。テストは日足 written=1 維持+DB 実在+error 記録を値でアサート(test_jquants.py:279-303) |
| §2 バックフィル CLI(両方指定時のみ・両端含む・平日のみ) | 合致 | `--backfill-statements-from/to`(jquants.py:437-444)、両方指定時のみ発動(jquants.py:453)、`_weekday_range` は両端含む inclusive ループ+weekday<5(jquants.py:363-376)。off-by-one なし: テストが 2026-05-01(金)〜05-08(金)で平日 6 日・土日不呼び出しを URL レベルで検証(test_jquants.py:323-346) |
| §2 冪等 | 合致 | ingest_statements の DiscDate+DiscNo キーをそのまま利用。再実行 written=0 を値でアサート(test_jquants.py:349-363) |
| §2 sleep+根拠コメント | 合致 | `_BACKFILL_SLEEP_SEC = 1.0`、Free プランの公表レートリミット不在(2026-08-04 時点)を根拠に保守的 1 秒とするコメント(jquants.py:352-357)— 指示書 §2「確認できなければ保守的に 1 秒程度」の指定どおり |
| §2 進捗ログ+合計サマリ | 合致 | 20 日ごと+最終日に 1 行(jquants.py:411-415)、終了時 `{days, written, total}`(jquants.py:418, 461-463) |
| §2 バックフィル未実行のまま PR | 合致 | docs.documents の J-Quants financial_statement は依然 0 件前提(タスク文書記載)。PR に実行痕跡なし |
| §3 daily.py 不変更・既存 2 関数不変更 | 合致 | diff は 3 ファイルのみ。fetch_statements(jquants.py:247-256)・ingest_statements(jquants.py:259-296)への diff ハンクなし |
| §4 テスト 4 項目 | 合致 | 配線(test_jquants.py:251-276)・失敗分離(:279-303)・バックフィル平日/冪等/サマリ(:323-363)・実効日伝播(:371-395)。すべて具体値のアサーション |
| §5 CI green | 合致 | required check pass(上記) |

**テストの実質性**: モック(FakeFetcher, tests/ingest/conftest.py:54-95)は URL 部分一致+呼び出し履歴の素直な実装で、実装に都合よく歪んでいない。特にバックフィルの平日判定は「呼び出し回数=6」と「date=2026-05-02/03 が URL に混入しない」の二重検証(test_jquants.py:342-346)、冪等は r1/r2 の dict 全体一致(test_jquants.py:361-363)、失敗分離は DB の bars 実在まで確認(test_jquants.py:297-303)と、値ベースで堅い。`--no-statements` 相当のスキップも fins/summary 不呼び出しを履歴で検証(test_jquants.py:306-320)— これは指示書反対意見書 §6-2 の「実装してよい・必須ではない」に該当し、過剰実装ではない。

## 所見

### 重大: なし

### 中(1件)

1. **statements 失敗が機械可読な監視面に現れない(stdout の print のみ)**。run_daily が例外を握って error を DailyResult に載せるため、jquants.main は statements 失敗時も 0 を返し(jquants.py:342-346, 476-485)、daily ジョブの per_source サマリは `status: ok, result: 0` になる(src/ryza/jobs/daily.py:198-208 — 例外送出のみが failed)。meta.runs の `ingest.jquants.daily` も success で終わる(src/ryza/provenance/runs.py:227-234)。観測手段は stdout の `print(f"jquants daily ...: {result}")`(jquants.py:482)のログ grep のみ。指示書 §1 が「終了コードは既存の流儀・daily.py に触れない」と明示している以上この設計は指示の範囲内であり approve を妨げないが、「静かに空回りさせない」の趣旨からは、Run.params への error 記録(Run に params パッチ機構あり: runs.py:150 付近)か週次チェックでの docs.documents 件数監視をフォローアップ起票すべき。

### 軽微(4件)

1. **except Exception の広さ**(jquants.py:345)。HTTP 失敗は RuntimeError(jquants.py:104)だが、捕捉は Exception 全体のため ingest_statements 内の実装バグ(TypeError 等)も同経路に飲まれる。例外型名込みで記録されるため無音ではないが、`(RuntimeError, psycopg.Error)` に絞ればバグとデータ源異常を区別できる。
2. **バックフィルフラグの片方のみ指定時、警告なく日次モードへフォールバック**(jquants.py:453 の and 条件)。指示書 §2「両方指定されたときのみ」の文言どおりだが、片方だけ渡した operator は意図せず日次を回すことになる。片方のみなら parser.error が安全。
3. **§4-4 テストの経路が main を通らない**。test_main_effective_date_applies_to_statements(test_jquants.py:371-395)は名前に main とあるが run_daily を直接呼ぶ。`--lag-days` → effective_quote_date → statements の end-to-end は「丸め自体の既存テスト(test_jquants.py:66-95)+quote_date 伝播の本テスト」の合成で分割検証されており論理的には被覆されるが、main の引数処理の結線そのものは未検証。
4. **バックフィル中の 1 日の失敗で全体が中断し、部分サマリが出ない**(jquants.py:401-417 — run_ctx が failed を記録して再送出、合計 print に到達しない)。冪等なので再実行で回復可能であり指示書も失敗許容を要求していないが、数百日運用ではどこまで進んだかが進捗ログ頼みになる。

## 反対意見書(この approve 判定が間違っている場合の理由トップ3)

1. **中所見 1 は request_changes に値する** — 監視の実効性こそ本タスクの核心(「静かに空回りさせない」)であり、stdout print は Cloud Run/VM のログ保持・確認運用に依存する脆い観測面。statements が毎日 403 で空回りしても daily サマリは全 ok で並ぶ。*反論*: 指示書 §1 が exit code の流儀維持と daily.py 不変更を明示指定しており、実装は指示に忠実。観測面の強化は指示書の改訂(=設計リードの判断)を要する事項で、実装 PR の差し戻し理由にするのは審査対象の取り違え。*代替案*: approve のまま、マージ後の初回実走(バックフィル前の 1 回)で DailyResult.statements の実値をログ確認する運用手順を完了報告に含めさせる。
2. **Free プランで /v2/fins/summary が取得可能かが未確認のまま配線している** — 指示書 §6-3 が自認するとおり、403/400 なら本配線は恒久的な空回りで、テストは全モックのためこのリスクを一切検出しない。*反論*: fail-closed(データが増えないだけ)であり、日足と同じ 12 週丸め済み日付を使うためプラン範囲外 400 の主因(直近日付)は回避済み。マージ後に設計リードが行うバックフィルが事実上の実 API 検証を兼ねる。*代替案*: バックフィル実行を「まず 1 週間ぶんの試走→サマリ確認→全範囲」の 2 段にする(CLI は日付範囲指定なので追加実装不要)。
3. **祝日を叩く無駄と、_weekday_range の反転レンジ挙動の非対称** — 祝日(例: テストが使う 2026-05-04〜06 は GW)にも API を叩き、また run_backfill_statements を反転レンジで直接呼ぶと黙って `{days: 0}` を返す(検査は CLI 層のみ: jquants.py:456-457)。*反論*: 祝日除外の不採用は effective_quote_date と同一の明示的設計判断(取引カレンダー依存を持ち込まない: jquants.py:367-368)で、空応答+1 秒 sleep のコストは無視できる。反転レンジの黙殺は「呼び出しゼロ」という無害側に倒れており、公開経路(CLI)では parser.error で防いでいる。*代替案*: 現状維持。ライブラリ関数として他所から呼ばれ始めたら ValueError 化を起案する。

## 結論

verdict: **approve**。指示書の全受け入れ基準を満たし、テストは実質的、スコープ逸脱なし。中所見 1(statements 失敗の機械可読な観測面)のフォローアップ起票を条件ではなく推奨として付す。
