window.RYZA_DATA = {
 "generated_at": "2026-08-02 14:53 JST",
 "phase": "実装フェーズ(T-001 進行中)",
 "milestones": [
  {
   "name": "全体設計 v3.2",
   "detail": "組織14部門・二系統会計・独立監査・研究本部・報道部・執筆規格",
   "status": "done"
  },
  {
   "name": "ガバナンス設計",
   "detail": "AI役員の役職資産・役員室・IPS改訂フロー",
   "status": "done"
  },
  {
   "name": "データ基盤+会計 詳細設計",
   "detail": "5スキーマ・3帳簿・証憑・リネージ",
   "status": "done"
  },
  {
   "name": "IPS v1.0",
   "detail": "DD25%・レバ2.0x・集中度20%・政策ミックス。§7論点の判断待ち",
   "issue": 4,
   "status": "todo"
  },
  {
   "name": "GCP セットアップ",
   "detail": "ryza-fund 作成・API有効化済み。Billing Export のみ手作業",
   "issue": 7,
   "status": "done"
  },
  {
   "name": "デモ口座開設",
   "detail": "IBKR / Alpaca / Saxo / Testnet",
   "issue": 8,
   "status": "user"
  },
  {
   "name": "T-001 DB基盤",
   "detail": "マイグレーション+帳簿制約",
   "issue": 1,
   "status": "doing"
  },
  {
   "name": "T-002 会計エンジン",
   "detail": "記帳・締め・財務諸表・照合",
   "issue": 2,
   "status": "todo"
  },
  {
   "name": "T-003 証憑・リネージ",
   "detail": "不変保存・改竄検知・遡及クエリ",
   "issue": 3,
   "status": "todo"
  },
  {
   "name": "報道部 詳細設計",
   "detail": "文体リンター・速報閾値・embed",
   "issue": 5,
   "status": "todo"
  },
  {
   "name": "リサーチ層 詳細設計",
   "detail": "エージェント入出力・市場観ステート",
   "issue": 6,
   "status": "todo"
  },
  {
   "name": "ガバナンス基盤 実装",
   "detail": "personas・議事録スキーマ・役員室チャット",
   "issue": 9,
   "status": "todo"
  },
  {
   "name": "データ取込・リサーチ実装",
   "detail": "J-Quants/EDINET 取込→分析→市場観",
   "status": "todo"
  },
  {
   "name": "報道部・Discord Bot 実装",
   "detail": "朝刊10:00・速報・承認フロー",
   "status": "todo"
  },
  {
   "name": "戦略・リスク・執行 実装",
   "detail": "動物園・リスクエンジン・状態機械・アダプタ",
   "status": "todo"
  },
  {
   "name": "GCP デプロイ・ペーパー運用開始",
   "detail": "e2-micro+Cloud Run、日次サイクル稼働",
   "status": "todo"
  }
 ],
 "issues": [
  {
   "number": 1,
   "title": "T-001: リポジトリ骨格と DB マイグレーション基盤",
   "state": "OPEN",
   "labels": [
    "impl"
   ]
  },
  {
   "number": 2,
   "title": "T-002: 会計エンジン(記帳・締め・財務諸表)",
   "state": "OPEN",
   "labels": [
    "impl"
   ]
  },
  {
   "number": 3,
   "title": "T-003: 証憑ストアとリネージ記録",
   "state": "OPEN",
   "labels": [
    "impl"
   ]
  },
  {
   "number": 4,
   "title": "IPS v1.0 の確定",
   "state": "OPEN",
   "labels": [
    "decision"
   ]
  },
  {
   "number": 5,
   "title": "報道部 詳細設計",
   "state": "OPEN",
   "labels": [
    "design"
   ]
  },
  {
   "number": 6,
   "title": "リサーチ層 詳細設計",
   "state": "OPEN",
   "labels": [
    "design"
   ]
  },
  {
   "number": 7,
   "title": "GCP: アカウント切替の認証(1分)→ Fable が再構築 → Billing Export(5分)",
   "state": "CLOSED",
   "labels": [
    "user-action"
   ]
  },
  {
   "number": 8,
   "title": "デモ口座の開設手順(手作業・IBKR は今日申請推奨)",
   "state": "OPEN",
   "labels": [
    "user-action"
   ]
  },
  {
   "number": 9,
   "title": "ガバナンス基盤: AI 役員の役職資産と役員室チャット",
   "state": "OPEN",
   "labels": [
    "impl",
    "design"
   ]
  },
  {
   "number": 10,
   "title": "ステータス表示を運用ダッシュボード(Streamlit)に統合",
   "state": "OPEN",
   "labels": [
    "impl"
   ]
  }
 ],
 "commits": [
  {
   "hash": "a5d984b",
   "date": "08/02 14:43",
   "subject": "docs: ステータスサイトをローカル配信に変更(Cloud Run 公開版を撤去)"
  },
  {
   "hash": "dd21347",
   "date": "08/02 14:41",
   "subject": "feat(site): 開発ステータスサイト(Cloud Run)— ロードマップ・Issues・変更履歴の可視化"
  },
  {
   "hash": "63a80f6",
   "date": "08/02 14:31",
   "subject": "docs(ips): レバレッジ型ETF禁止の理由を明記"
  },
  {
   "hash": "7385c30",
   "date": "08/02 13:48",
   "subject": "docs(governance): AI役員体制(役職資産による人格永続化)と IPS 改訂フローを設計"
  },
  {
   "hash": "39aa6a2",
   "date": "08/02 12:59",
   "subject": "docs(tasks): 実装指示書 T-001〜T-003 を発行(DB基盤・会計エンジン・証憑/リネージ)"
  },
  {
   "hash": "3ce64ce",
   "date": "08/02 12:56",
   "subject": "docs: 全体設計 v3.2 とデータ基盤+会計詳細設計を確定"
  }
 ]
};
