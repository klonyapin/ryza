# 開発品質の制度調査 — 開発管理規程(07-development.md)の設計根拠

- 作成: 2026-08-03(調査: 5並列エージェント、出典は各節)/ 経緯: 代表指示「全変更 PR 化・ISO 参考・SQuBOK 調査・開発部門の設置」
- 採用原則: 定款第6条 — **執行点(CI・PR テンプレート・監査・エージェントプロンプト)に落とせない要求は採用しない**

一人+AI エージェントの開発組織には、ISO 9001 の要求事項を認証なしで借用し、29110(超小規模組織プロファイル)でテーラリングし、SQuBOK を技法カタログ、42001/5338 を AI 統制の参照とする構成が最適である。

## 1. ISO 9001:2015 — 借用する要求事項(認証は取らない)

- **8.3.4 の三分割**: レビュー(要求を満たす能力の段階評価)/検証(アウトプット=インプット要求の確認)/妥当性確認(実用途で使えるか)。[解説](https://davidbarker.consulting/iso9001/clause-8-3-4-design-and-development-controls/)
- **8.5.6 変更の管理**: 変更のレビュー結果と**変更を許可した者**の記録保持。[解説](https://davidbarker.consulting/iso9001/clause-8-5-6-control-of-changes/)
- **10.2 是正処置**: 封じ込め→原因分析→**水平展開**(類似の不適合の探索)→処置→**有効性レビュー**。[解説](https://www.iso9001help.co.uk/10.2-Nonconformity-and-Corrective-Action.html)
- **7.5 文書化した情報**: 版管理・承認・保護(→ 定款第4条が既に実装)。一人組織でも認証可能だが内部監査(9.2)の独立性が最難点で外部委託が標準解 — Ryza は独立役員・監査ペルソナのプロンプト分離が代替する。[一人組織の実務](https://iso-specialist.com/iso-9001-for-one-man-businesses/)
- 判断: **認証は非採用**(審査費・外部監査委託費が恒常発生し、便益がない)。要求事項のみ借用

## 2. PR フロー ⇔ 規格要求の対応(実務裏付けあり)

- PR 説明文+Issue=変更依頼の文書化、人間レビュー=影響評価、マージ=承認権限、CI=実装検証、PR タイムライン=監査証跡([GitHub 公式](https://github.blog/enterprise-software/governance-and-compliance/demonstrating-end-to-end-traceability-with-pull-requests/)、[IEC 62304 実務](https://intuitionlabs.ai/articles/git-workflows-fda-compliance))
- 実例: Tidepool(OSS のまま FDA Class II+ISO 13485 認証。ただし record of truth は Issue トラッカー側で Git は証跡層)。[事例](https://elisa.tech/blog/2026/07/15/from-pull-request-to-patient-safety-how-tidepool-built-an-open-source-quality-management-system/)
- **重要な前提**: 「PR 承認=レビュー記録」は**規程で明文定義して初めて監査証拠になる**([OpenRegulatory](https://openregulatory.com/articles/quality-management-system-qms-in-github-gitlab))。また PR 単体を唯一の記録とせず上位の機械可読記録(Ryza では governance.decisions・A-13)から参照する
- CI=verification は PR 単位のユニット/統合検証であり、リリース検証(validation)は別途定義が必要

## 3. ISO/IEC 29110(VSE)— テーラリングの骨格

- 25人以下の組織向けに 12207 をプロファイル化。Basic プロファイル(29110-5-1-2:2025)は **PM 4アクティビティ**(計画/実行/評価管理/終結)+**SI 6アクティビティ**(開始/要求分析/設計/構築/統合テスト/納入)に約22成果物。[プレビュー](https://cdn.standards.iteh.ai/samples/82669/53c2b3e0d89b4126aea1556f6bd3c522/ISO-IEC-29110-5-1-2-2025.pdf)、[JISA 導入の手引き(日本語・無料)](https://www.jisa.or.jp/Portals/0/report/30-J011.pdf)
- **転用**: Ryza の実装指示書(docs/tasks/T-0xx)= Statement of Work+Project Plan、worktree+PR = Project Repository+版管理、受け入れ基準 = Acceptance Record、エージェント報告 = Progress Status Record という対応でほぼ全成果物が既存プロセスに載る

## 4. ISO/IEC 25010:2023・42001・5338 — 品質語彙と AI 統制

- 25010:2023 は9特性(Safety 新設)。Ryza の品質目標語彙は**信頼性(Faultlessness/回復性)・保守性(解析性/試験性)・セキュリティ・Safety(fail-safe)** を重点とする。[一覧](https://iso25000.com/index.php/en/iso-25000-standards/iso-25010)
- 42001(AIMS): A.6.2.4 V&V の方法定義、**A.6.2.8 イベントログ記録**、A.9.2 責任ある利用プロセス(human oversight は B 実装ガイダンス)。[管理策一覧](https://www.isms.online/iso-42001/annex-a-controls/)。Ryza のリネージ・審査意見書保存・承認フローが先行実装に相当
- 5338: AI ライフサイクルに**継続的検証(デプロイ後の継続テスト)**を追加 — Ryza では A-13・freshness 監視・E 系評価が対応。[解説](https://www.softwareimprovementgroup.com/blog/iso-5338-get-to-know-the-global-standard-on-ai-systems/)
- 注意: 5338 は「AI システムを開発する組織」向けであり「AI が開発主体」の規格ではない — AI 開発主体への統制は 42001 A.9(利用プロセス)+本規程の独自条項で埋める

## 5. SQuBOK Guide V3 — 技法カタログ

- 樹形図5章: 品質概念/品質マネジメント(21KA)/品質技術(10KA)/専門(ユーザビリティ・セーフティ・セキュリティ・プライバシー)/応用(**AI システム品質・アジャイル/DevOps** — V3 新設)。[公式](https://www.juse.or.jp/sqip/squbok/vol3/)
- レビュー技法: インスペクション(Fagan 1976: 発見誤りの82%・総開発コスト25-30%削減)〜モダンコードレビュー(パスアラウンド型)の体系。[整理](https://qiita.com/mkt_hanada/items/4a6c16800ed061111233)
- テスト設計3分類(仕様ベース/構造ベース/経験ベース)。[JSTQB シラバス](https://jstqb.jp/dl/JSTQB-SyllabusFoundation_VersionV40.J02.pdf)
- メトリクス目安(IPA データ白書・**国内 SIer 母集団のため参考値**): レビュー指摘密度 2.5件/KSLOC、結合テストバグ密度 1.21件/KSLOC。AI エージェント開発の母集団統計は存在しないため、**Ryza は自前ベースラインを蓄積する**(独立審査の指摘件数・重大度を記録し傾向監視)
- 採用不可(執行点に落ちないため): 教育・育成(2.6)、調達(2.9)等の人的組織前提の KA

## 6. 設計への含意(まとめ)

1. 規程の骨格 = 29110 Basic の PM/SI テーラリング+ISO 9001 の 8.3.4/8.5.6/10.2 借用
2. 「PR 承認=レビュー記録・マージ=変更許可者」を**規程で明文定義**(これが監査証拠性の前提)
3. 検証(CI)と妥当性確認(統合後のデプロイ・実走スモーク)を区別して定義
4. AI 固有: エージェント実装→独立審査(инспекция相当)→是正→統合のループを標準プロセス化し、審査意見書の保存(docs/reviews)と指摘メトリクスの蓄積を義務化
5. 是正処置(10.2)型のバグ対応: 封じ込め→原因→**水平展開**→有効性レビューを Issue テンプレートで型化
