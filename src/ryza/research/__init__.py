"""research — 分析エージェントと市場観ステート(リサーチ部門・T-011)。

構成(設計 20-research §4〜§7):

- ``llm``: LLM クライアント薄層(構造化出力・階層タグ・コスト記録・リトライ)。
- ``schemas``: 各エージェントの ``scores`` の JSON Schema と最小バリデータ。
- ``agents``: macro / micro / sentiment / editor の 4 分析エージェント。
- ``market_view``: 市場観ステートの決定論的更新規約(慣性・magnitude・スナップショット)。
- ``counterevidence``: 反証拠反転テストのハーネス(追従性の計測・監査 A-13)。

**境界**: LLM は判断材料(scores・更新案)を作るだけ。市場観ステートを変えるのは
``market_view`` の決定論ルールだけ(CLAUDE.md 不変原則1・LLM 直書き禁止)。
"""

from __future__ import annotations
