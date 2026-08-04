# T-029: J-Quants 財務サマリの構造化数値化(fundamentals 昇格)

**アーギュメント**: T-019(Peter=GARP)第一段の決定論スクリーニングは構造化された財務数値を前提とするが、日本側は J-Quants `/v2/fins/summary` の生 JSON が `docs.documents`+証憑に眠ったまま数値系列化されていないので、本タスクはこれを `market.indicators` へ決定論・point-in-time・冪等に昇格し、米国側(EDGAR companyfacts — 取込済み)と対称にする。

## 目的と背景(自己完結)

- T-019 §2-2-1 は「XBRL の数値が使える形で入っていないなら Peter 第一段は実装せず空を返す」と定める。本タスクはその前提を解消する側の実装である
- 現状の日本側財務データ経路(2026-08-04 時点):
  - `src/ryza/ingest/jquants.py` の `ingest_statements` が `/v2/fins/summary` を日次取得し、1 開示 = 1 文書として `docs.documents`(source_name='JQuants', meta.kind='financial_statement')へ冪等取込、生 JSON は `ledger.evidence`(kind='jquants_statement')に保存済み
  - **数値としては読めない**(payload は証憑の中)
- 米国側は `src/ryza/ingest/edgar.py` が companyfacts を `market.indicators`(series_code=`EDGAR:{CIK}:{taxonomy}:{tag}:{unit}`、ts=期末、as_of=filed)へ取込済み。本タスクはこの流儀を日本側に写す
- **EDINET type=5 ZIP のパースはしない**。理由: ①J-Quants サマリで Peter 第一段の足切りに必要な数値(売上・利益・EPS 等)が揃う、②EDINET XBRL 要素 ID のマッピング整備は高コストで、必要になった時点で別起案する方が安い(その場合も証憑 ZIP は不変保存済みでバックフィル可能)

## 1. 設計

### 1-1. 新モジュール `src/ryza/preprocess/fundamentals.py`

- **入力**: `docs.documents` のうち `source_name='JQuants'` かつ `meta->>'kind'='financial_statement'` で、冪等マーカ `meta->>'fundamentals_version'` が現行版と異なる行
- **payload の取得**: 文書にひもづく証憑(`ledger.evidence` kind='jquants_statement')を `EvidenceStore.get()` で読む。リネージ辺(documents→evidence)をたどるか、`raw_ref` 相当のメタから引く — 実装時に `ingest/base.upsert_document` の保存形を確認して最短の経路を選ぶこと
- **出力**: `market.indicators` への追記。書き込みは `ingest/base.py` の revision 対応 upsert(`src/ryza/ingest/base.py:361` 付近の既存ヘルパ)を**再利用**する(訂正開示 = 同一 (series_code, ts) の別 value → revision++ という既存規約に乗る)
- **処理は完全決定論・非 LLM**(不変原則7)。数値の解釈・補完・推定をしない

### 1-2. series_code 設計

```
JQUANTS:{symbol}:{field}:{period_kind}:{basis}
```

- `symbol`: `_normalize_symbol` 済みの銘柄コード(jquants.py と同一関数を使う)
- `field`: config のマッピングで定義した正規化名(例: NetSales, OperatingProfit, Profit, EPS)
- `period_kind`: 開示の当期区分(1Q / 2Q / 3Q / FY — payload の期区分フィールドから決定論で導出)。**会社予想は実績と別 field**(例: FcstNetSales)とし、period_kind は予想対象期
- `basis`: 連結/単体の別(Consolidated / NonConsolidated — payload の該当フィールドから導出)
- `ts` = 当期(または予想対象期)の**期末日**、`as_of` = **開示日時**(DiscDate 系フィールド)。開示時点以外を as_of にしてはならない(不変原則4 — 決算は対象期と開示時点が大きくずれる。T-019 §2-2-3)

### 1-3. フィールドマッピング `config/jquants_fields.yaml`

- payload の実フィールド名(V2 命名)→ 正規化 field 名の写像を **config に置く**(ハードコード禁止 — fm 系 config と同じ流儀)
- 対象フィールド(初版): 売上高・営業利益・経常利益・親会社株主帰属当期純利益・EPS、および同各項目の**会社予想**。BPS・総資産等は Peter 第一段で不要なら含めない(足すのは1行なので欲張らない)
- **各行に根拠コメント必須**(なぜこのフィールドをこの正規化名に写すか、J-Quants の仕様上の名前)。実装前に DB の実 payload を数件サンプルして実フィールド名を確認すること(推測で書かない)
- **fail-closed**: config に無いフィールドは書かない。config にあるが payload に欠測・非数値のものは**その項目だけ skip**(エラーにしない)。skip 件数は実行サマリに集計して出す(静かに欠けさせない)
- 単位換算はしない(J-Quants は円建て。EPS は円/株)

### 1-4. 配線とバックフィル

