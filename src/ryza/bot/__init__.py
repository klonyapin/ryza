"""Ryza 本体 Discord Bot(GCE 常駐)。詳細設計 30-press-discord.md §5/§6/§7 準拠。

責務(T-006 基盤):
- ``outbox``     … ``press.outbox`` をポーリング配送(sent_at で冪等・二重送信防止)
- ``approvals``  … 承認 embed+ボタン → ``governance.decisions`` 記録(オーナー ID 検証)
- ``killswitch`` … ``/kill`` ``/resume`` → ``ops.flags`` 更新(遷移は ``ops.flag_events`` に追記)
- ``daily``      … 18:00 JST 日報骨格(当面は稼働状況のみ)
- ``main``       … discord.py 2.6 常駐エントリポイント(トークンは Secret Manager から)

**設計方針**: discord.py に触れる I/O は ``main`` に集約し、``outbox`` / ``approvals`` /
``killswitch`` / ``daily`` は DB とデータのみを扱う純ロジックに保つ。これによりテストは
discord API をモックせず(=依存を入れず)ライブ DB のみで冪等・状態遷移・オーナー検証を検証できる。
"""

from __future__ import annotations

# ── embed 表現仕様(§6)─────────────────────────────────────────────────────
COLOR_NORMAL = 0x5B54C7  # 紫(通常)
COLOR_FLASH = 0xC24E3A   # 赤(速報)
COLOR_APPROVAL = 0x2E7D5B  # 緑(承認)

# 免責フッター(全投稿・§6)
DISCLAIMER = "本投稿は自己運用システムの内部記録であり投資助言ではない"

# outbox の論理チャンネル(§7)。deploy 時に環境変数で実 Discord チャンネル ID へ写像する。
CHANNELS = ("morning", "flash", "approval", "daily", "audit", "mgmt")

# ops.flags のキー
KILL_SWITCH = "kill_switch"

__all__ = [
    "COLOR_NORMAL",
    "COLOR_FLASH",
    "COLOR_APPROVAL",
    "DISCLAIMER",
    "CHANNELS",
    "KILL_SWITCH",
]
