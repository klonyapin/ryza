# 調査: 技術スタックの現状(2026-08-02 時点)

設計根拠資料。Web 調査による確認結果。

## 時系列DB
- **TimescaleDB**: Timescale 社は2025年6月に TigerData へ社名変更、OSS 開発は活発(2026年5月 v2.27.0、PostgreSQL 18 対応)。Community 版(TSL)はセルフホスト無料で圧縮・連続集計を含む。個人利用に問題なし
- **InfluxDB 3**: OSS の Core 版は単一ノード・コンパクション無し・「直近数日分向け」の制限。長期履歴保持は有償 Enterprise 前提 → **候補外**
- **QuestDB**: 高速だが Postgres 互換は部分的、エコシステムで劣る
- **規模感**: 数百銘柄×分足で年間約4,000万行 → 素の PostgreSQL でも回るスケール。Timescale の価値は圧縮(約90%減)と連続集計の運用省力化
- **推奨: PostgreSQL + TimescaleDB Community**。「1つの DB に全部入る」ことを最優先

## ベクター検索
- **pgvector 0.8.x** が現行(iterative index scan、HNSW 並列ビルド、halfvec)。数百万ベクトルまで単一インスタンスで実用的
- Qdrant 等の専用 DB は数千万ベクトル以上で初めて優位 → 個人規模では**不要**
- 推奨: **pgvector**(価格データとの JOIN が SQL で書ける)

## エージェントオーケストレーション
- **LangGraph**: 2025年10月に 1.0 GA、「2.0まで破壊的変更なし」ポリシー。ただし周辺パッケージで patch に破壊的変更が混入した実績 → バージョンピン留め必須。本番実績最多。MIT
- **CrewAI**: 試作は速いが制御が弱く「試作→LangGraph で本番再実装」パターンが多い → 本番非推奨
- **Claude Agent SDK**: ツールループ・MCP・サブエージェント内蔵だが Claude ロックイン。2026年6月からサブスク利用分が別枠クレジット化
- **自作**: 取引の意思決定フローは固定パイプラインで、動的エージェント協調は初期には不要。素の API 呼び出し+自前ステート管理が最も保守しやすい
- 推奨: **まず自作(素の LLM API)→ 複雑化したら LangGraph 1.x**

## バックテスト
- **Backtrader**: 事実上アーカイブ(数年リリースなし、新しい Python で手動パッチ要)→ **新規採用禁止**
- **zipline-reloaded**: メンテナンスオンリー。日本株はバンドル自作が必要 → 非推奨
- **vectorbt**: OSS 版は Apache 2.0 + Commons Clause(個人・自己運用は無料)。DataFrame を渡すだけで取引所非依存 → **日本株と相性最良**。活発なのは PRO 版($20/月)
- **NautilusTrader**: Rust コア・イベント駆動、バックテストと本番を同一コードで回せる。活発(2026年6月 v1.228.0)だが 1.x Beta で破壊的変更が続く。日本の証券会社アダプタは無く自作要
- 推奨: **vectorbt(探索)→ 執行忠実度が必要になったら NautilusTrader**

## ダッシュボード
- **Streamlit**: Apache 2.0・活発。オンデマンド分析 UI に最適、Python 完結
- **Grafana**: AGPLv3(閲覧利用は実害なし)。監視・アラート用に任意で併用
- **TradingView Lightweight Charts**: Apache 2.0 だが NOTICE 帰属表示+tradingview.com へのリンク表示義務あり。自前データを流せる。「ウィジェット」(埋め込み)は別規約で TradingView 提供データ前提
- 推奨: **Streamlit + Lightweight Charts**。Next.js 自作は要件確定後

## スケジューリング
- 2026年の定説: 「Temporal は個人に過剰、Dagster はデータエンジニアリング向け、新規は Prefect が最小摩擦」
- 推奨: **cron で開始 → リトライ・依存管理が辛くなったら Prefect 3.x**(Apache 2.0、セルフホスト軽量)

## Discord
- 一方向通知(シグナル・約定・エラー)は **Webhook への HTTP POST だけで完結**(常駐不要)
- 双方向操作(照会・Kill Switch)が必要になったら **discord.py 2.6.x**(安定・活発・MIT)

## 選定基準(全体)
1. 運用するプロセス数を最小にする
2. 死んでいるライブラリを避ける(Backtrader、InfluxDB 3 Core OSS)
3. 後から強い方に移行できる道を残す(素Postgres→Timescale、cron→Prefect、自作→LangGraph、vectorbt→Nautilus)

出典 URL は調査元レポート参照(TigerData blog、LangChain release policy、vectorbt license、TradingView free-charting-libraries ほか)
