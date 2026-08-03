"""execution — 執行層(T-016)。

00-system-design §9「執行はデモ/実の二系統・同一コードパス」の実装。構成:

- ``broker``: ブローカー抽象(``Broker`` Protocol・``BrokerOrder``/``BrokerResult``)
- ``config``: ``config/execution.yaml``(手数料・スリッページ)のローダ
- ``demo``: ``DemoBroker`` — market.bars の日足で約定をシミュレートする決定論ブローカー
- ``runner``: 執行ループ — status=passed の注文を Broker に流し、
  ``record_execution``(T-014)→ ledger 記帳(``post_fill``)→ 状態遷移
- ``close``: 締め処理 — 執行照合 → MTM/NAV(ledger 既存 API)→ ``risk.nav_daily``

LLM 非関与(不変原則1)。発注経路はコンプライアンスゲート(T-014)通過済みの
注文のみ(orders 行は ``gate_and_record`` が唯一の入口)。
"""
