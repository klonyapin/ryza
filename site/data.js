window.RYZA_DATA = {
 "generated_at": "2026-08-03 12:14 JST",
 "phase": "実装フェーズ(T-010 階層0前処理 進行中)",
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
   "status": "done"
  },
  {
   "name": "T-001〜T-003 DB・会計・証憑",
   "detail": "マイグレーション+帳簿制約・記帳/締め/財務諸表・リネージ",
   "status": "done"
  },
  {
   "name": "T-004/T-005 週次ジョブ・統合",
   "detail": "ops-weekly(Cloud Run Job+Scheduler)稼働中",
   "status": "done"
  },
  {
   "name": "T-006 Discord Bot 基盤",
   "detail": "GCE e2-micro 常駐・4チャンネル・承認/KillSwitch・日報",
   "status": "done"
  },
  {
   "name": "T-009 データ取込",
   "detail": "J-Quants/TDnet/EDINET/RSS/FRED/カレンダー",
   "status": "done"
  },
  {
   "name": "T-010 階層0前処理",
   "detail": "重複排除・分類・銘柄タグ・一次重要度・埋め込み",
   "status": "doing"
  },
  {
   "name": "T-011 分析エージェント+市場観",
   "detail": "macro/micro/sentiment/editor・慣性ルール・反証拠テスト",
   "status": "todo"
  },
  {
   "name": "T-007/T-008 朝刊・速報",
   "detail": "毎朝10:00 玲音の朝刊・文体リンター・速報と的中率追跡",
   "status": "done"
  },
  {
   "name": "T-012 取込ソース一括拡張",
   "detail": "EDGAR・e-Stat・海外中銀・国際機関",
   "status": "todo"
  },
  {
   "name": "第1回フル実装監査",
   "detail": "別ベンダー AI によるコード監査(最初の運用可能版の後)",
   "status": "todo"
  },
  {
   "name": "IPS v1.0 枠組み承認(複数プロファイル並走の土台)",
   "detail": "複数プロファイル(aggressive/moderate/conservative…上限なし)並走は決定済み。承認が要るのは枠組み: ガバナンス§2・基準プロファイル値・§7論点2つ。単一IPSへの収束は実弾移行時にデータで判断",
   "status": "todo"
  },
  {
   "name": "戦略・リスク・執行 実装",
   "detail": "動物園・リスクエンジン・状態機械・アダプタ",
   "status": "todo"
  },
  {
   "name": "デモ口座開設(IBKR・保留)",
   "detail": "執行層実装の2〜3週間前に IBKR のみ申請。それまで自前シミュレータで代替",
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
   ],
   "milestone": null
  },
  {
   "number": 2,
   "title": "T-002: 会計エンジン(記帳・締め・財務諸表)",
   "state": "CLOSED",
   "labels": [
    "impl"
   ],
   "milestone": null
  },
  {
   "number": 3,
   "title": "T-003: 証憑ストアとリネージ記録",
   "state": "CLOSED",
   "labels": [
    "impl"
   ],
   "milestone": null
  },
  {
   "number": 4,
   "title": "IPS v1.0 枠組みの承認(複数プロファイル並走の土台。§7 論点2つの判断待ち)",
   "state": "OPEN",
   "labels": [
    "decision"
   ],
   "milestone": "IPS v1.0 枠組み承認(複数プロファイル並走の土台)"
  },
  {
   "number": 5,
   "title": "報道部 詳細設計",
   "state": "CLOSED",
   "labels": [
    "design"
   ],
   "milestone": "T-007/T-008 朝刊・速報"
  },
  {
   "number": 6,
   "title": "リサーチ層 詳細設計",
   "state": "CLOSED",
   "labels": [
    "design"
   ],
   "milestone": "T-011 分析エージェント+市場観"
  },
  {
   "number": 7,
   "title": "GCP: アカウント切替の認証(1分)→ Fable が再構築 → Billing Export(5分)",
   "state": "CLOSED",
   "labels": [
    "user-action"
   ],
   "milestone": null
  },
  {
   "number": 8,
   "title": "(保留)デモ口座の開設 — 執行層実装が近づいたら IBKR のみ",
   "state": "OPEN",
   "labels": [
    "user-action"
   ],
   "milestone": "デモ口座開設(IBKR・保留)"
  },
  {
   "number": 9,
   "title": "ガバナンス基盤: AI 役員の役職資産と役員室チャット",
   "state": "OPEN",
   "labels": [
    "impl",
    "design"
   ],
   "milestone": null
  },
  {
   "number": 10,
   "title": "ステータス表示を運用ダッシュボード(Streamlit)に統合",
   "state": "OPEN",
   "labels": [
    "impl"
   ],
   "milestone": null
  },
  {
   "number": 11,
   "title": "GitHub トークンを Secret Manager へ登録(手作業・3分)",
   "state": "CLOSED",
   "labels": [
    "user-action"
   ],
   "milestone": null
  },
  {
   "number": 12,
   "title": "T-004: 週次運用ジョブ ops-weekly(Cloud Run Job + Scheduler)",
   "state": "CLOSED",
   "labels": [
    "impl"
   ],
   "milestone": null
  },
  {
   "number": 13,
   "title": "Discord ブリッジの設定(Webhook 1分 / Bot 10分)",
   "state": "OPEN",
   "labels": [
    "user-action"
   ],
   "milestone": null
  },
  {
   "number": 14,
   "title": "T-005: 会計エンジンと証憑ストアの統合",
   "state": "CLOSED",
   "labels": [
    "impl"
   ],
   "milestone": null
  },
  {
   "number": 15,
   "title": "第1回フル実装監査(別ベンダー AI によるコード監査)",
   "state": "OPEN",
   "labels": [
    "design"
   ],
   "milestone": "第1回フル実装監査"
  },
  {
   "number": 16,
   "title": "週次ダイジェスト",
   "state": "OPEN",
   "labels": [
    "digest"
   ],
   "milestone": null
  },
  {
   "number": 17,
   "title": "T-006: Ryza Discord Bot 基盤(GCE 常駐)",
   "state": "CLOSED",
   "labels": [
    "impl"
   ],
   "milestone": null
  },
  {
   "number": 18,
   "title": "T-009: データ取込パイプライン(J-Quants/TDnet/EDINET/RSS/カレンダー)",
   "state": "CLOSED",
   "labels": [
    "impl"
   ],
   "milestone": null
  },
  {
   "number": 19,
   "title": "(残: FRED のみ)データ資格情報の登録",
   "state": "CLOSED",
   "labels": [
    "user-action"
   ],
   "milestone": null
  },
  {
   "number": 20,
   "title": "T-012: 取込ソース一括拡張(EDGAR・e-Stat・海外中銀・国際機関)",
   "state": "OPEN",
   "labels": [
    "impl"
   ],
   "milestone": "T-012 取込ソース一括拡張"
  },
  {
   "number": 21,
   "title": "T-010: 階層0前処理パイプライン",
   "state": "CLOSED",
   "labels": [
    "impl"
   ],
   "milestone": "T-010 階層0前処理"
  },
  {
   "number": 22,
   "title": "T-011: 分析エージェント+市場観ステート",
   "state": "CLOSED",
   "labels": [
    "impl"
   ],
   "milestone": "T-011 分析エージェント+市場観"
  },
  {
   "number": 23,
   "title": "テスト隔離の改善: ingest テストが共有 DB の残留データで壊れる",
   "state": "OPEN",
   "labels": [
    "impl",
    "in-progress"
   ],
   "milestone": "T-010 階層0前処理"
  },
  {
   "number": 24,
   "title": "Kill Switch 多段化: /kill(凍結)・/winddown(計画的現金化)・/flatten(緊急清算)",
   "state": "OPEN",
   "labels": [
    "impl"
   ],
   "milestone": "戦略・リスク・執行 実装"
  },
  {
   "number": 25,
   "title": "T-007/T-008: 朝刊パイプライン+速報エンジン",
   "state": "CLOSED",
   "labels": [
    "impl"
   ],
   "milestone": "T-007/T-008 朝刊・速報"
  }
 ],
 "commits": [
  {
   "hash": "9c4823c",
   "date": "08/03 12:13",
   "subject": "chore(site): T-007/T-008 完了を反映"
  },
  {
   "hash": "770740b",
   "date": "08/03 12:13",
   "subject": "style: 増資コメントの行長修正"
  },
  {
   "hash": "509b652",
   "date": "08/03 12:12",
   "subject": "feat(press): 朝刊パイプラインと速報エンジン (T-007/T-008)"
  },
  {
   "hash": "b383d0a",
   "date": "08/03 12:11",
   "subject": "docs(design): IPS v1.3 — 独立レビュー(条件付き承認)の必須条件1〜5を反映"
  },
  {
   "hash": "edca140",
   "date": "08/03 12:08",
   "subject": "wip(press): 純関数テスト（リンター・採点・embed・画像）"
  },
  {
   "hash": "c416e4b",
   "date": "08/03 12:06",
   "subject": "wip(press): 朝刊パイプラインと速報エンジン本体"
  },
  {
   "hash": "56ab7ec",
   "date": "08/03 12:00",
   "subject": "feat(config): 初代4名の FM マンデートを確定・機械可読化"
  },
  {
   "hash": "7aea526",
   "date": "08/03 11:59",
   "subject": "wip(press): 執筆・embed・画像・トピック採点モジュール"
  },
  {
   "hash": "fba3b0b",
   "date": "08/03 11:57",
   "subject": "docs(design): IPS §7 の2論点を解消(2026-08-03 ユーザー決定)"
  },
  {
   "hash": "c92ce78",
   "date": "08/03 11:56",
   "subject": "wip(press): config ローダと文体リンター（L-1〜L-5・L-7・U字判定）"
  }
 ]
};
