# T-019: FM 第二陣(Stan・Peter)の実装

- 起草: 2026-08-04 設計リード / 前提: **T-017 の独立審査是正が main に統合済みであること**(第一陣の教訓を土台にするため)
- 役職資産は本タスクに**先行して確定済み**: `personas/fm-stan/`(charter+system)・`personas/fm-peter/`(同)。マンデートは 81 §3 で 2026-08-03 承認済み(`config/mandates/stan.yaml` / `peter.yaml`)
- 前提知識: CLAUDE.md(不変原則1・4・7)、[40-fund-managers.md](../design/40-fund-managers.md)(ロースター・起動順)、[81-fm-mandates.md](../design/81-fm-mandates.md) §3、`personas/fm-stan/charter.md`・`personas/fm-peter/charter.md`、`src/ryza/fm/`(base / sizing / theses / config / ben / jim)、[T-017](T-017-fm-agents.md)、独立審査記録 `docs/reviews/t017-fm-agents-independent-review.md`

## 目的

初代4名(40-fund-managers.md)の残り2名を稼働させ、哲学の直交性(バリュー/統計/**マクロ**/**成長**)を4象限そろえる。第一陣で作った経路(`fm/base.py` の提案 → sizing → thesis 記録 → ゲート)は**そのまま使う**。本タスクで新規に書くのは「候補の作り方」だけである。

## 第一陣の教訓(本タスクで必ず継承する事項)

T-017 とその独立審査で確立した統制は、第二陣で作り直さず**同じ関数を通す**こと。新規モジュールが `base.submit_intents` を迂回して自前で注文を組むのは設計違反(`src/ryza/fm/**` は保護領域 `fm_engine`)。

1. **検疫とデータ境界**(審査 C-3/C-11、`fm-prompt-quarantine`): LLM を使う FM は、外部由来テキスト(`docs.documents` 本文・過去の自分の提案)を `research/prompting.py` のフェンスで囲み、`_FENCE_NOTICE` 相当の注意書きを system 側に置く。検疫済み(`quarantined_open_instruments`)の保有は「thesis が無い」ではなく「**根拠が失われた**」として渡し、最優先の見直し(クローズ or 再引受)を求める。Stan は LLM 型なので `ben.py` の3層防御をそのまま踏襲する
2. **point-in-time の二軸**(審査 C-4/C-16/C-17): ユニバースは常に追記オンリー履歴から `as_of <= 判断時点` かつ `created_at < 判断時点の当日終端` で読む(`base.load_universe`)。実行サマリには必ず `base.universe_pit_status` の結果(`e6_covered`)を載せる。**指標(`market.indicators`)も同じ二軸で読むこと** — `ts <= as_of` だけでは改定後の値を過去に混ぜる(下記 §1-2)
3. **スロット制と重複排除**(審査 C-1/C-8): 数量は `sizing.entry_qty` のみが決める。同一銘柄の重複提案は `base.dedupe_intents` が先頭のみ採用、処理順は「クローズ → 新規建て・各群 instrument_id 昇順」で固定。未約定の通過注文(`load_pending_orders`)もスロット占有として数える。**確信度・スコアを引数に持つ関数を新設しない**(`tests/fm/test_sizing.py` のシグネチャ検査が保護領域テストとして固定している)
4. **fail-closed**(T-017 の設計どおり): 分類の無い銘柄・資産クラス NULL・参照価格欠落・NAV/現金欠落は、埋めずに落とすかゲートに block させる。ユニバースが空なら**空のまま発注ゼロ**にする。タグを緩めて埋めない(`fm-jim-universe-curated-classification` の顛末)
5. **FM ごとに別段**(審査 C-5): `jobs/daily.py` の配線は FM 単位の `_run_stage`(別 savepoint)。1段にまとめない
6. **モデル階層**(不変原則7): まず非 LLM で済むかを検討してから LLM を使う。Peter の**スクリーニングは決定論、絞り込みだけが LLM**(下記 §2)

