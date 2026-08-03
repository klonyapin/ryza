# Ryza — マルチアセット自動運用システム

「AI が運用する自己進化型ヘッジファンド」を目指す個人プロジェクト。運用会社の組織(フロント/ミドル/バックオフィス+投資委員会+独立監査+研究本部+報道部)を 1人+AI で再現する。

- **状態**: 全体設計 v3.2 確定(2026-08-02)。実装開始。当面は**デモ取引専念**
- **デプロイ先**: GCP(月額 $0〜数ドル構成)
- **開発ステータスサイト(ローカル)**: `python3 site/build.py && python3 -m http.server 8080 -d site` → http://localhost:8080 。同内容は運用ダッシュボードの「開発ステータス」ページでも閲覧可(下記)

## 運用ダッシュボード(Cloud Run + IAP 公開+ローカル)

Streamlit 製の組織ダッシュボード(概況 / 成績 / リスク / ジョブ / コスト / 取込 / 承認・通知 / 報道 / 市場観 / 計画 / 組織 / 規則 / 役員室 / 開発ステータス)。**Cloud Run + IAP で公開**(2026-08-03 代表指示。認証は IAP の許可リストに全面委譲 — アプリ内に認証コードは無い。2026-08-02 に撤去した無認証 Cloud Run 公開版とは異なり、許可した Google アカウントしかアクセスできない)。役員室(議事録の追記のみ)を除き**読み取り専用** — Kill Switch 等の操作は Discord Bot の管轄。

- **デプロイ**: `./ops/deploy-dashboard.sh`(冪等)。**origin が `klonyapin/ryza` を指し、作業ツリーが clean かつ HEAD == origin/main でなければ中断**する(稼働コード=承認済み main。定款第5条)。イメージタグはコミット SHA で、Cloud Run にラベル `code-version` と env `RYZA_CODE_VERSION` として記録される(後者は `meta.runs.code_version` に届く)。Cloud Build → Cloud Run(Direct VPC egress で VM 内 PostgreSQL の内部 IP へ接続)→ IAP 有効化+許可リストを**代表1名へ宣言的に収束** → サービス/プロジェクト双方の IAM で allUsers・allAuthenticatedUsers の invoker を検査 → **未認証 curl で実際に拒否される(401/403/302)ことを確認**(200 なら失敗扱い)
- **`.gcloudignore` はリポジトリに置く**: 無いと gcloud が自動生成して作業ツリーが dirty になり、次回の git ゲートが落ちる。`.gitignore` を変えたら合わせて更新する
- **DB は 2 ロール**: 読取ページ = `ryza_dashboard`(SELECT のみ・`default_transaction_read_only = on`)、役員室の書込 = `ryza_boardroom`(`governance.minutes` / `minute_resolutions` / `stances` の INSERT と `meta.runs` の INSERT/UPDATE のみ)。接続 URL は別々の Secret から別々の env(`RYZA_DATABASE_URL` / `RYZA_BOARDROOM_DATABASE_URL`)で注入する
- **実行 SA**: 専用の `ryza-dashboard@`(付与は対象 Secret の `secretAccessor` のみ。既定 compute SA は使わない)
- **実行タイミング**: PostgreSQL の再起動は設定に実変更があった初回のみ。それでも **09:00 JST 前後(日次サイクル)は避ける**
- **コスト**: min-instances=0 / max-instances=1。**初回アクセスはコールドスタートで数十秒かかる**(許容の設計)
- **可視化の規約(T-018)**: 表示形は `dashboard/viz.py` のヘルパ経由でのみ作る(bullet 型・underwater 図・共通フォーマッタ)。禁止記法は円グラフ・ゲージ・二軸・生 JSON・比較文脈のない単独数値カード・累計 vanity 数値で、根拠と出典は [docs/research/dashboard-visualization-guidelines.md](docs/research/dashboard-visualization-guidelines.md)。概況は 6 ブロック固定の一画面監視面(明細は各詳細ページへ)。**スキーマに無い指標は作らず「未実装」と明示する**
- **計画ページの正**: `config/roadmap.yaml`(curated)。フェーズ・マイルストーンの状態が変わったら**設計リードが PR で更新する**(自動生成しない — 静的な計画に Issues/PR/meta.runs の動的状態を重ねて表示する設計)

ローカル実行(従来どおり。IAP はクラウド側だけの層):

```sh
uv sync --extra dashboard          # 依存導入(streamlit・requests。本体依存には含めない)
./ops/fetch-fonts.sh               # 初回のみ: Noto Sans JP を取得(後述。省略しても動く)
.venv/bin/streamlit run dashboard/app.py   # 必ずリポジトリルートから(.streamlit/config.toml を読む)
```

接続先 DB は環境変数 `RYZA_DATABASE_URL`(既定 `postgresql://ryza:ryza@localhost:5432/ryza`。`docker compose up -d db` で起動)。「開発ステータス」ページは `site/data.js` を表示するため、更新するときは `python3 site/build.py` を先に実行する。

