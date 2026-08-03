# T-015: リスクエンジン MVP(DD・実現ボラ・ES・limits_state 執行)

- 起草: 2026-08-03 設計リード / 前提: **T-014 統合後に着手**(trading/risk スキーマに依存)
- 前提知識: CLAUDE.md、00-system-design §9、config/ips.yaml(risk_budget・hard_limits)、src/ryza/ips.py、T-014 の `risk.limits_state`
- **保護領域**(リスクリミット)。統合は設計リードが独立役員審査+みなし承認手続で行う

## 目的

IPS §3.2 のハードリミットを**測定して執行フラグに変換する**決定論エンジン。ゲート(T-014 G-10)が参照する `risk.limits_state` の値を毎日計算する。LLM 不関与。

## スコープ(MVP — 00 §9 の全項目のうちデモ開始に必須の4つ)

1. **DD 追跡**: ips.yaml `drawdown_definition`(帳簿単位・設定来ピーク・連続測定)どおり、帳簿 NAV 系列から現在 DD を計算。`dd_soft_limit`(15%)超で dd_soft、`dd_hard_limit`(25%)超で dd_hard を立てる。**dd_hard の解除はフラグ自動ではなく委員会の明示操作のみ**(ips.yaml の復帰条項)— 解除用の関数は作るが自動では呼ばない
2. **実現ボラ**: `realized_vol_ewma_days`(20日)の EWMA 年率換算を帳簿リターンから計算。`realized_vol_limit`(15%)超で vol_exceeded
3. **日次 ES(95%)**: ヒストリカル法(直近1年の日次リターンにポジションウェイトを適用)+パラメトリック併算(正規仮定・分散から)。大きい方を採用し `daily_es95_nav_max`(3%)超で es_exceeded。ポジションが無い間は 0
4. **リスクレポート**: 日次で上記+ガードレール消費率(集中度・資産クラス・現金・レバの現在値/上限)を ops チャンネルへ embed 1通(00 §9 日次サイクルの「リスクレポート」)。フラグが立った時は urgent

## 実装

- `src/ryza/risk/engine.py` — 純計算(入力: NAV 系列・リターン系列・positions・IPSConfig。出力: RiskState dataclass)。DB 分離(計算はテスト容易な純関数)
- `src/ryza/risk/daily.py` — DB から系列を読み、engine を呼び、`risk.limits_state` を更新(as_of・run_id 付き)、レポート enqueue。`python -m ryza.risk.daily` CLI
- `src/ryza/jobs/daily.py` への配線: 会計締めの後・リスクレポートの位置(00 §9 の順序)。ステージ名 `risk`。**ジョブ配線は既存 daily の流儀(ステージ関数+state 記録)に従う**
- NAV 系列の出所: ledger の NAV(T-002)。まだ日次 NAV スナップショットが無い場合は `ledger` スキーマを確認し、無ければ `risk.nav_daily`(book_id×date×nav、追記)を 0015 で新設して締め時に記録する方式にする(設計判断として報告に明記)
- データ不足時は **fail-safe 側**: 系列が `calibration_horizon` に満たない間はフラグを立てない代わりにレポートに「データ不足 n/20営業日」を明記(IPS の月次レビュー条項と同じ姿勢)。ただし DD はデータ1日目から有効

## テスト(tests/risk/)

- DD・EWMA・ES の数値検証(手計算固定値との一致)/ フラグ境界 / dd_hard の非自動解除 / データ不足時の挙動 / limits_state 更新の冪等性 / ゲート(T-014)との結合: フラグを立てた状態で gate が block すること

## 受け入れ基準

全テスト+ruff 通過 / ips.yaml 値のハードコードなし / LLM 非関与 / 日次ジョブで risk ステージが走り ops へレポートが届く(ローカル DB で確認)/ コミットは engine → daily → 配線で刻む(日本語+Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>、push しない)