## 1. Stan(グローバルマクロ・LLM)

### 1-1. シグナル設計案 — 「マクロ指標のレジーム → 配分候補」

Stan の哲学は「非対称なマクロの賭けを、正しい時に張る」である。実装は**二段**にする。

- **第一段(決定論・非 LLM)**: `market.indicators` から観測可能なレジーム記述子を計算する。
  候補(いずれも `config/fm_stan.yaml` にパラメータと根拠コメントを置く。ハードコード禁止):
  - 米イールドカーブ: `FRED:T10Y2Y` の水準と 60 営業日変化(景気先行の標準的指標)
  - 実質金利の方向: `FRED:DGS10` − 期待インフレ(`FRED:T10YIE` は現在 `active: false` のため、有効化を伴う場合は `config/fred_series.yaml` の変更として起票する)
  - 政策金利の方向: `FRED:FEDFUNDS`・ECB MRO(`config/intl_series.yaml`)の直近変化
  - 為替: ECB 参照為替(`EXR/D.JPY.EUR...`)等、**取込済みの系列だけ**を使う
  この段の出力は数値記述子であって配分ではない。**閾値は config に置き、LLM に触らせない**(`market_view.yaml` と同じ流儀)。
- **第二段(LLM・mid 階層 / `dept_tag='fm.stan'`)**: 第一段の記述子と、`research.market_view` の現在の市場観・as_of 以前の文書を入力に、**ユニバース内の候補の採否**(buy / close)と thesis・invalidation・evidence_refs を出させる。数量・比率・確信度はスキーマに入れない(`schemas.py` の `STAN_SCHEMA` は `BEN_SCHEMA` と同じ形にする)。
- **時間の反証条件を必須にする**(charter §義務): 出力スキーマを分割し、`invalidation_md`(自然文)に加えて **`invalidation_horizon_days`(整数)と `invalidation_series`(系列コードの配列)を必須フィールドにする**。自然文からの構文抽出はしない — 「日数と系列コードが文中にあるか」の構文検査は合否判定として脆く、書き方の揺れで静かに全滅する(独立審査 C-2 が Peter について指摘したのと同型の問題)。決定論検査は次の3点:
  1. `invalidation_horizon_days` が 1 以上 **かつ `config/fm_stan.yaml` の上限以下**であること(下記)
  2. `invalidation_series` が空でなく、**全要素が `market.indicators` に実在する系列コード**であること(実在検査。存在しない系列を書いて検査を通すことはできない)
  3. `invalidation_md` が空でないこと
  いずれか欠ければ `rejected` に理由つきで落とす。**これは Stan 固有の追加検証**であり、`ben.py` の `_reject_reason` に相当する位置に置く。
- **時間条件の上限を config で持つこと(必須。独立審査 C-3)**: `invalidation_horizon_days` に上限が無いと「200 営業日以内に…」で検査を通り、事実上の無期限保有になる。哲学から確信度→サイズを切除した結果、**invalidation の緩さが LLM の確信を実効リスクに変換する唯一の残存経路**であるため、ここを開けたままにしない。`config/fm_stan.yaml` に `invalidation.max_horizon_days` を置き、**マンデート `stan.yaml` の `style_reference.holding: days_to_weeks` と整合する営業日数**にする。上限値そのものは実装時の起案でよいが、**「なぜその日数が days_to_weeks なのか」の根拠コメントを必ず書く**(値だけを置かない)。上限超過は緩和ではなく `rejected`。
- 実行頻度は**週次**(マンデートのスタイル参照値=中回転・週数回)。`config/fm_stan.yaml` の `weekday` で指定し、Ben と別の曜日にする(同日に mid 階層の LLM 呼び出しを2本重ねない — コスト平準化)。

### 1-2. データ源の制約(**着手前に必ず読むこと**)

