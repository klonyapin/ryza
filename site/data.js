window.RYZA_DATA = {
 "generated_at": "2026-08-03 02:21 JST",
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
   "state": "CLOSED",
   "labels": [
    "user-action"
   ]
  },
  {
   "number": 12,
   "title": "T-004: 週次運用ジョブ ops-weekly(Cloud Run Job + Scheduler)",
   "state": "CLOSED",
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
  },
  {
   "number": 14,
   "title": "T-005: 会計エンジンと証憑ストアの統合",
   "state": "CLOSED",
   "labels": [
    "impl"
   ]
  },
  {
   "number": 15,
   "title": "第1回フル実装監査(別ベンダー AI によるコード監査)",
   "state": "OPEN",
   "labels": [
    "design"
   ]
  },
  {
   "number": 16,
   "title": "週次ダイジェスト",
   "state": "OPEN",
   "labels": [
    "digest"
   ]
  },
  {
   "number": 17,
   "title": "T-006: Ryza Discord Bot 基盤(GCE 常駐)",
   "state": "OPEN",
   "labels": [
    "impl"
   ]
  },
  {
   "number": 18,
   "title": "T-009: データ取込パイプライン(J-Quants/TDnet/EDINET/RSS/カレンダー)",
   "state": "CLOSED",
   "labels": [
    "impl"
   ]
  },
  {
   "number": 19,
   "title": "(残: FRED のみ)データ資格情報の登録",
   "state": "CLOSED",
   "labels": [
    "user-action"
   ]
  },
  {
   "number": 20,
   "title": "T-012: 取込ソース一括拡張(EDGAR・e-Stat・海外中銀・国際機関)",
   "state": "OPEN",
   "labels": [
    "impl"
   ]
  },
  {
   "number": 21,
   "title": "T-010: 階層0前処理パイプライン",
   "state": "OPEN",
   "labels": [
    "impl"
   ]
  },
  {
   "number": 22,
   "title": "T-011: 分析エージェント+市場観ステート",
   "state": "OPEN",
   "labels": [
    "impl"
   ]
  },
  {
   "number": 23,
   "title": "テスト隔離の改善: ingest テストが共有 DB の残留データで壊れる",
   "state": "OPEN",
   "labels": [
    "impl"
   ]
  }
 ],
 "commits": [
  {
   "hash": "f3a5f8d",
   "date": "08/03 02:20",
   "subject": "fix(ingest): J-Quants V2 移行(x-api-key 認証・/v2 エンドポイント)"
  },
  {
   "hash": "0574ee8",
   "date": "08/03 02:10",
   "subject": "feat(ingest): データ取込パイプライン (T-009)"
  },
  {
   "hash": "e54d8b0",
   "date": "08/03 02:09",
   "subject": "docs(tasks): T-010(階層0前処理)・T-011(分析エージェント+市場観)を発行"
  },
  {
   "hash": "e3d99ab",
   "date": "08/03 02:06",
   "subject": "docs: 段階的ソース拡張を撤回(ユーザー指摘)— 一括拡張バッチ T-012 発行、真の後回しはパラダイム別のみ"
  },
  {
   "hash": "aff2624",
   "date": "08/03 02:05",
   "subject": "wip(ingest): カレンダー・鮮度 SLA + import 整理 (T-009)"
  },
  {
   "hash": "9a7f779",
   "date": "08/03 02:03",
   "subject": "wip(ingest): J-Quants/TDnet/EDINET/RSS/FRED ソース (T-009)"
  },
  {
   "hash": "7fb298e",
   "date": "08/03 01:59",
   "subject": "wip(ingest): 取込共通基盤とスキーマ (T-009)"
  },
  {
   "hash": "b635215",
   "date": "08/03 01:58",
   "subject": "docs: 取込ソース拡充(FRED追加・官公庁RSS確定・第2期ソース正式登録・拡張プロセス制度化)、FM ロースター承認"
  },
  {
   "hash": "54eb076",
   "date": "08/03 01:56",
   "subject": "feat(personas): 報道部キャスター玲音の charter・口調ガイド(調査に基づく)"
  },
  {
   "hash": "785caf6",
   "date": "08/03 01:54",
   "subject": "docs: T-009(データ取込)発行、初代 FM ロースター案 v0.1(Ben/Jim/Stan/Peter)"
  }
 ]
};
