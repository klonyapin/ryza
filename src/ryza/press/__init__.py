"""press — 報道部（朝刊パイプライン T-007・速報エンジン T-008）。

設計 30-press-discord.md §2〜§4・§6・§7 準拠。責務境界:

- **文体リンター**（``linter``）: 執筆モデルの構造化出力を決定論で検査（§4 の L-1〜L-5・L-7）。
  純関数・LLM 不使用。L-6（タグ整合の抜取）だけは軽量 LLM を使うため別扱い。
- **トピック選定**（``topics``）: 素材（market_view 変化点・research_reports 高スコア・
  カレンダーイベント）から候補を作り、報道価値＝新規性×影響度×確度で採点（採点根拠も保存）。
- **執筆**（``writer``）: StructuredLLM で玲音の口調＋U字構造の記事を構造化出力する。
- **embed / images**（``embeds`` / ``images``）: Discord embed 組立と画像ボードのタグ検索。
- **朝刊**（``morning``）: 素材→候補→採点→上位5→執筆→リンター（再生成≤2）→outbox 投入。
- **速報**（``flash``）: トリガ→軽量判定→執筆→短縮リンター→outbox urgent。予兆は
  ``press.predictions`` へ登録し、期限到来で的中判定する。

**不変原則の反映**（CLAUDE.md）: LLM は判断材料を作る側。採否・レート制御・落板・的中判定は
すべて決定論コードが行い、LLM の確信度を直接ポジション/採否にしない。全 LLM 呼び出しは
``StructuredLLM``（部門タグ ``press``・コスト記録）経由。
"""

from __future__ import annotations