Stan は初代4名の中で**もっともデータが揃っていない**。以下は実装で回避するのではなく、**fail-closed で空を返す**ことで扱う。緩めてはならない。

1. **ユニバースがほぼ空**: `config/mandates/stan.yaml` の universe は `[index_futures, index_etf, fx, rates]` だが、決定論分類(`src/ryza/risk/classify.py`)はこれらのタグを付けない。curated 供給も 2026-08-04 時点で `liquid_equity` のみ(`config/universe/jim-curated.yaml`)。→ **本番のユニバースは空=発注ゼロ**が既定の挙動である。供給は reminder `fm-etf-futures-curated-classification` の管轄であり、本タスクで `config/universe/**` に銘柄を足す場合は**マンデート同格の保護領域**(governance.yaml 注記・審査 C-24)として独立審査+承認トレーラが要る
2. **FX・金利系の建玉経路が無い**: `market.instruments` に FX・金利商品の登録が無く、`ledger`・執行層(`src/ryza/execution/`)も証拠金取引を記帳できない。→ **第一版は現物 ETF(`index_etf`)のみを対象**とし、`fx` / `rates` は「ユニバースに列挙されているが供給が無い」状態を維持する(マンデートは狭める方向のみ有効なので、これは違反ではない)
3. **先物のノーショナルとスロット制が噛み合わない**: `sizing.entry_qty` は「1スロットの金額 ÷ 参照価格」であり、証拠金・想定元本を区別しない。先物に適用すると**想定元本=1スロット**、すなわち実効レバレッジ 1.0x になり、マンデートの `pod_gross_leverage_limit: 3.0` は使われない。→ 第一版はこれを**そのまま受け入れる**(狭める方向)。レバレッジを使いたい場合はサイジングの拡張として別タスクで起案し、独立審査に載せること。**本タスクでレバレッジ倍率を導入しない**
4. **指標の point-in-time**: `market.indicators` は改定を `revision` と `as_of` で表す。読出しは必ず `ts <= as_of AND as_of <= 判断時点` で、同一 `(series_code, ts)` については `as_of` 最大の行を採る(改定前の値をリプレイで再現するため)。`fm/base.py` の `load_prices` と同型のヘルパを `fm/indicators.py`(新規)に置き、純関数のレジーム計算と DB 読出しを分ける(`jim.compute_signal` と同じ分け方 — 数値検証を DB 無しで書けるようにするため)

### 1-3. Stan のサイジング設定

`config/fm_stan.yaml` の `sizing.max_slots` は **4** を起案する(1スロット=仮想資本の 25%)。マンデートのポッド内集中度上限 50% の半分に留め、境界に張り付かない(Ben・Jim と同じ流儀)。`sizing.check_slots` が load 時に検証する。

あわせて `invalidation.max_horizon_days`(§1-1・独立審査 C-3)を **必須項目**として置く。`StanConfig.load` は、値の欠落・非正・過大(例: マンデートの `days_to_weeks` から明らかに乖離する値)を **load 時に `FMConfigError` で落とす**(`JimConfig` が `fast_window >= slow_window` を落とすのと同じ流儀)。実行時ではなく設定読込時に露見させること。

## 2. Peter(GARP・決定論スクリーニング + LLM 絞り込み)

### 2-1. シグナル設計案 — 「決算成長 × バリュエーション」

不変原則7(まず非 LLM)に従い、**母集団の絞り込みは決定論**で行い、LLM は最後の定性判断だけを担う。

