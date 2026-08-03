"""fm — ファンドマネージャー(FM)エージェント第一陣(T-017)。

構成(40-fund-managers.md・81-fm-mandates.md):

- ``theses``   … 提案記録 ``trading.fm_theses``(追記オンリー)+ 証憑の point-in-time 検証
- ``sizing``   … 決定論サイジング(スロット制)。**LLM 由来の値を受け取らない**(不変原則1)
- ``base``     … ユニバース・価格・ポジション読出しと「提案 → ゲート → 注文」の共通経路
- ``jim``      … 非 LLM の日次シグナル(20日/60日 SMA クロス+出来高フィルタ)
- ``ben``      … LLM(mid 階層)の週次銘柄選定。出力は候補の**採否**のみでサイズは決めない
- ``config``   … ``config/fm_ben.yaml`` / ``config/fm_jim.yaml`` のローダ

**第一陣は long-only**(設計リード裁定 2026-08-03): ledger が空売りの記帳に未対応の
ため(``execution/runner.py``)、生成する direction は buy / close のみ。short の解禁は
信用記帳 API の実装後、ゲート側の事前遮断と併せて行う。
"""

from __future__ import annotations