- **daily 配線**: `src/ryza/jobs/daily.py` の jquants 取込の後段に直列で呼ぶ(`edinet.main` と同じ流儀の CLI エントリポイント `python -m ryza.preprocess.fundamentals`)。preprocess/runner.py のパイプラインには**組み込まない**(あちらは文書前処理=embeddings・分類の責務で、数値系列化は別ジョブとして独立に失敗・再実行できる方がよい)
- **バックフィル**: 同 CLI に `--backfill` を持たせ、既存の全 statement 文書を処理する(冪等マーカがあるので安全に再実行できる)。完了報告にはバックフィル実行結果(処理文書数・書込系列数・skip 集計)を含める
- **リネージ**: 昇格した indicator ごとに `record(conn, run, outputs=[("indicators", indicator_ref(series_code, ts))], inputs=[("documents", doc_id)])`(preprocess/runner.py の既存パターン)。producer_job / run_id / as_of は `Run` が刻む

## 2. データ制約(実装前に読むこと)

1. **四半期値は累計**: J-Quants サマリの四半期開示は累計値である。単四半期化(差分)は**本タスクでやらない** — 生の開示値を忠実に系列化するのが本タスクの契約で、差分化はスクリーナ側(T-019)の責務。ここで加工すると「開示された値」と「系列の値」が乖離し証憑照合が壊れる
2. **データ遅延**: J-Quants の無償プランには財務データの提供遅延がある。本タスクは**取込済み payload の昇格のみ**を行い、遅延の解消はスコープ外(取込側の課題)
3. **銘柄の対応**: symbol の正規化は jquants.py の `_normalize_symbol` を import して使う(重複実装しない)

## 3. 実装物の一覧

| # | パス | 内容 |
|---|---|---|
| 1 | `src/ryza/preprocess/fundamentals.py` | 昇格ジョブ本体+CLI(`--date`/`--backfill`) |
| 2 | `config/jquants_fields.yaml` | フィールドマッピング(根拠コメント付き) |
| 3 | `src/ryza/jobs/daily.py` | jquants 後段への配線(1〜2行) |
| 4 | `tests/preprocess/test_fundamentals.py` | 下記 §4 |
| 5 | `docs/tasks/T-029-jquants-fundamentals.md` | 本仕様書(そのままコミット) |

マイグレーションは**不要**(market.indicators を再利用。新テーブルを作らない)。

## 4. テスト(tests/preprocess/)

モック payload(実フィールド名に合わせたフィクスチャ)で:

1. 正常系: 1 文書 → 期待した series_code / ts / as_of / value の行が書かれる(**as_of が開示日時であることを値で固定**)
2. 欠測 skip: 一部フィールド欠測 → 該当項目のみ skip、他は書かれる、skip が集計される
3. 訂正開示: 同一期の別 value → revision が進む(既存 upsert の規約どおり)
4. 冪等: 同一入力の再実行で書込 0
5. バックフィル: 複数文書の一括処理+冪等マーカ更新
6. リネージ: indicators→documents の辺が張られる

DB は `RYZA_TEST_DATABASE_URL`。ローカル共有 DB の既知失敗は Issue #142 の一覧と照合し、**一覧外の失敗のみ**実装起因を疑う。テストのディレクトリ一括実行は1回に留める(共有 DB 負荷)。CI(クリーン DB)が合否の正。

## 5. 受け入れ基準

1. CI green
2. §4 のテストが全て存在し、意味のあるアサーションを持つ
3. `config/jquants_fields.yaml` の全行に根拠コメント
4. LLM 呼び出しゼロ(決定論のみ)
5. バックフィル実行結果の報告(処理件数・skip 集計)
6. 新規モジュールのため**独立審査エージェントのインスペクション必須**(CLAUDE.md 作業体制)。スキーマ・ゲート・会計・リスクには触れないので保護領域手続(承認トレーラ)は不要だが、審査意見書は docs/reviews に保存する
7. 「制約により実装しない」と判断した項目があれば `ops/reminders.yaml` に機械可読で登録してから完了報告(セッション内の約束は無効)

## 6. 反対意見書(この指示が間違っている場合の理由トップ3)

1. **一次資料は EDINET であり、J-Quants は二次加工である** — 昇格した数値の証憑が「J-Quants の応答 JSON」になり、発行体の開示そのものではない。*反論*: 証憑チェーンは J-Quants 応答まで完備しており(kind='jquants_statement')、Peter 第一段の足切り用途には十分。EDINET XBRL 直読みが必要になれば別起案でき、その際も本タスクの series 設計(source プレフィクス付き)なら併存できる。*代替案*: EDINET type=5 CSV のパーサを先に作る — 要素 ID マッピングの整備コストが高く、T-019 のゲート(2026-08-20)に間に合わせる便益が薄い
2. **market.indicators への銘柄×項目の直積はテーブルの責務超過** — マクロ系列向けのテーブルが数千銘柄×十数項目で肥大する。*反論*: EDGAR companyfacts が既に同じ使い方をしており(銘柄×タグ)、PK 設計(series_code, ts, revision)も同一。専用テーブルへの将来移行はリネージがあるので可能。*代替案*: `market.fundamentals` 新テーブル — スキーマ変更(保護領域)になり手続コストが増すわりに、初版で得る構造上の利点が薄い
3. **会社予想の取込は look-ahead 混入の温床** — 予想値を実績のように読む事故が起きる。*反論*: 実績と別 field 名+as_of=開示日時で分離しており、point-in-time 読出し(`ts <= as_of AND as_of <= 判断時点`)に従う限り安全。むしろ GARP の「予想成長率」に必要。*代替案*: 初版は実績のみ — Peter 第一段の核(PER÷予想成長率)が組めなくなり、T-019 の前提解消にならない