- **第一段(決定論スクリーニング・非 LLM)**: マンデートのユニバース(`jp_equity_midcap_cash` / `us_equity_cash`)に対して、財務データから次を計算し閾値で足切りする。閾値と根拠は `config/fm_peter.yaml`。
  - **成長**: 直近4四半期の売上・営業利益の前年同期比(EDINET/EDGAR の取込済み財務。`src/ryza/ingest/edinet.py` / `edgar.py` の格納先を確認して読むこと)
  - **バリュエーション**: 価格(`market.bars`)と利益から PER 相当、および **成長率に対する倍率**(GARP の核。PER ÷ 予想成長率 の類)
  - **足切り**: 成長率下限・倍率上限・赤字除外・データ欠測は**除外**(fail-closed。「不明だから通す」をしない)
  - 出力は「スクリーニングを通った候補と、その計算値」。**順位づけをサイズに使わない**(順位は LLM への提示順にすぎない)
- **第二段(LLM・mid 階層 / `dept_tag='fm.peter'`)**: 通過した候補に対して、as_of 以前の開示・文書を根拠に「何で儲けている会社か(一段落)・何が変わったか・価格は成長に見合うか」を書かせ、採否を出させる。出力スキーマは `BEN_SCHEMA` を基礎に、下記の反証条件フィールドだけを差し替える(数量・確信度のフィールドは持たない)。
- **反証条件を二本要求する — スキーマ分割で担保する**(charter §義務・独立審査 C-2): 自然文 `invalidation_md` を構文解析して「成長側と価格側が両方書かれているか」を判定するのは**合否判定不能**である(書き方の揺れに対して頑健な判定規則が書けず、緩ければ素通り・厳しければ静かに全滅する)。したがって出力スキーマを分割し、**`invalidation_growth_md` と `invalidation_price_md` を別フィールドとして必須**にする。決定論検査は次のとおり:
  1. 両フィールドとも**非空**であること(片方だけの候補は `rejected`)
  2. 各フィールドの最小要件 — `invalidation_growth_md` は**決算由来の数値閾値**への言及(スクリーニングが計算した指標名の集合との照合。自由記述の全文解析はしない)、`invalidation_price_md` は**価格側の指標**(倍率・PER 等、同じく計算済み指標名の集合との照合)への言及があること
  3. 一方の文章に両方を書いて他方を空にする運用は認めない(2で弾かれる)
  `trading.fm_theses` の列は `invalidation_md` 1本のままでよい。**2フィールドは決定論の文字列連結で1本に合成して記録する**(見出しつきの定型連結。マイグレーションは不要)。合成規則はコード内の定数に置き、LLM に組み立てさせない。
  検査は Stan の追加検証と同じ位置(`_reject_reason` 相当)に置く。
- **身近な観察を証憑にさせない**: `evidence_refs` は既存の `EVIDENCE_KINDS`(document / research_report / bar / indicator)のみ。`validate_evidence_refs` が未知 kind を弾くので追加実装は不要だが、system プロンプトで明示すること(charter の禁止事項)。
- 実行頻度は**週次**(中回転)。Ben・Stan と別の曜日にする。

### 2-2. データ源の制約

1. **財務データの整備状況を先に確認すること**: EDINET/EDGAR の取込(T-009/T-012)がどこまで構造化された財務数値を保持しているかによって、第一段の実装可否が決まる。**XBRL の数値が使える形で入っていないなら、本タスクの第一段は「実装せず、空を返す」で正しい**(reminder に残して次へ送る)。文書本文から LLM に数値を読ませて成長率にするのは**やってはならない**(レベル1ファクトの証憑要件と point-in-time を同時に壊す)
2. **中堅株のユニバース供給**: `jp_equity_midcap_cash` タグも curated 供給が無い(Jim の `liquid_equity` と同じ状況)。空=発注ゼロが既定。供給は `config/universe/**`(保護領域)への追加として別途承認を要する
3. **決算の point-in-time**: 決算は「対象期(ts)」と「開示時点(as_of)」が大きくずれる。必ず開示時点で読む。四半期決算を対象期基準で参照すると典型的な look-ahead になる(不変原則4)

### 2-3. Peter のサイジング設定

`config/fm_peter.yaml` の `sizing.max_slots` は **6** を起案する(1スロット=仮想資本の 16.7%)。マンデートのポッド内集中度上限 30% のおよそ半分。

