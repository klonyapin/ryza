# T-026: 参照形式の検証(F-10)+軽微堅牢化群(F-13)— Issue #122

- 起草: 2026-08-04 設計リード / 対象: A-12 監査所見 A-12-13・A-12-04(裁定 F-10・中)、A-12-09・pass5-5・pass3b-2 ほか(裁定 F-13・軽微)
- 前提知識: CLAUDE.md、docs/reviews/a12/00-adjudication.md §3、**各所見の原文**(docs/reviews/a12/ 配下の pass 別ファイル — 該当箇所の特定はここから辿る)、src/ryza/governance/decisions.py、src/ryza/provenance/evidence.py、src/ryza/governance/boardroom.py
- **保護領域**(governance_engine・監査コード)。統合は設計リードが独立役員審査+みなし承認手続で行う
- 本仕様書自体を実装ブランチの最初のコミットとして `docs/tasks/T-026-ref-validation-and-minor.md` に含めること

## F-10: proposal_ref / source の形式検証

### 問題(A-12-13 + A-12-04)

`record_deemed_approval`(governance/decisions.py L254 付近)の proposal_ref は空文字チェックのみで、短い任意文字列が通る(重複判定の UNIQUE が「偶然一致」で誤作動し得る)。証憑の source も形式検証がなく、表示系(embed・レポート)への注入面になる。

### 実装

1. **proposal_ref**: 書き込み時に次の3形式のみ許可 — (a) 本リポジトリの PR URL `https://github.com/<owner>/<repo>/pull/<数字>`、(b) `decision:<数字>`、(c) `manual:<[a-z0-9][a-z0-9-_]{2,63}>`。不一致は理由付き ValueError。**既存 DB 行には触れない**(検証は書き込み時のみ)。設計リードによる事前確認済み(2026-08-04): 既存 governance.decisions の proposal_ref 28 distinct は全て形式 (a) の本リポジトリ PR URL — 差し戻し不要、3形式で実装せよ
2. **source**(provenance/evidence.py `EvidenceStore.store` と ledger 側 create_evidence の source 引数): `re.fullmatch(r"[\w][\w.:/\-]{0,127}", source)`(Python の str に対する `\w` は Unicode 単語文字 — 日本語を含む)に制限。**ASCII 限定にしないこと** — 設計リードによる事前確認済み(2026-08-04): 既存 ledger.evidence の source 実データは `TDnet(564) / 日銀(55) / BOE(51) / 米経済分析局BEA(47) / FRB(20) / ECB(15) / FRED(12) / intl_banks(7) / investment_committee(2) / demo(2) / J-Quants(1)` の 11 種で、日本語を含む。この 11 値全てが通ることをテストで固定。狙いは表示系(embed・レポート)への注入面の遮断であり、空白・改行・制御文字・markdown メタ文字(`[]()*_~|<>` 等 — `\w` と `.:/‐` 以外)を排除できればよい
3. 検証は純粋関数として切り出し、単体テスト可能にする

## F-13: 軽微堅牢化群

各項目とも、**所見原文(docs/reviews/a12/ の該当 pass ファイル)を読んで該当箇所を特定**すること。以下は裁定の要約と方針:

1. **A-18-8 全ゼロ時の無音経路注記(A-12-09)**: 検査対象が 0 件のとき findings 0 と区別できない旨を docstring(と必要なら embed の分母表示)に注記。**ロジック変更は不可**(監査コードの意味論を変えない — 表示・注記のみ)
2. **test_classify の語彙正規表現(pass5-5)**: テストが migration から語彙を抽出する正規表現を `'([a-z_]+)'` 型から `'([^']+)'::text` 型へ拡張し、語彙の隠蔽(正規表現に合わない語彙が検査から漏れる)を防ぐ。テストのみの変更
3. **REVOKE FROM PUBLIC の no-op 整理**: **既存 migration の書き換えは禁止**(追記オンリーの保護領域)。docs/design 配下(または migrations の README 的文書があればそこ)に「REVOKE FROM PUBLIC は所有者ロール実行では no-op であり、ロール分離後の統制とドキュメント上の意図表明として書いている」という規約を1節で文書化し、0035/0036 の注記と整合させる
4. **_AMOUNT_PATTERN の科学的記数法(boardroom.py L278 付近)**: `1e6` 型の表記が金額として素通り/誤解釈しないよう、パターンの意図(検出か拒否か)を所見原文で確認して是正
5. **sanitize_speech のコードブロック内話者行(boardroom.py L640 付近)**: コードフェンス内にある話者行様のテキストの扱いを所見原文どおり是正
6. **チャネル ID ログ(pass3b-2)**: 所見原文で該当ログ箇所を特定して是正(ID の伏字化等)。**現行 main で該当コードが見つからない場合は、所見時点から実装が変わったと判断し、是正不要の根拠(該当コードの消滅コミット等)を完了報告に記す — 無理に何かを変えない**

## テスト

- F-10: 3形式それぞれの正常系+不正系(短い文字列・別リポジトリの URL・`decision:abc`・空白入り)/ source は上記実データ 11 値の全通過+不正系(空文字・改行入り・markdown メタ文字入り・128 文字超)
- F-13: 変更した項目ごとに最小の回帰テスト(注記のみの項目はテスト不要)

## 受け入れ基準

全テスト+ruff 通過 / 既存 migration 無変更 / 既存 DB 行と既存呼び出しの互換維持 / 監査コードのロジック無変更(注記のみ)/ LLM 非関与 / コミットは項目単位で分割(F-10 と F-13 は別コミット)+日本語+`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`、push しない(統合は設計リードが行う)。DB テストは `RYZA_DATABASE_URL=postgresql://ryza:ryza@localhost:15432/ryza`、worktree では `PYTHONPATH=$PWD/src` 必須
