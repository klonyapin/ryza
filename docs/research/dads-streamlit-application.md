# デジタル庁デザインシステム(DADS)の Streamlit ダッシュボードへの適用可能性

- as_of: 2026-08-03 / producer: research-agent(設計リード監修)/ 用途: ダッシュボードデザイン改修(代表指示)の根拠

## 1. 出典

- DADS β版トップ: https://design.digital.go.jp/dads/
- カラー(概要): https://design.digital.go.jp/dads/foundations/color/
- カラーパレット: https://design.digital.go.jp/dads/foundations/color/color-palette/
- タイポグラフィ: https://design.digital.go.jp/dads/foundations/typography/
- 余白: https://design.digital.go.jp/dads/foundations/spacing/
- ボタン(アクセシビリティ): https://design.digital.go.jp/dads/components/button/accessibility/
- コンポーネント一覧: https://design.digital.go.jp/dads/components/
- デザイントークン(npm/GitHub): https://github.com/digital-go-jp/design-tokens / https://www.npmjs.com/package/@digital-go-jp/design-tokens
- トークン実体(検証に使用): https://unpkg.com/@digital-go-jp/design-tokens@1.1.0/dist/tokens.css
- ウェブアクセシビリティ導入ガイドブック: https://www.digital.go.jp/resources/introduction-to-web-accessibility-guidebook
- WCAG 2.1 達成基準 2.5.5 ターゲットサイズ: https://waic.jp/translations/WCAG21/Understanding/target-size.html
- Streamlit theming: https://docs.streamlit.io/develop/concepts/configuration/theming
- Streamlit config.toml: https://docs.streamlit.io/develop/api-reference/configuration/config.toml
- Streamlit フォント: https://docs.streamlit.io/develop/concepts/configuration/theming-customize-fonts
- st.navigation: https://docs.streamlit.io/develop/api-reference/navigation/st.navigation

## 2. 採用すべきトークン実値

トークンは primitive / semantic / component の3層。npm `@digital-go-jp/design-tokens` v1.1.0 の `tokens.css` から抽出した実値。

**Blue(キーカラー系)**: 50 `#e8f1fe` / 100 `#d9e6ff` / 200 `#c5d7fb` / 300 `#9db7f9` / 400 `#7096f8` / 500 `#4979f5` / 600 `#3460fb` / 700 `#264af4` / 800 `#0031d8` / 900 `#0017c1` / 1000 `#00118f` / 1100 `#000071` / 1200 `#000060`。白背景で 4.5:1 を満たす実用下限は 800 以降。**Primary は Blue-900 `#0017c1`、hover を Blue-1000、active を Blue-1100** が DADS の段階規定に合う。

**Solid Gray(本文・境界)**: 50 `#f2f2f2` / 100 `#e6e6e6` / 200 `#cccccc` / 300 `#b3b3b3` / 400 `#999999` / **420 `#949494`(白背景 3:1 = 非テキスト下限)** / 500 `#7f7f7f` / **536 `#767676`(白背景 4.5:1 = テキスト下限)** / 600 `#666666` / 700 `#4d4d4d` / 800 `#333333` / **900 `#1a1a1a`(本文テキスト)**。

**セマンティック**: success-1 `#259d63` / success-2 `#197a4b`、error-1 `#ec0000` / error-2 `#ce0000`、warning-yellow-1 `#b78f00` / -2 `#927200`、warning-orange-1 `#fb5b01` / -2 `#c74700`。**色のみで損益を伝えない**(符号・矢印を併記)。

**タイポグラフィ**: Noto Sans JP / Noto Sans Mono(SIL OFL 1.1)。サイズトークン 14/16/17/18/20/22/24/26/28/32/36/45/48/57/64 px。**本文の標準最小 16px、14px はスペース制約時のみ、14px 未満は不許可**。行間: 本文 150% 以上、密な情報表示 120〜130%、見出し 140%。ウェイトは 400/700 の2段のみ。

**余白**: 基本単位 8px。**角丸**: 4/6/8/12/16/24/32px + full。**エレベーション**: 8段(例 `0 2px 8px 1px rgba(0,0,0,.1), 0 1px 5px 0 rgba(0,0,0,.3)`)。

## 3. アクセシビリティ数値基準

- **ターゲットサイズ: ボタンは 44×44 CSS px 以上**(隣接要素と重複なし)。満たないサイズは余白でターゲット領域を確保。リンクは最低 24×24 px
- **コントラスト: テキスト 4.5:1、非テキスト(ボタン塗り・枠線・アイコン・グラフ要素)3:1**
- **フォーカス**: DOM 順移動・tabindex 正値禁止。フォーカスリングのトークンは配布物に無い → WCAG 一般則(2px 以上・3:1)を自前基準に
- **準拠目標**: 導入ガイドブックは JIS X 8341-3:2016(WCAG 2.0 AA 相当)

## 4. ナビゲーションパターン

DADS はサイドナビを単独コンポーネントとして持たず、メニューリスト(グルーピング可)+ドロワーで構成。ダッシュボードに必要なのは「グルーピングされたメニュー+現在地表示」。

## 5. Streamlit 対応表(要点)

| DADS 基準 | Streamlit 実装 | 可否 |
|---|---|---|
| Primary/text/背景/境界/リンク色 | `[theme]` config.toml | ○ |
| Noto Sans JP | `[[theme.fontFaces]]`+self-host WOFF2(外部配信は IP 送信のため不可) | ○ |
| 本文 16px・角丸 | `baseFontSize=16` / `baseRadius` | ○ |
| ナビのグルーピング・現在地表示 | `st.navigation(sections)` | ○ |
| 行間・タイプスケール・44px ターゲット・フォーカスリング | CSS 注入(生成クラス依存 — バージョン更新で壊れうる) | △ |
| ダークモード | `[theme.dark]`(ただし DADS 公式ダークパレットが存在しない — 自前導出) | △ |
| データテーブルのスクリーンリーダー対応 | canvas 描画(glide-data-grid)のため**不可** | × |
| スキップリンク・visited 色・フォーカス順の完全制御 | **不可** | × |

## 6. 実現不可・重大な制約(フレームワーク移行判断の材料)

1. `st.dataframe` は canvas 描画で DOM にテキストが無い — スクリーンリーダー・ブラウザ内検索・拡大が効かない(CSS では回避不能)
2. スキップリンクの正規挿入手段なし
3. 見出し階層の構造的制御が弱い
4. フォーカス順の完全制御不可(視覚順と DOM 順の乖離を防げない)
5. visited リンク色の指定不可
6. DADS 公式のダークモードパレットが存在しない
7. CSS 注入は非公式 API — バージョン更新で無言で壊れ、CI で検知できない

## 7. 適用方針(設計リード裁定 — 調査エージェントの反対意見を採用)

DADS は行政サイト(文書中心・公開・多様な利用者)向けであり、Ryza ダッシュボード(完全非公開・単一利用者・数値表中心)への**全面準拠は実利のないコスト**。よって:

- **採用**: トークン層(色・タイポ・余白・角丸の実値)+実利のあるアクセシビリティ(コントラスト 4.5:1・44px ターゲット・色のみに依存しない損益表示・キーボード操作)
- **不採用**: JIS X 8341-3 全面準拠(スクリーンリーダー・スキップリンク・visited 色)
- フレームワーク移行(§6 の 1・2 が決定打になる場合)は Phase 5 で比較調査の上判断(reminders: dashboard-framework-evaluation)