## 3. 実装物の一覧

1. `src/ryza/fm/indicators.py`(新規)— `market.indicators` の point-in-time 読出し + レジーム記述子の**純関数**
2. `src/ryza/fm/stan.py`(新規)— 週次サイクル。`ben.py` の構造(着任プロンプト → 入力 → LLM → 検証 → `base.submit_intents`)を踏襲
3. `src/ryza/fm/screening.py`(新規)— Peter の決定論スクリーニング(**純関数** + DB 読出しを分離)
4. `src/ryza/fm/peter.py`(新規)— 週次サイクル
5. `src/ryza/fm/schemas.py` に `STAN_SCHEMA` / `PETER_SCHEMA` を追加。いずれも `BEN_SCHEMA` を基礎に**サイズ・確信度のフィールドを持たない**。反証条件だけが FM 固有(独立審査 C-2/C-3 — 自然文の構文解析に判定を委ねない):
   - `STAN_SCHEMA`: `invalidation_md` + `invalidation_horizon_days`(整数)+ `invalidation_series`(文字列配列)
   - `PETER_SCHEMA`: `invalidation_growth_md` + `invalidation_price_md`(`invalidation_md` 単体は持たない。記録時に決定論で合成)
6. `src/ryza/fm/config.py` に `StanConfig` / `PeterConfig` を追加(既存の流儀: frozen dataclass + `load` + 値域検証)。`StanConfig` は `invalidation.max_horizon_days` を**必須**とし、欠落・非正を load 時に落とす
7. `config/fm_stan.yaml` / `config/fm_peter.yaml`(新規。全パラメータに根拠コメント)
8. `src/ryza/jobs/daily.py` に `fm.stan` / `fm.peter` 段を追加(**FM ごとに別段** — 教訓5)
9. `config/org.yaml` の `persona:` 行のコメントを「T-019 で作成」に更新済みであること(役職資産は先行済み)

**`src/ryza/fm/**` は保護領域(`fm_engine`)** — 独立役員審査+承認トレーラが必要(定款第5条)。`config/mandates/**` は変更しない(交付済みのものを読むだけ)。

## 4. テスト(tests/fm/)

- `test_indicators.py` — レジーム記述子の数値検証(固定系列 → 期待値)。改定(revision)がある系列で `as_of` を変えるとリプレイ結果が変わることを固定
- `test_screening.py` — 成長×バリュエーションの足切りの数値検証。**欠測が除外side に倒れる**ことを固定(fail-closed)
- `test_stan.py` — FixtureProvider で: スキーマ適合 / evidence 欠落の拒否 / `invalidation_md` 欠落の拒否 / **`invalidation_horizon_days` 欠落の拒否** / **上限超過(`max_horizon_days` + 1)の拒否** / **`invalidation_series` が空・実在しない系列コードの拒否** / ユニバース外の拒否 / as_of 超の evidence 参照の拒否 / 個別株候補の拒否(マンデートの禁じ手)
- `test_peter.py` — 同上(共通分)+ **`invalidation_growth_md` 単独の候補の拒否** / **`invalidation_price_md` 単独の候補の拒否** / 両方揃った候補の通過 / **2フィールドが決定論の定型連結で `fm_theses.invalidation_md` に合成されること**(合成文字列のゴールデン)
- `test_config.py`(既存または新規)— `StanConfig` が `invalidation.max_horizon_days` の欠落・非正を load 時に落とすこと
- `test_sizing.py`(既存・保護領域テスト)に Stan/Peter のスロット設定がマンデートと整合すること(`check_slots`)を追加
- `tests/test_ips.py`(既存・保護領域テスト)は変更不要(マンデートは既に4名分ある)
- `tests/jobs/` — daily の段追加が既存段を壊さないこと(FM 段は独立 savepoint)
- リプレイ(`as_of` 指定・過去日)で一巡動作すること

