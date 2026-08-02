# 調査: GCP デプロイとマルチアセットデモ環境(2026-08-02 時点)

設計根拠資料。v2 設計(GCP 化・マルチアセット・デモ専念)の接地に使用。

## A. GCP

### DB の決定的事実
- **Cloud SQL for PostgreSQL に timescaledb 拡張は無い**(任意拡張の追加不可)。AlloyDB も非対応
- Cloud SQL で使えるもの: **pgvector(v0.8.0)、pg_partman(v5.2.4)、pg_cron(v1.6.4)**
- pg_partman + pg_cron で時間パーティションの自動管理は可能。失うのは hypertable・time_bucket・連続集計・列指向圧縮だが、個人規模(〜数億行)なら実用上十分。集計は日次ロールアップで代替
- → **設計判断: TimescaleDB を落とし、素の PostgreSQL + pg_partman + pgvector に変更**(GCE 自前→Cloud SQL の移行路を確保)

### コスト
- **GCE e2-micro 無料枠は健在**(us-west1/us-central1/us-east1 のみ。月全時間+標準PD 30GB 無料)。asia-northeast1 は約$8/月
- Cloud SQL 最小(db-f1-micro)約$7〜10/月(無料枠なし)
- Cloud Run Jobs+Scheduler(3ジョブまで無料)で日次バッチはほぼ$0。ダッシュボードは min-instances=0 でほぼ$0
- **Discord Bot(WebSocket 常駐)を Cloud Run で動かすのは罠**(min-instances=1 で$20〜150/月)。**GCE e2-micro に同居させれば$0**
- Secret Manager 6バージョンまで無料、Artifact Registry 0.5GBまで無料(クリーンアップポリシー必須)

### 推奨構成(月額 $0〜数ドル)
| コンポーネント | サービス |
|---|---|
| 常駐(Discord Bot + PostgreSQL 同居) | GCE e2-micro us-west1 + 30GB PD($0) |
| 日次バッチ | Cloud Run Jobs + Cloud Scheduler(〜$0) |
| ダッシュボード | Cloud Run Service min-instances=0(〜$0) |
| シークレット/イメージ | Secret Manager / Artifact Registry($0) |

予算ができたら DB を Cloud SQL(db-f1-micro〜)へ移行。

## B. マルチアセットのデモ環境(日本居住・無資金)

| 資産クラス | 推奨 | 条件・品質 |
|---|---|---|
| 株・ETF・先物・OP・債券 | **IBKR ペーパー** | ライブ口座開設(審査あり・**入金不要**)でペーパー口座+API(TWS/Web API)。約定は板参照で最も現実的。未購読データは遅延 |
| 米国株(即日開発用) | **Alpaca ペーパー** | メール登録のみ。IBKR 審査待ちの間の開発着手に最適 |
| FX・CFD | **Saxo OpenAPI SIM** | developer.saxo で無料シミュレーション口座($100k 仮想)+REST API。即日 |
| FX(国内代替) | デューカスコピー・ジャパン デモ | 無料だが JForex API(Java SDK) |
| 暗号資産 現物 | **Binance Spot Testnet** | GitHub ログインのみ・口座不要。板は薄く月次リセット(疎通検証用) |
| 暗号資産 デリバ | **Deribit Testnet** | 無料独立アカウント。板は非現実的(疎通検証用) |

### 使えないもの(要注意)
- **OANDA Japan API**: デモ環境はあるがトークン発行に「残高25万円+プロコース+Gold」が必要 → 無資金では不可
- **Bybit**: 日本居住者向けサービス終了(2026年)→ 除外
- **Binance グローバル / Demo Mode**: 日本居住者は口座開設不可
- **Tradovate**: API はライブ口座+$1,000+月$25 → 無資金では不可
- 国内 FX/先物業者で無資金デモ+公開 API は見当たらず

### 横断的示唆
- 約定シミュレーション品質が環境ごとにバラバラ → **ブローカー抽象化レイヤー(注文/ポジション/フィルの共通インターフェース)を最初に切り**、各環境をアダプタとして差す。Testnet 系の非現実的な約定は自前フィルシミュレータで補完
