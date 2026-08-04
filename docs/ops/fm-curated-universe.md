# 手順: curated ユニバースの供給(流動性系タグ)

- 対象: 決定論ルールが付けられない universe タグ(`liquid_equity` 等)の人手供給
- 根拠: reminder `fm-jim-universe-curated-classification` / `curated-universe-daily-reconcile`。定義は `config/universe/*.yaml`、反映ロジックは `src/ryza/risk/classify.py`、日次の自動照合は `src/ryza/jobs/daily.py`(`curated` 段)
- 実施者: 起案=設計リード、承認=投資委員会(ユーザー)、**反映=daily(自動)**

## なぜ人手なのか

決定論ルール(`classify_instrument`)は流動性・時価総額系のタグを**付けない**。母集団データ(売買代金の分位など)を要するためであり、タグを緩めて埋めるのは fail-open になる。結果として、タグが供給されるまで Jim のユニバースは空=発注ゼロになる — これは設計どおりの挙動であって、埋めるべき障害ではない。

したがって供給は「基準を決めて、基準を満たす銘柄を列挙し、承認を得る」という手順を踏む。**銘柄を1行足すことは、その FM が売買できる銘柄を1つ増やすこと**である。

## 手順

1. **基準を決める**(`criterion`)。何をもって流動性が高いとするかを、後から機械検証できる形で書く。実測が使えない段階では代理基準でよいが、代理であることと置換課題を明記する
2. **銘柄を列挙する**(`entries`)。各行に `rationale`(なぜ基準を満たすか)を書く。ローダは `rationale` の無い行を拒否する
3. **`manages_tags` を宣言する**。このファイルが正であるタグの集合。config から外れた銘柄のタグは反映時に**剥がされる**(付与だけを config 駆動にすると「config が正」が嘘になる)
4. **内容ハッシュを更新する**。`content_sha256` は `criterion` と全エントリ(symbol・tags・rationale)の正規化ハッシュで、実内容と一致しなければローダが拒否する。同じ `version` のまま中身を差し替えられないようにするための固定である

   ```
   uv run python -c "import yaml,pathlib;from ryza.risk.classify import curated_content_digest as d;\
   r=yaml.safe_load(pathlib.Path('config/universe/jim-curated.yaml').read_text());\
   print(d(r['criterion'], r['entries']))"
   ```

5. **承認を得る**。検査は3段で、どれか1つでも欠けるとローダが拒否する:

   - `approved_at` が **ISO 日付**としてパースできること(自由文は承認日にならない)
   - `approved_by` が **`representative` 固定**(起草者が自分で名乗れない)
   - `content_sha256` が実内容と一致すること

   ただし**承認の正はファイル内の文字列ではない**。`config/universe/**` は `config/governance.yaml` の `protected_areas`(area: mandates)に登録されており、変更コミットには `Approved:` トレーラが要る(A-18-1 が突合する)。YAML の3項目はその写しであり、両輪で「銘柄を足すこと」と「承認済みと書くこと」を同一 PR・無トレーラで行えないようにしている。マンデート自体の変更ではないため定款第3条の3専決には当たらない

6. **反映は daily が自動で行う**(手動 CLI は初回・緊急時のみ)

   マージ後の反映操作は不要である。日次サイクル(`src/ryza/jobs/daily.py` の `curated` 段)が毎朝 `config/universe/*.yaml` を列挙し、承認検査を通ったファイルを `apply_curated_universe` で DB へ照合する。差分が無ければ `unchanged` が増えるだけで、分類履歴(`market.instrument_classification_history`)には**新規行を書かない**(冪等)。

   段の位置は **FM 段(`fm.jim` / `fm.ben`)の直前**(分析段の後)である — 取込 → 前処理 → 分析 → **curated 照合** → FM → 執行/締め → リスク → 朝刊。この位置にあるため、**config の付与・撤回はマージ翌朝の提案から効く**(設計リード裁定 2026-08-04)。撤回は売買母集団を狭める判断であり、1 日遅れて効くのはリスク側に倒れるためである。付与も同時に当日有効になるが、curated 定義の変更は `Approved:` トレーラつきの PR マージを経ているため当日有効で問題ない。

   確認するのは #運営 の実行サマリの `curated` フィールドである:

   ```
   curated  ✅ files=1 granted=0 unchanged=35 revoked=0
   ```

   - `granted`: 新たにタグが付いた銘柄。config を広げた翌日に一度だけ立つ
   - `unchanged`: config と DB が一致している銘柄。平常時はここだけが動く
   - `revoked`: config から消えたためタグを剥がした銘柄。**母集団が狭まった**ことを意味する
   - `unresolved`: 銘柄マスタ(`market.instruments`)に存在しない symbol。取込前の銘柄を先に curate できる一方、綴り間違いを黙って飲み込まないため `<ファイル名>:<symbol>` の形で返す。**毎回ゼロであることを確認する**
   - `unclassifiable`: ルール分類も既存分類も無い銘柄。タグだけの分類行は作らない(商品・単元の無い分類はゲートで block されるだけ)
   - `skipped`: 承認検査に落ちて**反映しなかった**ファイルと理由。承認済みのつもりの config が効いていない状態であり、最優先で調べる

   `revoked` / `unresolved` / `skipped` のいずれかが非ゼロの日は、サマリの行頭が 🚨 になり、#運営 へ専用の警告 embed(「⚠️ curated ユニバース照合」)が別途投入される。daily は例外で止めない — 未承認ファイル 1 件が朝刊・締め・リスクまで巻き添えにするのは過剰だからである。ただし黙殺もしない。

   手動 CLI は残してある。**使うのは初回投入と緊急時(翌朝を待てないとき)だけ**で、実行しても daily の照合結果は変わらない(同じ関数を呼ぶ冪等な操作である):

   ```
   uv run python -m ryza.risk.classify --curated-universe config/universe/jim-curated.yaml --dry-run  # 読み込み検証のみ
   uv run python -m ryza.risk.classify --curated-universe config/universe/jim-curated.yaml
   ```

   結果は `{"granted": n, "unchanged": n, "revoked": n, "unresolved": [...], "unclassifiable": [...], "source": "curated:..."}`。

