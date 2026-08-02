# T-011: 分析エージェント+市場観ステート

- 発行日: 2026-08-03 / 依存: T-010 完了
- 必読: docs/design/20-research.md §4〜§5・§7(仕様の正)、docs/design/00-system-design.md §2(LLM/決定論境界)、CLAUDE.md

## ゴール

4分析エージェント(macro/micro/sentiment/editor)と市場観ステートの更新機構を実装する。LLM 呼び出しは構造化出力+コスト記録(Run.add_cost)必須。

## 実装

```
src/ryza/research/llm.py           -- LLM クライアント薄層(構造化出力・階層タグ・run コスト記録・リトライ)
src/ryza/research/agents/macro.py  -- 各エージェント: 入力組立(担当キュー+現在の市場観)→プロンプト(personas/analyst-*/)→scores 検証→research_reports 保存+リネージ
src/ryza/research/agents/micro.py
src/ryza/research/agents/sentiment.py
src/ryza/research/agents/editor.py -- 統合・矛盾検出・市場観更新案(diff)・朝刊トピック候補
src/ryza/research/market_view.py   -- 更新規約の決定論実装: diff 適用・慣性ルール(反転条件 config)・magnitude 算出・日次スナップショット
src/ryza/research/counterevidence.py -- 反証拠反転テストのハーネス(合成反証拠の生成器+反転率カーブ計測。A-13/回帰テスト用)
personas/analyst-macro/system.md 等 -- プロンプト資産(初版)
tests/research/                    -- LLM はモック(構造化出力のフィクスチャ)。市場観規約・慣性・magnitude は決定論の単体テスト
```

- scores スキーマは 20-research §4 の表のとおり(JSON Schema を定義しバリデーション)
- **editor の更新案は提案にすぎず、market_view.py の決定論ルールだけがステートを変更できる**(LLM 直書き禁止)
- input_refs(参照 doc_id)欠落は保存時に拒否

## 受け入れ基準

- [ ] モック LLM でエンドツーエンド(文書→分析→市場観 diff→スナップショット)
- [ ] 慣性ルール: 単一文書での regime 反転が拒否される/複数日の証拠蓄積で通る
- [ ] magnitude 閾値超で速報トリガのイベントが出る(press.outbox はまだ使わず、フックの発火のみ検証)
- [ ] 反証拠ハーネスが反転率カーブを出力する
- [ ] `uv run pytest` 全通過・ruff パス
- 完了コミット: `feat(research): 分析エージェントと市場観ステート (T-011)`+Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
