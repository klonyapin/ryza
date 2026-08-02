# T-010: 階層0前処理パイプライン

- 発行日: 2026-08-03 / 依存: T-009 完了
- 必読: docs/design/20-research.md §3(仕様の正)、docs/design/10-data-accounting.md §3、CLAUDE.md

## ゴール

到着文書(docs.documents)への非LLM 前処理: 重複排除・言語判定・分類・銘柄タグ・一次重要度・埋め込み。結果は documents.meta と embeddings に格納し、重要度の振り分けキューを出力する。

## 実装

```
src/ryza/preprocess/dedup.py       -- content_hash 完全一致+埋め込み近傍の準重複(閾値 config)
src/ryza/preprocess/classify.py    -- 開示種別・ニュースカテゴリ(辞書・正規表現。学習分類器は器だけ用意し初期はルールのみ)
src/ryza/preprocess/tagger.py      -- 銘柄コード・社名辞書マッチ(market.instruments から辞書生成)
src/ryza/preprocess/importance.py  -- 一次重要度(開示種別重み+保有/watchlist+統計異常の併発。ルールは config/importance.yaml)
src/ryza/preprocess/embed.py       -- 軽量埋め込み(ローカル: sentence-transformers 系の小型多言語モデル)→ docs.embeddings
src/ryza/preprocess/runner.py      -- 未処理文書の検出→一括処理→docs.documents.meta 更新、重要度別キュー(DB view)
tests/preprocess/
```

- LLM 呼び出し禁止(階層0)。embed のモデルは requirements に固定し、モデル名・次元を documents.meta に記録
- 判定根拠(どのルールで分類・スコアしたか)を meta に保存(監査 A-13 のサンプル検査対象)
- 冪等: 処理済みマーカー(meta.preprocessed_at + バージョン)。ルール改訂時は再処理可能に

## 受け入れ基準

- [ ] フィクスチャ文書(開示・ニュース各種)で分類・タグ・重要度が期待どおり
- [ ] 準重複(同一内容の別ソース記事)が抑制される
- [ ] 埋め込みが embeddings に入り、類似検索が動く(pgvector)
- [ ] `uv run pytest` 全通過・ruff パス
- 完了コミット: `feat(preprocess): 階層0前処理パイプライン (T-010)`+Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
