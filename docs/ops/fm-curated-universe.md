# 手順: curated ユニバースの供給(流動性系タグ)

- 対象: 決定論ルールが付けられない universe タグ(`liquid_equity` 等)の人手供給
- 根拠: reminder `fm-jim-universe-curated-classification`。実装は `src/ryza/risk/classify.py`、定義は `config/universe/*.yaml`
- 実施者: 起案=設計リード、承認=投資委員会(ユーザー)

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

6. **反映する**

   ```
   uv run python -m ryza.risk.classify --curated-universe config/universe/jim-curated.yaml --dry-run  # 読み込み検証のみ
   uv run python -m ryza.risk.classify --curated-universe config/universe/jim-curated.yaml
   ```

   結果は `{"granted": n, "unchanged": n, "revoked": n, "unresolved": [...], "unclassifiable": [...], "source": "curated:..."}`。

   - `unresolved`: 銘柄マスタ(`market.instruments`)に存在しない symbol。取込前の銘柄を先に curate できる一方、綴り間違いを黙って飲み込まないため件数と symbol を返す。**毎回ゼロであることを確認する**
   - `unclassifiable`: ルール分類も既存分類も無い銘柄。タグだけの分類行は作らない(商品・単元の無い分類はゲートで block されるだけ)

7. **反映を確認する**

   ```sql
   SELECT c.instrument_id, i.symbol, c.universe_tags, c.asset_class, c.source, c.as_of
   FROM market.instrument_classification c
   JOIN market.instruments i ON i.instrument_id = c.instrument_id AND i.valid_to IS NULL
   WHERE c.universe_tags && ARRAY['liquid_equity']
   ORDER BY i.symbol;
   ```

## point-in-time(E6)

付与は `upsert_classification` 経由のため、追記オンリー履歴(`market.instrument_classification_history` — 0026)にも同一トランザクションで残る。したがって「いつからその銘柄が `liquid_equity` だったか」は再現でき、**今日付けたタグが過去のリプレイに漏れない**(読出しは bitemporal)。

逆に言えば、**反映を忘れた期間のユニバースは当時も空だった**ものとして扱われる。実測基準へ移行するときも同じで、遡って付け直しても過去のリプレイ結果は変わらない(`created_at` は DB 側が固定する)。

## 改訂

- 基準の変更、または銘柄の追加・削除は `version` を上げ、`content_sha256` を再計算して再承認する。`source` には版と内容ハッシュの両方が入る(`curated:jim-liquid-equity:v1:69827e5059ef`)ため、**同じ v1 の異なる内容**も履歴から区別でき、適用済みリストを監査で一意に復元できる
- 代理基準を使っている間は、**指数の定期見直しのたびに突き合わせる**。追随を忘れると「かつて流動性が高かった銘柄」を持ち続けることになる
