window.RYZA_DATA = {
 "generated_at": "2026-08-02 15:32 JST",
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
   "name": "デモ口座開設(保留)",
   "detail": "執行層実装の2〜3週間前に IBKR のみ申請。それまで自前シミュレータで代替",
   "status": "todo"
  },
  {
   "name": "T-001 DB基盤",
   "detail": "マイグレーション+帳簿制約",
   "issue": 1,
   "status": "done"
  },
  {
   "name": "T-002 会計エンジン",
   "detail": "記帳・締め・財務諸表・照合",
   "issue": 2,
   "status": "done"
  },
  {
   "name": "T-003 証憑・リネージ",
   "detail": "不変保存・改竄検知・遡及クエリ",
   "issue": 3,
   "status": "done"
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
   "state": "CLOSED",
   "labels": [
    "impl"
   ]
  },
  {
   "number": 2,
   "title": "T-002: 会計エンジン(記帳・締め・財務諸表)",
   "state": "CLOSED",
   "labels": [
    "impl"
   ]
  },
  {
   "number": 3,
   "title": "T-003: 証憑ストアとリネージ記録",
   "state": "CLOSED",
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
   "title": "(保留)デモ口座の開設 — 執行層実装が近づいたら IBKR のみ",
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
  },
  {
   "number": 11,
   "title": "GitHub トークンを Secret Manager へ登録(手作業・3分)",
   "state": "OPEN",
   "labels": [
    "user-action"
   ]
  },
  {
   "number": 12,
   "title": "T-004: 週次運用ジョブ ops-weekly(Cloud Run Job + Scheduler)",
   "state": "OPEN",
   "labels": [
    "impl"
   ]
  },
  {
   "number": 13,
   "title": "Discord ブリッジの設定(Webhook 1分 / Bot 10分)",
   "state": "OPEN",
   "labels": [
    "user-action"
   ]
  }
 ],
 "commits": [
  {
   "hash": "8b79420",
   "date": "08/02 15:31",
   "subject": "feat(ledger): 会計エンジン(記帳・日次締め・財務諸表・照合) (T-002)"
  },
  {
   "hash": "6d6fd40",
   "date": "08/02 15:27",
   "subject": "wip(T-002): 会計エンジンの記帳・締め・財務諸表・照合モジュール骨子"
  },
  {
   "hash": "ddbc090",
   "date": "08/02 15:25",
   "subject": "feat(provenance): 証憑ストアとリネージ記録 (T-003)"
  },
  {
   "hash": "68f6296",
   "date": "08/02 15:17",
   "subject": "docs: T-001 レビュー完了(14/14 passed)— 証憑インライン格納の補足と進捗更新"
  },
  {
   "hash": "3eb5b00",
   "date": "08/02 15:14",
   "subject": "docs: E9(多重検定補正: DSR/PBO)追加、E8 の方法論的位置づけ明記、ops-weekly(T-004)発行"
  },
  {
   "hash": "e0b0f0a",
   "date": "08/02 15:13",
   "subject": "feat(db): 5スキーマのマイグレーション基盤と帳簿制約 (T-001)"
  },
  {
   "hash": "9a37dc3",
   "date": "08/02 15:10",
   "subject": "docs: 評価プロトコルに E8(資本スケール&開始時点スイープ)を追加"
  },
  {
   "hash": "4191230",
   "date": "08/02 15:07",
   "subject": "feat(ops): リマインダー・レジストリと週次定例ルーチンの制度化"
  },
  {
   "hash": "0297811",
   "date": "08/02 15:05",
   "subject": "wip(T-001): trade + ledger migrations with integrity triggers"
  },
  {
   "hash": "c550b8f",
   "date": "08/02 15:03",
   "subject": "wip(T-001): migrate runner + meta/market/docs migrations"
  }
 ]
};
