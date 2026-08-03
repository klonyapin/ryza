# ダッシュボード設計・データ可視化の理論とガイドライン(調査)

- as_of: 2026-08-03 / producer: research-agent(設計リード監修) / 用途: Ryza 社内モニタリングダッシュボード設計の根拠
- 結論(先出し): Ryza のダッシュボードは「一画面の監視面 + ドリルダウン」の二層とし、比較文脈(目標・前期・ベンチマーク)を必ず数値に添え、知覚精度順位(位置>長さ>角度>面積>色)に従ってチャート型を機械的に選ぶ。ゲージ・円グラフ・二軸・生 JSON は禁止記法とする。

## 1. ダッシュボード設計理論

### 1.1 Stephen Few — Information Dashboard Design
- 定義: ダッシュボードは「目的達成に必要な最重要情報を一画面に集約し、一目で監視できる視覚表示」。スクロール・画面遷移なしで全体が見えることを要件とする([Perceptual Edge: Common Pitfalls in Dashboard Design](https://www.perceptualedge.com/articles/Whitepapers/Common_Pitfalls.pdf)、[書籍概要](https://www.perceptualedge.com/images/Dashboard_Outline.pdf))。
- 非データピクセルの削減、重要データの配置による強調(左上・中央が最強)、KPI の論理的グルーピングと空間的分離([Dashboard Design course notes](https://www.perceptualedge.com/files/Dashboard_Design_Course.pdf))。
- 単独数値は無意味であり、比較文脈(目標・前期・平均・ベンチマーク)を伴って初めて意味を持つ。この要請から Few は 2005 年にゲージの代替として **bullet graph** を設計した([Tableau: bullet graphs beat gauge charts](https://www.tableau.com/about/blog/2015/2/bullet-graphs-beat-gauge-charts)、[Bullet graph](https://en.wikipedia.org/wiki/Bullet_graph))。
- **Ryza への示唆**: トップ画面は 1 画面固定(スクロール禁止)。「NAV・当日 PnL・リスク使用率・Kill Switch 状態・ジョブ健全性・未処理承認」の 6 ブロックに限定し、左上に最重要(取引系統の生死と NAV)を置く。各 KPI は必ず比較文脈付き(bullet 型: 実績+目標マーカー+許容/警戒/危険帯)で描き、単独数値のカード表示をしない。

### 1.2 Tufte — data-ink ratio / chartjunk
- data-ink ratio = データを表す非冗長なインクの比率。「非データインクを(常識の範囲で)消せ」「冗長なデータインクを消せ」が原則。装飾的要素は chartjunk と呼ばれ、情報を増やさずに認知負荷だけを増やす([InfoVis:Wiki: Data-Ink Ratio](https://infovis-wiki.net/wiki/Data-Ink_Ratio)、[Tufte's Principles of Data-Ink](https://jtr13.github.io/cc19/tuftes-principles-of-data-ink.html))。
- **留保**: Tufte 自身が「within reason(常識の範囲で)」と留保しており、過度な最小化は可読性を落とすとする実証批判(Bateman et al. の chartjunk 記憶実験など)も存在する。Ryza では「装飾の禁止」までを規範とし、「グリッド線や軸ラベルの削除」までは踏み込まない。
- **Ryza への示唆**: グリッド線は淡色 1 段階のみ、3D・影・グラデーション・ロゴ・枠線は禁止。色は「状態(正常/警戒/停止)」と「シナリオ(実績/計画/予測)」の意味付けにのみ割り当て、装飾に使わない。

### 1.3 Shneiderman — Visual Information-Seeking Mantra
- 「Overview first, zoom and filter, then details-on-demand」。7 データ型 × 7 タスク(overview / zoom / filter / details-on-demand / relate / history / extract)の分類([The Eyes Have It, IEEE VL 1996, DOI 10.1109/VL.1996.545307](https://www.oreilly.com/library/view/the-craft-of/9781558609150/xhtml/B9781558609150500469.htm))。
- **Ryza への示唆**: 一画面のトップ = overview。詳細(ジョブ実行ログ・LLM 呼び出しトレース)は details-on-demand で降りる。`history` タスクは as_of スナップショット選択 UI(過去日付のダッシュボード再現)として将来実装し、point-in-time 原則をダッシュボード層でも守る。

## 2. IBCS(International Business Communication Standards)

- SUCCESS の 7 原則: **Say**(メッセージを述べる)/ **Unify**(記法を統一)/ **Condense**(情報密度を高める)/ **Check**(整合性を検証)/ **Express**(適切な表現を選ぶ)/ **Simplify**(冗長を除く)/ **Structure**(論理構造を与える)([IBCS Standards](https://www.ibcs.com/standards/))。
- 主要ルール上位 5: タイトル概念の統一、時系列 vs 構造比較でチャート型を使い分け、シナリオ記法、差異(variance)表示、スケーリングの一貫性。下位 5(避けるべきもの): 誤ったチャート型(円・レーダー)、誤導的スケーリング、装飾過多、情報密度の低い多ページ化、メッセージ不在([Top and bottom 5 of IBCS](https://www.ibcs.com/resource/top-and-bottom-5-of-international-business-communication-standards/))。
- **シナリオ記法(統一塗り分け)**: 実績 = 濃色ベタ塗り、前年/前期 = 淡いグレーのベタ塗り、計画/予算 = 輪郭線のみ(アウトライン)、予測 = ハッチング([同上](https://www.ibcs.com/resource/top-and-bottom-5-of-international-business-communication-standards/)、[IBCS v1.1 抜粋 PDF](https://www.ibcs.com/wp-content/uploads/2021/02/IBCSv1-1_p125-131.pdf))。
- **差異表示**: 赤・緑は差異(variance)専用に予約。緑 = 有利、赤 = 不利。絶対差異(実額)と相対差異(%)を別チャートで並置するのが標準([Implementing IBCS rules in Power BI](https://towardsdatascience.com/implementing-ibcs-rules-in-power-bi/))。
- **Ryza への示唆**: 会計系統(架空=ファンド帳簿 / 実費=運営帳簿)の月次表示に IBCS シナリオ記法をそのまま採用。実績=ベタ、IPS 目標/予算=アウトライン、月末見込み=ハッチング。赤緑は「対計画差異」と「リスクリミット超過」だけに予約し、系統の識別には使わない(系統識別は色ではなくパネル分離とラベル)。二系統混在の 1 チャートは禁止。

## 3. メトリクス選定論

- **良い指標の条件**(Lean Analytics): 比較可能・理解可能・**比率またはレート**であること・**行動を変えること**。行動を変えない指標は vanity metric([Lean Analytics ch.2 "How to Keep Score"](https://www.oreilly.com/library/view/lean-analytics/9781449335687/ch02.html)、[NN/g: Vanity Metrics](https://www.nngroup.com/articles/vanity-metrics/))。
- **"So what?" テスト**: 数字を見て次の行動が特定できないなら載せない。累計・総数などの単調増加する絶対値は典型的な vanity metric([NN/g](https://www.nngroup.com/articles/vanity-metrics/))。
- **SRE 4 ゴールデンシグナル**: Latency / Traffic / Errors / Saturation。「症状(何が壊れたか)を監視し、原因(なぜか)は掘る側に置く」。アラートは「未検知・緊急・アクション可能」の 3 条件を満たすものだけ([Google SRE Book: Monitoring Distributed Systems](https://sre.google/sre-book/monitoring-distributed-systems/))。
- **先行 vs 遅行**: リターン・PnL は遅行指標(結果)。先行指標は「シグナル生成数、約定スリッページ、データ鮮度、LLM 出力の棄却率、リスク使用率」など、将来の結果を変えうる操作可能量。
- **Ryza への示唆**: ダッシュボードを「遅行(成果)」と「先行(プロセス)」に明示分離。ジョブ層の 4 ゴールデンシグナル写像 — Latency = 日次サイクル各ジョブ所要と締切余裕、Traffic = 処理シグナル数/LLM 呼び出し数、Errors = ジョブ失敗率・取込欠損・スキーマ違反、Saturation = リスクリミット使用率・API レート余裕・LLM コスト予算消化率。累計 LLM 呼び出し回数のような単調増加値は排除し、「1 判断あたりコスト」「予算消化率」の比率へ。

## 4. チャート選択ガイドライン

- **知覚精度順位**(Cleveland & McGill 1984, JASA 79(387)): ①共通軸上の位置 → ②非整列軸上の位置 → ③長さ・方向・角度 → ④面積 → ⑤体積・曲率 → ⑥濃淡・色の彩度。「できるだけ順位の高い要素を使え」([原論文 PDF](https://www.math.pku.edu.cn/teachers/xirb/Courses/biostatistics/Biostatistics2016/GraphicalPerception_Jasa1984.pdf)、[JASA](https://www.tandfonline.com/doi/abs/10.1080/01621459.1984.10478080))。
- **関係 → チャート型のマッピング**(FT Visual Vocabulary): 推移=ライン/エリア、比較・順位=横棒/ドットプロット、差異=発散棒、構成=積み上げ棒・ツリーマップ(円ではなく)、分布=ヒストグラム/ボックス、相関=散布図、規模=棒([FT chart-doctor/visual-vocabulary](https://github.com/Financial-Times/chart-doctor/tree/main/visual-vocabulary)、[EU Data Visualisation Guide](https://data.europa.eu/apps/data-visualisation-guide/visual-vocabulary))。
- **テーブルが適する場合**: 正確な値の参照が目的、単位が混在する少数行 × 多次元、後で数値を引用・監査する必要がある場合。監査証跡としての可読性はチャートより表が勝る。
- **金融特有の慣行**:
  - 資産推移 = ラインチャート + **直下に軸を揃えたアンダーウォーター(ドローダウン)図**([PerformanceAnalytics `chart.Drawdown`](https://rdrr.io/cran/PerformanceAnalytics/man/chart.Drawdown.html)、[Visualizing Drawdown](https://gregorygundersen.com/blog/2021/08/27/drawdown/))。
  - PnL の内訳 = ウォーターフォール(始点 NAV → 実現損益 → 評価損益 → 手数料・スリッページ → 終点 NAV)。
  - **ゲージは非推奨**: 角度依存で情報量が少なく比較文脈を載せられない。bullet 型に置換([Tableau](https://www.tableau.com/about/blog/2015/2/bullet-graphs-beat-gauge-charts))。**例外**: Kill Switch のような二値/少状態は bullet でなく状態インジケータ(色+テキストラベル)が適切。
- **Ryza への示唆**: チャート型は「データ関係タグ → 許可チャート型」の写像をヘルパ層で実装し、実装者の裁量で円グラフやゲージが混入しない構造にする。リスク使用率・コスト予算消化率は bullet 型。配分は横棒または積み上げ 100% 棒。承認ガバナンスは表+残時間バー。

## 5. 投資運用ダッシュボードの実例・慣行

- **ファクトシートの標準構成**: 投資目的・運用者、NAV 推移、期間別リターン表(3M/1Y/3Y/5Y/設定来)、上位 10 保有、配分、リスク指標(ボラ・最大 DD)、費用([Trustnet: How to read a fund factsheet](https://www.trustnet.com/investing/13425988/how-to-read-a-fund-factsheet)、[Resonanz Capital: hedge fund metrics](https://resonanzcapital.com/insights/understanding-hedge-fund-quantitative-metrics-a-handy-cheatsheet-for-investors))。
- **GIPS 2020 の表示慣行**: 年次リターン最低 5 年(設定来が短ければ設定来)+ベンチマーク併記+3 年年率化事後標準偏差等([GIPS Standards for Firms 2020 PDF](https://www.gipsstandards.org/wp-content/uploads/2021/03/2020_gips_standards_firms.pdf)、[ACA: Section 4](https://www.acaglobal.com/industry-insights/2020-gips-standards-explanation-provisions-firms-section-4/)、[CFA Institute overview](https://www.cfainstitute.org/insights/professional-learning/refresher-readings/2026/overview-of-the-global-investment-performance-standards))。
- 実務上の含意: 単一の累積リターン数字ではなく「年次リターン+ベンチマーク+分散度+事後リスク」の組で示すのが業界の最低線。
- **Ryza への示唆**: デモ系統の成績表示は GIPS の**形式**を(準拠主張はせず)借り、常に「等配分 buy-and-hold 対照」と「全コスト込み」を同一表に並置(E4・E3)。E9 に従い生 Sharpe を単独で大きく出さず、Deflated Sharpe / PBO と同視野に置く。カットオフ後期間は背景色帯で明示(E1)。

## 6. アンチパターン一覧

| # | アンチパターン | 理由 | 出典 |
|---|---|---|---|
| A1 | 円グラフ・ドーナツの多用 | 角度・面積判断は知覚精度が低い。IBCS も非推奨 | [Few 解説](https://10qviz.org/save-the-pies-for-desert/)、[IBCS bottom 5](https://www.ibcs.com/resource/top-and-bottom-5-of-international-business-communication-standards/) |
| A2 | 円形ゲージ・メーター | 情報量が少なく比較文脈を載せられない。bullet 型に置換(二値状態は状態インジケータ) | [Tableau](https://www.tableau.com/about/blog/2015/2/bullet-graphs-beat-gauge-charts) |
| A3 | 二軸(第 2 Y 軸) | 恣意的スケールで偽相関を見せる。small multiples に置換 | [Datawrapper](https://www.datawrapper.de/blog/dualaxis)、[PolicyViz](https://policyviz.com/2022/10/06/avoiding-the-dual-axis-chart/) |
| A4 | 誤導的スケーリング(切断軸・不揃いスケール) | 視知覚を歪める。IBCS 主要ルール | [IBCS top 5](https://www.ibcs.com/resource/top-and-bottom-5-of-international-business-communication-standards/) |
| A5 | 装飾・3D・影・意味のない色(chartjunk) | 情報を増やさず認知負荷だけ増やす | [InfoVis:Wiki](https://infovis-wiki.net/wiki/Data-Ink_Ratio) |
| A6 | 生 JSON / 生ログの直貼り | メッセージ不在の極端形。要約+details-on-demand へ | [IBCS](https://www.ibcs.com/resource/top-and-bottom-5-of-international-business-communication-standards/)、[Shneiderman](https://www.oreilly.com/library/view/the-craft-of/9781558609150/xhtml/B9781558609150500469.htm) |
| A7 | 意味のない精度桁数(false precision) | 虚偽の確信を与える。有効 2〜3 桁 | [False precision](https://en.wikipedia.org/wiki/False_precision)、[Dashboard Critic](https://dashboardcritic.substack.com/p/wrong-decimal-places-avoid-this-simple) |
| A8 | 一画面に収まらない監視面 | 全体像と相互関係が失われる | [Few: Common Pitfalls](https://www.perceptualedge.com/articles/Whitepapers/Common_Pitfalls.pdf) |
| A9 | 比較文脈のない単独数値カード | 対比なしには解釈不能 | [Few / bullet graph 設計動機](https://www.tableau.com/about/blog/2015/2/bullet-graphs-beat-gauge-charts) |
| A10 | vanity metric(累計呼び出し数など単調増加絶対値) | 行動を変えない。比率・レートへ変換 | [Lean Analytics ch.2](https://www.oreilly.com/library/view/lean-analytics/9781449335687/ch02.html)、[NN/g](https://www.nngroup.com/articles/vanity-metrics/) |
| A11 | 原因メトリクスでのアラート乱発 | アラート疲れを生み無視される | [Google SRE Book](https://sre.google/sre-book/monitoring-distributed-systems/) |
| A12 | 赤緑を差異以外に流用 | IBCS は赤緑を variance 専用に予約 | [IBCS top 5](https://www.ibcs.com/resource/top-and-bottom-5-of-international-business-communication-standards/) |
| A13 | 生 Sharpe の単独強調 | 多重検定バイアスを隠す。Deflated Sharpe / PBO と同視野に(E9) | 00-system-design.md §1(E9)、[GIPS](https://www.gipsstandards.org/wp-content/uploads/2021/03/2020_gips_standards_firms.pdf) |

## 7. 未解決・要追加調査

- Few の「Common Pitfalls」13 項目の逐語リストは PDF 抽出不可。必要なら原本 PDF を手元で開いて補完(URL は上記)。
- IBCS 各ルールの逐条(v1.2 全文)は会員向け。無償公開は v1.1 の抜粋 PDF のみ確認。
