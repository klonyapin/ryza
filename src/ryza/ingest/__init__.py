"""ingest — データ取込パイプライン（T-009）。

初期スコープのソース群を Cloud Run Jobs 想定のジョブとして実装する。各ジョブは
``python -m ryza.ingest.<source>`` で実行でき、ローカルでも動く。全取込は共通基盤
``ingest.base`` を通り、Run 経由・as_of 付き・原文の証憑保存・content_hash 冪等・
リネージ登録の 5 点を満たす（設計 20-research §2）。

ソース:
- ``jquants``   … 日本株 日足・財務・銘柄マスタ（J-Quants Free プラン）
- ``tdnet``     … 適時開示 RSS（生取込のみ。開示種別分類は T-010）
- ``edinet``    … EDINET v2 書類一覧 + type=5 CSV
- ``news_rss``  … 汎用 RSS（feed URL は config/feeds.yaml）
- ``fred``      … FRED マクロ統計系列（series は config/fred_series.yaml）
- ``calendar``  … 経済カレンダー（静的政策/指標 + J-Quants 決算予定）
- ``freshness`` … ソース別鮮度 SLA 検査 → press.outbox（#運営）へ警告
"""

from __future__ import annotations
