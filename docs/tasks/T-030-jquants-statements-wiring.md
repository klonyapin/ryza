# T-030: J-Quants 財務諸表取込の daily 配線+日付範囲バックフィル

**アーギュメント**: `src/ryza/ingest/jquants.py` の `ingest_statements` / `fetch_statements` は実装済みだが**どこからも呼ばれておらず**(run_daily は銘柄マスタ+日足のみ)、DB には財務諸表文書が 0 件のまま — T-029(financial サマリの数値昇格)と T-019(Peter=GARP)の前提が空転しているので、本タスクは statements 取得を日次経路に配線し、過去分を日付範囲バックフィルで埋める。

## 目的と背景(自己完結)

- `jquants.run_daily`(src/ryza/ingest/jquants.py)は `ingest_instruments` と `ingest_daily_quotes` のみを実行する。財務(`/v2/fins/summary`)の取得・取込関数(`fetch_statements` / `ingest_statements`)は同ファイルに実装済み・テスト済みだが未配線
- 確認事実(2026-08-04): `docs.documents` に source_name='J-Quants' かつ kind='financial_statement' の行は 0 件、`ledger.evidence` に kind='jquants_statement' は 0 件
- 下流: T-029(`src/ryza/preprocess/fundamentals.py` — PR #147)が statements 文書を `market.indicators` へ昇格する。取込が無ければ昇格対象も無い

## 1. daily 配線

- `run_daily` に statements 取得ステップを追加する: `fetch_statements(fetcher, key, stmt_date=<実効日>)` → `ingest_statements(...)`
- **実効日は `effective_quote_date` を再利用**(Issue #38 の Free プラン 12 週遅延の丸め+土日繰り下げ)。財務データにも同じプラン遅延が適用されるため、日足と同じ丸めでよい。`lag_days` は既存の `--lag-days` 引数を共用する
- `DailyResult` に `statements: dict[str, int]` を追加し、`main` の実行サマリ出力にも載せる(静かに空回りさせない — 取込 0 件が観測できること)
- statements 取得の失敗(HTTP エラー)は**日足取込を巻き添えにしない**: 日足の後に実行し、例外は捕捉してサマリに `error` として記録、終了コードは既存の流儀に従う(他ソースの部分失敗の扱い — `src/ryza/jobs/daily.py` の呼び出し側がどう扱うか確認して整合させること)

## 2. 日付範囲バックフィル

- `main` に `--backfill-statements-from YYYY-MM-DD` / `--backfill-statements-to YYYY-MM-DD` を追加(両方指定されたときのみバックフィルモード)
- from→to の**平日のみ**を日次で `fetch_statements(stmt_date=...)` → `ingest_statements` する。開示が無い日は空リストが返るだけ(エラーにならない)
- 冪等: `ingest_statements` の冪等キー(DiscDate+DiscNo)で再実行安全(既存実装のまま)
- レート制限への配慮: 呼び出し間に小さな sleep(値は定数で持ち、根拠コメントを書く。J-Quants の公表レート制限を確認し、確認できなければ保守的に 1 秒程度)
- 進捗は 20〜30 日ごとに 1 行のログ(数百日を無言で回さない)。終了時に合計(処理日数・written・total)を出す
- **バックフィル自体はこのタスクで実行しない**(マージ後に設計リードが行う)。CLI を作るところまで

## 3. 実装物の一覧

| # | パス | 内容 |
|---|---|---|
| 1 | `src/ryza/ingest/jquants.py` | run_daily への statements 配線+バックフィル CLI |
| 2 | `tests/ingest/test_jquants.py` | 下記 §4 の追加テスト |
| 3 | `docs/tasks/T-030-jquants-statements-wiring.md` | 本仕様書(そのままコミット) |

新規モジュール・スキーマ変更・保護領域なし。`src/ryza/jobs/daily.py` には触れない(jquants.main の内側で完結させる)。

## 4. テスト(tests/ingest/test_jquants.py に追加)

既存のモック Fetcher パターンに従う:

1. run_daily が statements を取得・取込し、結果が DailyResult.statements に載る
2. statements の HTTP エラーが日足取込の成功を巻き添えにしない(日足 written は維持され、statements はエラーとして記録される)
3. バックフィル: 3〜5 日ぶんのモックで平日のみ処理・冪等(再実行で written 0)・合計サマリ
4. 実効日の丸めが statements にも適用される(lag_days の反映)

## 5. 受け入れ基準

1. CI green
2. §4 のテストが存在し実質的なアサーションを持つ
3. sleep 値・丸めの根拠コメント
4. 既存の `fetch_statements` / `ingest_statements` を変更しない(呼ぶだけ。変更が必要になったら理由を完了報告に書く)
5. バックフィルは未実行のまま PR(実行は設計リード)

## 6. 反対意見書(この指示が間違っている場合の理由トップ3)

1. **日次で date 指定取得だと、遅延丸めで同じ開示を何度も見に行く/取りこぼす境界がある** — 丸めが「今日−12週」に張り付くため、日次実行が1日ずつずれて全日をちょうど1回ずつ舐める前提が崩れると欠落日が出る。*反論*: 冪等キーがあるので重複は無害。欠落側はバックフィル CLI で任意範囲を再取得できる。*代替案*: 毎回「前回取得日から実効日まで」を範囲取得する方式 — 状態(前回取得日)の永続化が必要になり初版の複雑度が上がる。まず日次+手動バックフィルで運用し、欠落が観測されたら起案する
2. **statements は日足と独立のジョブにすべき** — 失敗の分離・再実行の粒度から別 CLI が正しい。*反論*: §1 で失敗を分離しており、同一 API・同一認証・同一遅延丸めのソースを2ジョブに割る運用コストの方が高い。*代替案*: `--no-statements` フラグで無効化できるようにしておけば分離の余地は残る(実装してよい・必須ではない)
3. **Free プランの提供範囲確認が先** — `/v2/fins/summary` が Free プランで取得可能かの確認なしに配線しても 403 で空回りする。*反論*: 空回りは §1 のサマリ記録で観測可能であり、fail-closed(データが増えないだけ)。プラン制約が判明したらリマインダー登録して対応する。*代替案*: 実装前に API を手で叩いて確認する — 認証情報は環境にあるので実装エージェントが1回試すのは許容(読み取りのみ)
