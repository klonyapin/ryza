# T-016: デモ執行+会計連携(約定シミュレータ→記帳→NAV)

- 起草: 2026-08-03 設計リード / 前提: **T-014 統合後に着手**(T-015 とは並行可)
- 前提知識: CLAUDE.md、00-system-design §9(執行はデモ/実の二系統・同一コードパス)、T-014(orders/executions/positions・gate_and_record)、src/ryza/ledger/(T-002 会計エンジン)、docs/design/00-system-design.md の会計・照合節
- 保護領域との関係: 会計エンジン(ledger)への**書込は既存の仕訳 API 経由のみ**(ledger 本体は変更しない)。執行層自体は保護領域外だが、ブローカー抽象は将来 IBKR 実装が入る境界なので interface を明確に

## 目的

ゲートを通過した注文(status=passed)を**デモ市場で約定させ、会計に記帳し、NAV を更新する**決定論パイプライン。これで「注文案 → ゲート → 約定 → 帳簿 → NAV」がエンドツーエンドで回り、FM エージェント(T-017)を差し込めば売買が始まる。

## 実装

1. **ブローカー抽象** `src/ryza/execution/broker.py` — `Broker` Protocol: `submit(order) -> BrokerResult`。実装は将来 `IBKRBroker`(実)と本タスクの `DemoBroker`(デモ)。同一コードパス原則(00 §9)
2. **DemoBroker** `src/ryza/execution/demo.py` — market.bars の直近終値で約定をシミュレート:
   - market 注文: 終値 × (1 ± slippage)。slippage は決定論(注文額/日次売買代金の関数。パラメータは `config/execution.yaml` 新設 — 根拠コメント付き初期値・E4 全コスト込み評価の入力になる)
   - limit 注文: 当日バーの高安に対して約定判定(guaranteed fill はしない)
   - 手数料: config の率(日本株・米株別。初期値は実在ネット証券の水準を出典付きで)
   - バーが無い銘柄は rejected(理由付き)
3. **執行ループ** `src/ryza/execution/runner.py` — status=passed の注文を broker に流し、BrokerResult を `trading.executions` に記帳 → `apply_execution` で positions 更新 → **ledger 仕訳**(既存 API で: 約定＝証券/現金の振替+手数料費用。evidence_id は execution 行を証憑として登録)→ orders.status を filled/rejected に遷移。1注文=1トランザクション
4. **NAV 更新**: 締め処理 `src/ryza/execution/close.py` — 日次で positions を market.bars 終値で評価し、`risk.nav_daily`(T-015 で新設。T-015 未統合ならここで作る — 二重定義にならないよう統合順を報告で明記)へ book_id×date×nav を記帳。評価差損益の仕訳は ledger の既存流儀に従う(期末評価替え)
5. **daily 配線**: ジョブ順序は 00 §9 どおり「ゲート→執行→会計記帳→照合→NAV 確定」。T-014 の gate ステージの後に execution ステージを追加(注文が無い日は no-op)
6. **照合**: executions と ledger 仕訳の突合関数(件数・金額一致。ブレイクは ops へ通知)— A-2 の基盤

## テスト(tests/execution/)

- DemoBroker の約定・スリッページ・limit 判定・手数料の数値検証 / 執行ループの原子性(仕訳失敗時に executions が残らない)/ 帳簿分離(DEMO_FUND 以外の book への記帳が スキーマ制約で落ちる)/ 照合の一致・不一致検出 / E2E: gate_and_record(pass)→ runner → NAV 更新まで通し

## 受け入れ基準

全テスト+ruff 通過 / LLM 非関与 / config 値のハードコードなし・出典コメント / ledger 本体の変更なし / コミット刻み(broker → demo → runner → close → 配線)。日本語+Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>、push しない
