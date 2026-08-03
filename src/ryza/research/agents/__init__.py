"""agents — 4 分析エージェント(macro / micro / sentiment / editor)。

各エージェントは「入力組立(担当キュー + 現在の市場観)→ プロンプト(personas/analyst-*/)
→ scores 検証(JSON Schema)→ research_reports 保存 + リネージ」の純関数的ジョブ
(設計 20-research §4)。共通基盤は ``agents.base``。
"""

from __future__ import annotations
