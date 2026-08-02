window.RYZA_DATA = {
 "generated_at": "2026-08-03 01:40 JST",
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
  }
 ],
 "commits": [
  {
   "hash": "8bdbbcd",
   "date": "08/03 01:39",
   "subject": "docs: リサーチ層+情報分析 詳細設計 v1.0(常時稼働・階層0前処理・市場観更新規約・反証拠テスト)"
  },
  {
   "hash": "d82b44c",
   "date": "08/03 01:32",
   "subject": "docs: 報道部設計のチャンネル名残存参照を統合後の構成に整理"
  },
  {
   "hash": "ad761b2",
   "date": "08/03 01:31",
   "subject": "wip(bot): press スキーマ+outbox/approvals/killswitch/daily/main 骨格 (T-006)"
  },
  {
   "hash": "088950f",
   "date": "08/03 01:31",
   "subject": "docs: 報道部設計改訂 — マスコットは外部API取得、チャンネル4統合+指定カテゴリ配下に自動設置"
  },
  {
   "hash": "4573a6c",
   "date": "08/03 01:25",
   "subject": "docs: 報道部+Discord Bot 詳細設計 v1.0、T-006(Bot 基盤)発行"
  },
  {
   "hash": "f6b72f0",
   "date": "08/03 01:19",
   "subject": "fix(ops): デプロイスクリプトの Cloud Build 設定を一時ファイル渡しに修正+進捗更新"
  },
  {
   "hash": "b09cb20",
   "date": "08/02 16:14",
   "subject": "docs: コスト設計機能(FinOps)— ユニットエコノミクス・階層実験・探索の最低保証枠"
  },
  {
   "hash": "85109f3",
   "date": "08/02 16:10",
   "subject": "docs: 研究制度 v2 — 探索/確証モード分離・実験台帳・Lakatos 型プログラム評価(調査に基づく全面改訂)"
  },
  {
   "hash": "c58ad02",
   "date": "08/02 16:02",
   "subject": "docs: マルチマネージャー制(ポッド制)採用 — FM ペルソナ・固定上限撤廃・中央機能唯一の鉄則"
  },
  {
   "hash": "6b05154",
   "date": "08/02 16:00",
   "subject": "docs: 営業期間=1ヶ月制(労働律速/データ律速の2分類)と柔軟な監査(重要性基準・リスクベース・ゼロトレランス)"
  }
 ]
};