### デザイン(デジタル庁デザインシステム / DADS 準拠)

代表指示 2026-08-03「ページ切替ボタンが小さい/デジタル庁ガイドラインを参考に見直し」への対応。根拠と出典は [docs/research/dads-streamlit-application.md](docs/research/dads-streamlit-application.md)。

- **トークン層は `.streamlit/config.toml`**(公式 API)。色・タイポ・角丸・フォントを DADS の実値で与える。primary = Blue-900 `#0017C1` / 本文 = Solid Gray-900 `#1A1A1A` / 境界 = Gray-420 `#949494`(非テキスト 3:1 の下限)/ 本文 16px / 角丸 8px。差異・超過の色は DADS セマンティック(error-1 `#EC0000` / success-2 `#197A4B` / warning-orange-2 `#C74700`)へ統一し、いずれも白背景で 4.5:1 を満たす。**起動は必ずリポジトリルートから**(Streamlit はカレントディレクトリの `.streamlit/config.toml` しか読まない。Cloud Run 側は Dockerfile が `/app` へ COPY する)
- **CSS 層は `dashboard/dads.py`**。config.toml に対応設定が無い「タップターゲット 44×44 px」「行間(本文 150% / 表 130%)」「フォーカスリング」だけを CSS 注入で補う。**Streamlit の内部 DOM 依存の非公式手段であり、バージョン更新で無言で壊れる**。CI で検査できるのは「CSS ブロックが注入されていること」までで、実寸は人間が実ブラウザで確認するしかない(同 §6-7)
- **フォント**: Noto Sans JP(SIL Open Font License 1.1)の WOFF2 サブセットを **self-host**(`dashboard/static/fonts/`)。CDN 配信にしないのは、閲覧のたびに代表の IP と User-Agent が第三者へ送られるため。取得は `./ops/fetch-fonts.sh`(curl と uv だけ。可変フォント → wght 400/700 → JIS X 0208 サブセット → WOFF2)。**OFL 1.1 は再配布・改変・コミットを許すが、ライセンス全文 `LICENSE-OFL.txt` の同梱が条件**でスクリプトが一緒に取得する。未取得でもアプリは壊れず、OS 同梱の日本語ゴシックへフォールバックする(詳細: [dashboard/static/fonts/README.md](dashboard/static/fonts/README.md))
- **採用しなかったもの**: **JIS X 8341-3:2016 の全面準拠**(スクリーンリーダー対応・スキップリンク・visited リンク色)。理由は (a) 本ダッシュボードが IAP 許可リスト1名の完全非公開ツールで支援技術の利用者が存在せず、(b) 中核の `st.dataframe` が canvas 描画で DOM にテキストを持たないため CSS でも JS でも到達できない(同 §6-1)。実利のない準拠コストを払わないという判断であって「無視してよい」ではなく、利用者が代表1名という前提が変われば再評価する。フレームワーク移行(Streamlit 継続 vs Next.js 系)の比較調査は Phase 5 に登録済み(`ops/reminders.yaml`: `dashboard-framework-evaluation`)
- **設計書(図入り)**: [docs/design/00-system-design.md](docs/design/00-system-design.md) — mermaid 図は GitHub 上でそのまま描画される

## ドキュメント地図

| ドキュメント | 内容 |
|---|---|
| [docs/design/00-system-design.md](docs/design/00-system-design.md) | **全体設計書 v3.2(正)** — 組織・二系統構造・会計・監査・研究・GCP |
| [docs/design/70-writing-standard.md](docs/design/70-writing-standard.md) | 執筆規格(保護領域)— 型習得・Uneven U・引用管理・イントロ/結論の型 |
| [docs/design/10-data-accounting.md](docs/design/10-data-accounting.md) | データ基盤+会計 詳細設計(スキーマ定義) |
| [docs/research/](docs/research/) | 設計根拠の調査資料5本(出典 URL 付き) |
| [CLAUDE.md](CLAUDE.md) | LLM セッション向けの必読事項(不変原則・禁止事項) |

## 絶対に守る基本構造(要約)

1. **取引は二系統**: デモ(当面)/実(将来)。同一コードパス、アダプタで分離
2. **会計は二系統・帳簿3エンティティ**: デモファンド帳簿(架空)/実取引ファンド帳簿(将来)/運営会計帳簿(実費)。混合禁止
3. **LLM は判断材料を作る側**。お金を動かす経路(シグナル合成→サイジング→ゲート→執行→会計)は決定論的コード
4. **全仕訳に証憑必須**、全データ・生成物にリネージ(来歴)
5. **戦略評価は E1〜E7 プロトコル**(カットオフ後評価・buy-and-hold 対照・全コスト込み 等)必須
6. **重要決定は Discord で人間承認**(IPS・政策・予算・戦略昇格・システム変更 PR)
