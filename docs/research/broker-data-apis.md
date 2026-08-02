# 調査: ブローカーAPI・市場データAPI(2026-08-02 時点)

設計根拠資料。Web 調査による確認結果。

## 日本株ブローカーAPI

### 立花証券 e支店 API — 第一候補
- 提供継続中。「個人のための無料の日本株API」として公式提供、2026年も更新あり(公開鍵暗号化方式 v4r9、電話番号認証必須化)
- e支店個人口座保有者なら無料。公式デモ環境あり
- JSON ベースの独自 HTTP API。OS・アプリ非依存で Linux + Python + クラウド運用が可能(これが最大の利点)
- 発注・約定/残高照会・リアルタイム株価・板情報・日足20年分・ニュース取得に対応。日本株(現物・信用)のみ
- 出典: https://www.e-shiten.jp/api/ / https://www.e-shiten.net/api/info.html

### 三菱UFJ eスマート証券(旧auカブコム)kabuステーションAPI — 次点
- 提供継続中・活発に更新(2026年2月 最良執行方針対応、2026年5月 国内株手数料無料化対応)
- Professional プラン以上で無料(条件は緩い)。検証用サンドボックスあり(動作確認用)
- **制約: Windows 専用アプリ kabuステーションを常時起動し、同一PCからのみ API 利用可**。レート制限: 発注系 5件/秒、情報系 10件/秒。WebSocket PUSH あり(400ms 間引き)
- 出典: https://kabu.com/item/kabustation_api/default.html / https://github.com/kabucom/kabusapi

### SBI証券 / 楽天証券 — 候補外
- SBI: 個人向け公式発注 API 非公開(2026年時点)。非公式手法は規約リスク
- 楽天: 公式 REST API なし。マーケットスピード II RSS(Excel)経由のみで常時稼働システムに不向き

### Interactive Brokers(IBKR証券・日本法人)
- TWS API / Client Portal API / FIX を個人にも開放。日本居住者は日本法人で口座開設、日本株は JASDEC 登録(約3営業日)が必要
- ペーパートレーディング口座を公式提供
- TWS API は TWS / IB Gateway の常時起動が必要(約50msg/秒上限)
- 出典: https://www.interactivebrokers.co.jp/jp/index.php?f=50761

## 米国株

### Alpaca — 開発・検証用
- ペーパー口座はメールアドレスのみで全世界から無料開設可。本番 API と同一仕様のシミュレーション
- **日本居住者のライブ口座可否は公式に明示なし** → 実弾運用は不可の前提で設計する
- 無料データプランは IEX のみ・200リクエスト/分
- 出典: https://docs.alpaca.markets/us/docs/paper-trading

### IBKR — 本番第一候補
- 日本居住者がライブ口座+API+ペーパーまで確実に使える実質唯一の選択肢。日本株・米国株を同一口座で扱える

## 市場データ

### J-Quants API(JPX公式)— 日本株ヒストリカルの本命
- 2026年1月に CSV 提供と分足・Tick 追加、2026年5月に適時開示書類(TDnet)アドオン(月額11,000円)開始
- プラン: Free ¥0(2年分日足、直近12週除く)/ Light ¥1,650(5年)/ Standard ¥3,300(10年、信用残・指数等)/ Premium ¥16,500(20年)
- **リアルタイム配信ではない** → リアルタイム時価はブローカー API 側で取得する設計が必要
- 出典: https://www.jpx.co.jp/markets/other-data-services/j-quants-api/

### Polygon.io → Massive
- 2025年10月に Massive にリブランド(既存 API キー継続可)。無料5コール/分・15分遅延、本格利用 $199/月〜。米国のみ

### yfinance — 本番不適
- 非公式。429/IP ブロック頻発、Yahoo 側変更で突然壊れる。プロトタイピング補助限定

## 開示・ニュース

- **EDINET API v2**: 稼働中・無料・API キー登録制。type=5 で XBRL の CSV 変換取得可。仕様書2026年6月更新
- **SEC EDGAR API**: 無料・登録不要。10リクエスト/秒(IP単位)、User-Agent に連絡先必須
- **NewsAPI**: 無料枠は100req/日・記事24時間遅延・非商用のみ → トレーディング用途に実質不向き。日本株は TDnet 適時開示 RSS / J-Quants アドオン / 立花 API のニュース取得が実用的

## 結論(推薦)

- 日本株: **立花証券 e支店 API**(Linux/クラウドで完結・無料・デモあり)。次点 kabuステーション API(Windows 常時起動が制約)
- 米国株: **IBKR**(本番)+ **Alpaca ペーパー**(開発・検証)
- データ: **J-Quants**(ヒストリカル・財務)+ ブローカー API(リアルタイム)+ EDINET/EDGAR(開示)