7. **反映を確認する**(手動 CLI を使ったとき、または警告が出た日)

   ```sql
   SELECT c.instrument_id, i.symbol, c.universe_tags, c.asset_class, c.source, c.as_of
   FROM market.instrument_classification c
   JOIN market.instruments i ON i.instrument_id = c.instrument_id AND i.valid_to IS NULL
   WHERE c.universe_tags && ARRAY['liquid_equity']
   ORDER BY i.symbol;
   ```

## なぜ自動照合なのか(2026-08-04 の教訓)

**「config が正」と宣言することと、config と DB が一致していることは別の主張であり、後者は機構でしか担保できない。** 承認済みの定義ファイルは、それ自体では何の状態も変えない — 誰かが反映操作を実行してはじめて `market.instrument_classification` に届く。反映が人手の一回きりの操作である限り、実行漏れは例外もログも残さず、ただ「タグが付いていない」という**正常に見える状態**として現れる。fail-closed の設計はこの沈黙をさらに深くする。タグが無ければユニバースは空になり、空のユニバースは発注ゼロという設計どおりの挙動を返すからである。

実際に、2026-08-04 09:00 JST の日次サイクル初回実運用で `fm.jim` の universe は 0 だった(実行サマリの記録)。原因は PR #99 で承認済みの `config/universe/jim-curated.yaml` が DB へ反映されていなかったことで、本手順書の CLI を実行する運用ステップが抜け落ちていた。設計リードが同日 09:28 に手動反映し `granted=35` を得ている。承認・マージ・CI はすべて緑であり、どの統制もこの状態を検出していない。

反映漏れよりも危険なのは**撤回**の漏れである。config から銘柄を消す操作は売買母集団を狭める判断であり、それが DB に届かなければ、FM は「もう売買してはならない銘柄」を候補に持ち続ける。付与の漏れは機会損失で済むが、撤回の漏れはリスク側に倒れる。そして両者は運用上まったく同じ形——「誰かが反映を実行しなかった」——で発生する。

したがって反映は日次サイクルの一段として毎日走らせ、config と DB の差分をゼロに保つ。段を FM の前に置くのは、撤回が効くまでの遅れをゼロ日にするためである。冪等性がその前提である。毎日走る照合が履歴に行を積み続ければ、`instrument_classification_history` は日数×銘柄数で膨らみ、「いつタグが変わったか」を履歴から読めなくなる(不変原則4 の point-in-time が壊れる)。`upsert_classification` は内容が同一なら追記しないため、差分の無い日は現在値表の `as_of` だけが進む。

## point-in-time(E6)

付与は `upsert_classification` 経由のため、追記オンリー履歴(`market.instrument_classification_history` — 0026)にも同一トランザクションで残る。したがって「いつからその銘柄が `liquid_equity` だったか」は再現でき、**今日付けたタグが過去のリプレイに漏れない**(読出しは bitemporal)。

逆に言えば、**反映されなかった期間のユニバースは当時も空だった**ものとして扱われる。これは自動照合を入れても消えない性質であり(2026-08-04 の 09:00〜09:28 は実際に空である)、遡って付け直しても過去のリプレイ結果は変わらない(`created_at` は DB 側が固定する)。実測基準へ移行するときも同じである。自動照合が縮めるのは反映漏れの**継続期間**(最大 1 日)であって、過去の穴を埋める手段ではない。

## 改訂

- 基準の変更、または銘柄の追加・削除は `version` を上げ、`content_sha256` を再計算して再承認する。`source` には版と内容ハッシュの両方が入る(`curated:jim-liquid-equity:v1:69827e5059ef`)ため、**同じ v1 の異なる内容**も履歴から区別でき、適用済みリストを監査で一意に復元できる
- 代理基準を使っている間は、**指数の定期見直しのたびに突き合わせる**。追随を忘れると「かつて流動性が高かった銘柄」を持ち続けることになる
