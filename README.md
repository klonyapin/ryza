# Ryza — マルチアセット自動運用システム

「AI が運用する自己進化型ヘッジファンド」を目指す個人プロジェクト。運用会社の組織(フロント/ミドル/バックオフィス+投資委員会+独立監査+研究本部+報道部)を 1人+AI で再現する。

- **状態**: 全体設計 v3.2 確定(2026-08-02)。実装開始。当面は**デモ取引専念**
- **デプロイ先**: GCP(月額 $0〜数ドル構成)
- **開発ステータスサイト(ローカル)**: `python3 site/build.py && python3 -m http.server 8080 -d site` → http://localhost:8080 。同内容は運用ダッシュボードの「開発ステータス」ページでも閲覧可(下記)

## 運用ダッシュボード(ローカル専用)

Streamlit 製の閲覧用ダッシュボード(概況 / 取込 / 報道 / コスト / 市場観 / 開発ステータス)。**ローカル専用 — 公開ホスティングしない**(Cloud Run 公開版は 2026-08-02 に撤去済み)。**読み取り専用** — 書込・操作系 UI は無く、Kill Switch 等の操作は Discord Bot の管轄。

```sh
uv sync --extra dashboard          # 依存導入(streamlit。本体依存には含めない)
.venv/bin/streamlit run dashboard/app.py
```

接続先 DB は環境変数 `RYZA_DATABASE_URL`(既定 `postgresql://ryza:ryza@localhost:5432/ryza`。`docker compose up -d db` で起動)。「開発ステータス」ページは `site/data.js` を表示するため、更新するときは `python3 site/build.py` を先に実行する。
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
