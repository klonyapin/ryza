window.RYZA_DATA = {
 "generated_at": "2026-08-03 11:17 JST",
 "phase": "実装フェーズ(T-010 進行中/T-011 待機)",
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
   "name": "詳細設計(データ/報道/リサーチ/IPS)",
   "detail": "5スキーマ・3帳簿・文体リンター・分析エージェント・市場観・IPS v1.0ドラフト",
   "status": "done"
  },
  {
   "name": "GCP セットアップ",
   "detail": "ryza-main・API有効化・Billing Export 済み",
   "issue": 7,
   "status": "done"
  },
  {
   "name": "T-001〜T-003 DB・会計・証憑",
   "detail": "マイグレーション+帳簿制約・記帳/締め/財務諸表・リネージ",
   "issue": 3,
   "status": "done"
  },
  {
   "name": "T-004/T-005 週次ジョブ・統合",
   "detail": "ops-weekly(Cloud Run Job+Scheduler)稼働中",
   "issue": 14,
   "status": "done"
  },
  {
   "name": "T-006 Discord Bot 基盤",
   "detail": "GCE e2-micro 常駐・4チャンネル・承認/KillSwitch・日報",
   "issue": 17,
   "status": "done"
  },
  {
   "name": "T-009 データ取込",
   "detail": "J-Quants/TDnet/EDINET/RSS/FRED/カレンダー",
   "issue": 18,
   "status": "done"
  },
  {
   "name": "T-010 階層0前処理",
   "detail": "重複排除・分類・銘柄タグ・一次重要度・埋め込み",
   "issue": 21,
   "status": "doing"
  },
  {
   "name": "T-011 分析エージェント+市場観",
   "detail": "macro/micro/sentiment/editor・慣性ルール・反証拠テスト",
   "issue": 22,
   "status": "todo"
  },
  {
   "name": "T-007/T-008 朝刊・速報",
   "detail": "毎朝10:00 玲音の朝刊・文体リンター・速報と的中率追跡",
   "status": "todo"
  },
  {
   "name": "T-012 取込ソース一括拡張",
   "detail": "EDGAR・e-Stat・海外中銀・国際機関",
   "issue": 20,
   "status": "todo"
  },
  {
   "name": "第1回フル実装監査",
   "detail": "別ベンダー AI によるコード監査(最初の運用可能版の後)",
   "issue": 15,
   "status": "todo"
  },
  {
   "name": "IPS v1.0 確定",
   "detail": "DD25%・レバ2.0x・集中度20%・政策ミックス。§7論点の判断待ち",
   "issue": 4,
   "status": "todo"
  },
  {
   "name": "戦略・リスク・執行 実装",
   "detail": "動物園・リスクエンジン・状態機械・アダプタ",
   "status": "todo"
  },
  {
   "name": "デモ口座開設(保留)",
   "detail": "執行層実装の2〜3週間前に IBKR のみ申請。それまで自前シミュレータで代替",
   "issue": 8,
   "status": "user"
  },
  {
   "name": "ペーパー運用開始",
   "detail": "日次サイクル(取込→分析→朝刊→取引→記帳)フル稼働",
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
   "state": "CLOSED",
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
    "impl",
    "in-progress"
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
    "impl",
    "in-progress"
   ]
  }
 ],
 "commits": [
  {
   "hash": "63af2bd",
   "date": "08/03 11:16",
   "subject": "wip(preprocess): 階層0前処理モジュール群+migration 0009 (T-010)"
  },
  {
   "hash": "6f1bee7",
   "date": "08/03 03:19",
   "subject": "chore(site): 進捗更新(T-006 完了・Bot 常駐稼働)"
  },
  {
   "hash": "5f1a076",
   "date": "08/03 03:18",
   "subject": "fix(bot): 配送処理をワーカースレッドへ — イベントループ自己デッドロック(heartbeat blocked)の解消"
  },
  {
   "hash": "1f2ecd2",
   "date": "08/03 03:11",
   "subject": "fix(ops): 重複 RestartSec を整理(30秒に統一)"
  },
  {
   "hash": "88c1888",
   "date": "08/03 03:11",
   "subject": "fix(ops): Bot の再起動間隔を30秒に(クラッシュループ時のゲートウェイ・ハンマリング防止)"
  },
  {
   "hash": "232f4c5",
   "date": "08/03 03:07",
   "subject": "fix(bot): daily ループの Run API 誤用も修正"
  },
  {
   "hash": "b2028b6",
   "date": "08/03 03:06",
   "subject": "fix(bot): 起動通知の Run API 誤用を修正(context manager ではなく start_run/finish)"
  },
  {
   "hash": "6d849bc",
   "date": "08/03 03:03",
   "subject": "fix(bot): Secret 取得を stdlib REST 化(proto-plus 互換性クラッシュの回避)"
  },
  {
   "hash": "1c01639",
   "date": "08/03 03:00",
   "subject": "fix(bot): owner_ids プロパティと discord.py 親クラスの衝突を修正、deploy を PG17/冪等化"
  },
  {
   "hash": "4d9a598",
   "date": "08/03 02:46",
   "subject": "feat(bot): Ryza Discord Bot 基盤 (T-006)"
  }
 ]
};