## 5. 受け入れ基準

1. 全テスト + `ruff check` 通過
2. **サイジング経路に LLM 由来の値が入らない**ことがテストで固定されている(`tests/fm/test_sizing.py` のシグネチャ検査が新規関数にも及ぶこと)
3. Stan・Peter とも**ユニバースが空の環境で例外を出さず、発注ゼロで正常終了**し、実行サマリに `pit_universe`(E6 充足状況)が載る
4. 反証条件の追加検証が **スキーマ分割によって**(自然文の構文解析ではなく)rejected 経路で機械的に効いている — Stan は `invalidation_horizon_days` の必須・`config` 上限・`invalidation_series` の実在検査、Peter は `invalidation_growth_md` / `invalidation_price_md` の両方必須。それぞれ「欠落」「上限超過」「片方だけ」のテストが存在する
5. `config/mandates/` を変更していない。`config/universe/**` を変更する場合は独立審査+承認トレーラがある
6. 実装で「制約により今回は実装しない」と判断した項目(§1-2・§2-2 の該当分)は、**`ops/reminders.yaml` に機械可読で登録**してから完了報告する(セッション内の約束は無効 — CLAUDE.md)
7. コミット刻み(indicators → stan → screening → peter → 配線)。日本語コミットメッセージ + `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`、worktree から push しない

## 6. 反対意見書(この指示が間違っている場合の理由トップ3)

議論規約2に従い、本指示自体への反証を添える。

1. **そもそも第二陣を今動かすべきでない**: Stan のユニバースは空、Peter の財務データも未確認である。「動くが何も発注しない FM」を2体増やすのは、E9(多重検定)の観点では検定数だけを増やして成績評価を悪化させる。**代替案**: 供給(`config/universe/**`・財務の構造化)が済むまで T-019 を保留し、先に第一陣の成績が測れる状態を作る。本指示は「役職資産と設計を先に確定させる」ことでこの批判に部分的に応じているが、実装着手の可否は着手時点のデータ整備状況で再判断してよい。
   **判定主体と時点(独立審査の注文により明記)**: 判定するのは**設計リード**であり、時点は reminder `t019-fm-second-gen-impl` の期日(2026-08-20)である。判定材料は同 reminder の body に列挙した2点(①`config/universe/**` に `index_etf` / `jp_equity_midcap_cash` の供給があるか、②EDINET/EDGAR の構造化財務が使える形で入っているか)。着手を見送る場合は reminder の `date` を更新し、見送りの理由を同ファイルに残す(判断の履歴を消さない)
2. **Stan の哲学から "サイズ" を切除すると、残るものが Jim と大差ない**: 非対称性をスロットの採否と反証条件の鋭さだけで表現するなら、それは「トレンドフォロー+早い損切り」であり、マクロの物語は装飾になりかねない。哲学の直交性(40 §狙い)が名目だけになる恐れがある。**代替案**: マクロ記述子を FM の入力にせず、中央のリスク配分(ポッド間の資本再配分)の入力にする — すなわち Stan をポッドではなく**リスク管理部の入力**として設計し直す。
   **この代替案を採らなかった理由**: 40-fund-managers.md(ロースター)と 81-fm-mandates.md(交付済みマンデート)の改訂を伴う **L3 の制度変更**であり、実装指示書(L4)のスコープ外だからである。採るなら CIO 起案 → 独立役員審査 → みなし承認の経路に載せる別件として起票すること
3. **決定論スクリーニング(Peter §2-1)の閾値が過適合の入口になる**: 成長率下限・倍率上限は「良さそうな値」を人が選ぶことになり、E5/E9 の検証手続を経ない。Jim のパラメータが「最適化を経ていない既定値をあえて使う」と宣言している(`config/fm_jim.yaml`)のと同じ規律が要る。**代替案**: 閾値を分位点(ユニバース内の相対順位)で定義し、絶対値のチューニングを不可能にする
