---
review: t029-jquants-fundamentals
reviewed_sha: bd495e094753433a9fd973c62cf98ee66f3785b9
reviewer: 独立役員(プロンプト分離)
review_date: 2026-08-04
verdict: request_changes
---

# 独立審査意見書: PR #147(T-029 J-Quants 財務サマリの構造化数値化)

**アーギュメント**: 本 PR のコア設計(series_code・ts=期末/as_of=開示日時・fail-closed・冪等マーカ・revision upsert 再利用・リネージ・非 LLM)は指示書 §1〜§4 に忠実で、config のフィールド名は J-Quants 公式仕様と全項目一致することを審査側でも独立に照合したが、受け入れ基準1「CI green」が既存配線テストの未更新により客観的に不成立であり(CI run 30911380662: `tests/jobs/test_daily.py::test_default_ingest_sources_are_wired` failed)、加えて DiscTime 欠測時の as_of 繰上げという look-ahead を許す経路(不変原則4)が残るため、判定は request_changes とする。いずれも修正は小さく、再審査は差分確認で足りる。

## 審査で確認した事実(判定の土台)

- 合否の正である指示書は `/docs/tasks/T-029-jquants-fundamentals.md`(ブランチ内に同梱)。差分は 5 ファイル 1,029 行追加のみで、スキーマ・ゲート・会計・リスクに触れない(`git diff origin/main...bd495e0 --stat`)— 保護領域手続は不要、新規モジュールとして本独立審査のみが必須(指示書 §5-6)
- **既存規約の再利用**: 書込は `base.write_indicator`(src/ryza/ingest/base.py:357-397、revision 対応 upsert)を再利用(fundamentals.py:427)。symbol 正規化は `ingest.jquants._normalize_symbol` を import(fundamentals.py:53、重複実装なし)。リネージは `provenance.lineage.record`(outputs×inputs、lineage.py:45-73)を既存パターンどおり呼ぶ(fundamentals.py:432-436)— 指示書 §1-1・§2-3 どおり
- **フィールド名の独立照合**: 審査側で https://jpx-jquants.com/en/spec/fin-summary と /typeofdocument を WebFetch し、config/jquants_fields.yaml の全 16 項目(Sales/OP/OdP/NP/EPS/DEPS、F*、NxF* — 仕様の変則表記 `NxFNp` 含む)、および導出用フィールド(DiscDate/DiscTime/DocType/CurPerType/CurPerEn/CurFYEn/NxtFYEn)が仕様の V2 命名と一致することを確認した。config 全行に根拠コメントあり(受け入れ基準3充足)
- **LLM 不使用**: fundamentals.py に LLM・ネットワーク呼び出しは存在しない(import は psycopg/yaml/ryza 内部のみ、fundamentals.py:36-54)— 受け入れ基準4充足
- **テスト実行**(共有 DB・1回): `tests/preprocess/` 62 件全 pass(新規 8 件含む、125.28s)。失敗ゼロのため Issue #142 の既知失敗一覧との照合は不要
- **テストの実質性**: フィクスチャは本番と同一経路 `base.upsert_document` で文書+証憑+リネージを作り(test_fundamentals.py:73-98)、as_of を「2026-05-14 15:00 JST = 06:00 UTC」の値で固定検証(test_fundamentals.py:130-133)、訂正開示の revision 列を `[(0, 45000000000.0), (1, 45500000000.0)]` で固定(test_fundamentals.py:201-204)、欠測項目の**不在**を row is None で検証(test_fundamentals.py:167-172)。モックが実装に都合よく歪んでいる形跡はない
- **セキュリティ**: SQL は全てパラメータ化・識別子は静的リテラルのみ(fundamentals.py:284-293, 311-318, 369-372)。外部入力(証憑 payload)は json.loads → dict 型検査 → 値としてのみ使用で、series_code への混入も文字列整形のみ(fundamentals.py:258)。SQL injection・識別子組立の問題なし

## 所見

### 重大(1件)

**C-1. CI red — 既存配線テストの未更新(受け入れ基準1違反)**。daily.py の `_default_ingest_sources` に `jquants_fundamentals` を挿入した(daily.py:160)が、ソース一覧を固定する既存テスト `tests/jobs/test_daily.py:692`(`test_default_ingest_sources_are_wired`)を更新しておらず、PR の CI(run 30911380662)が failed(2257 passed / 1 failed)。指示書 §4 末尾「CI(クリーン DB)が合否の正」・受け入れ基準1「CI green」に照らし、これ単独で approve 不能。**修正**: 期待リストに `jquants_fundamentals` を追加し、ついでに「jquants の直後に位置する」ことを順序でアサートすると配線意図(T-029 §1-4)がテストに固定される。

### 中(5件)

**C-2. DiscTime 欠測時の as_of 繰上げは look-ahead を許す経路(不変原則4)**。`_parse_as_of` は DiscTime 欠測時に「00:00:00 JST」へフォールバックする(fundamentals.py:164)。これは実際の開示時刻(例: 15:00 JST)より最大約 24 時間**早い** as_of を刻むため、point-in-time 読出し(as_of ≤ 判断時点)で「開示前に知っていた」ことになる系列を作り得る。docstring(fundamentals.py:157-160)は「実行時点に落とすより忠実」と論じるが、比較すべき保守側の代替(翌日 00:00 JST = 開示日の終端、または skip+error 集計)を検討していない。指示書 §1-2「開示時点以外を as_of にしてはならない」の趣旨は「開示より**前**に倒さない」ことにある。**修正**: 欠測時は開示日の JST 終端(翌日 00:00 JST)を刻むか、`no_disc_time` としてエラー集計へ落とす。いずれも 1〜3 行。あわせて欠測経路のテストを追加する(現状 `_parse_as_of` のテストが無い)。

**C-3. 予想修正開示(EarnForecastRevision 等)が全 skip され、四半期間の予想改定が系列に反映されない**。仕様の DocType には `EarnForecastRevision` / `DividendForecastRevision`(+REIT 変種)があり(/spec/fin-summary/typeofdocument で審査側確認)、`_basis_from_doctype` は "_" 分割の中央要素が無いこれらを None → 全項目 skip とする(fundamentals.py:117-135、テスト test_fundamentals.py:264-272 で固定)。fail-closed としては指示書 §1-3 準拠で point-in-time 上も安全(古いだけで漏洩しない)だが、GARP(T-019)の核である「予想成長率」がまさに使う予想値の期中改定を落とすため、データ品質上の実質的制約である。**修正**(本 PR 内でなくてよい): 「制約により実装しない項目」として扱い、C-6 の reminders 登録に含める。skip 集計上も `no_basis` が「非財務諸表 DocType」と「basis 導出不能」を混同しているので、キーを分けると運用時の診断が楽になる。

**C-4. NonConsolidated 開示での値の所在(トップレベル vs NC\*)が未検証**。J-Quants 仕様はトップレベルの Sales/OP 等を「Consolidated Results」とし、別に `NC*`(NCSales 等)の並行フィールドを定義する(審査側 WebFetch で確認)。本実装は DocType が `..._NonConsolidated_...` の開示でもトップレベルのフィールドを読む(fundamentals.py:254)。単体開示のみの企業でトップレベルが空・NC\* 側に値が入る仕様だった場合、単体開示企業(小型株 — GARP の主猟場)の系列が丸ごと no_value skip になる。PR 記載のとおり DB に実 payload が 0 件でサンプル確認は不可能だった(指示書 §1-3 の「実 payload 数件サンプル」が果たせない状況)ため実装判断自体は責められないが、**初回実取込後の検証が制度化されていない**。**修正**: C-6 の reminders 登録に「初回取込後、NonConsolidated 開示の skip 集計を確認し必要なら NC\* マッピングを追補」を含める。誤データは書かれない(fail-closed)ので緊急性は低い。

**C-5. daily 配線の既定 limit 500/日は決算集中日に遅延を蓄積する**。配線は `jquants_fundamentals.main([])`(daily.py:160)で既定 limit=500(fundamentals.py:507-509)。daily.py 自身の tdnet コメントが「決算集中日(1000件超/日)」と書くとおり(daily.py:161-162)、ピーク日は当日中に処理しきれず、配線コメント「当日の新規開示が同一 daily 実行内で数値系列化される」(daily.py:149-150)と矛盾する。冪等マーカで自己回復はする(翌日以降 500 件/日ずつ消化)ため重大ではない。**修正**: `run_promotion` を「未処理が尽きるまでループ」させるか、daily 配線側で `--backfill` 相当を渡す(どちらも数行)。

**C-6. 将来アクションが ops/reminders.yaml に未登録(受け入れ基準7・CLAUDE.md「将来アクションの制度化」違反)**。PR 本文は「バックフィルは PR マージ後に設計リードが実施」と述べるが、差分に ops/reminders.yaml の変更は無い(diff --stat で確認)。セッション内の約束は無効であり、①マージ後バックフィルの実施、②初回実取込後の実 payload 検証(C-4)、③予想修正開示の扱い再検討(C-3)は機械可読で登録しなければ消える。なお受け入れ基準5(バックフィル実行結果の報告)自体は「対象 0 件」の明記があり実質充足と判定する(0 件の環境で実行しても意味のある数字は出ない)。

### 軽微(4件)

**C-7. `FY_NEXT` は仕様に無い造語の period_kind で、field 接頭辞 `NxFcst*` と二重符号化**。翌期予想は field 名(NxFcst\*)だけで既に判別でき、ts=NxtFYEn でも判別できるため、period_kind への FY_NEXT 導入(fundamentals.py:249)は冗長。docstring に明記されており(fundamentals.py:11-12)実害は無いが、読出し側は 2 つの規約を両方知る必要がある。用語導入時は定義を書く規約(CLAUDE.md)には従っている。

**C-8. `_basis_from_doctype` の docstring と挙動の不一致**。docstring は「REIT・Foreign …は None → skip」と読める(fundamentals.py:122-124)が、仕様上の `FYFinancialStatements_Consolidated_REIT` / `..._Consolidated_Foreign` は中央要素が Consolidated のため通過し昇格される。挙動自体は妥当(basis が導出できるものは書いてよい)だが、コメントを実挙動に合わせるべき。

**C-9. `_num` が "NaN"/"Infinity" を受理する**。`float(v)` は "NaN"・"Infinity"・"1e999" を非有限 float に変換し(fundamentals.py:183-194)、numeric 列へ書かれ得る(PostgreSQL numeric は NaN を許容)。NaN は自己不等のため `write_indicator` の同値判定(base.py:384)を常にすり抜け、再処理時に revision を無限に進める素地になる。J-Quants が返す可能性は低いが、fail-closed の趣旨からは `math.isfinite` での拒否が一貫する(1 行)。

**C-10. FY_NEXT 経路(NxF\* に実値があるケース)の書込テストが無い**。フィクスチャは NxF\* を常に空にしており(test_fundamentals.py:59-63)、`ts=NxtFYEn`・series `NxFcst*:FY_NEXT` の書込は一度も検証されていない。C-2 のテスト追加と合わせて 1 ケース足すのが安価。

## 反対意見書(この request_changes 判定が間違っている場合の理由トップ3)

1. **CI red は 1 行修正の既存テスト更新漏れであり、判定を左右する「設計上の欠陥」ではない** — approve+コメントで足り、request_changes は往復コストの無駄だという立場はあり得る。*反論*: 受け入れ基準1「CI green」は指示書が定めた客観基準で、独立審査が基準を裁量で緩めれば基準の意味が失われる。修正は小さいのだから差し戻しコストも小さい。*代替案*: 「軽微修正条件付き approve」区分の新設 — 現行の審査規約に無い区分を審査側が発明するのは越権。
2. **C-2(DiscTime 欠測フォールバック)は理論上の経路で、実データで DiscTime が欠測する証拠が無い** — 実害未確認の経路で差し戻すのは過剰だという立場。*反論*: 不変原則4は「未来情報の混入はバグではなく設計違反」と定めており、発生頻度でなく経路の存在自体が違反である。修正も 1〜3 行で安い。*代替案*: reminders 登録で後日修正 — look-ahead 経路は他の後回し項目(C-3〜C-5)と違い「誤ったデータが書かれる」側なので、マージ前に閉じるべき。
3. **C-4(NC\* の所在)を検証不能のまま指摘するのは不公平で、むしろ本 PR の範囲外** — DB 0 件で指示書 §1-3 のサンプル確認が不可能だった以上、実装者は最善を尽くしたという立場。*反論*: 所見は実装の否定ではなく「検証の制度化(reminders 登録)」を求めるもので、これは受け入れ基準7が明文で要求する手続である。*代替案*: 審査側が J-Quants に課金してサンプル取得して決着させる — 意見は証拠で解決する原則には適うが、API 課金の発生はコスト意識の規約に反し、初回実取込を待てば無料で同じ証拠が得られる。

## 判定

**request_changes**。必須修正は C-1(既存テスト更新)と C-2(as_of フォールバックの保守化+テスト)、および C-6(reminders 登録)。C-3〜C-5 は reminders 登録で本 PR 外に送ってよい。C-7〜C-10 は任意。再審査は上記の差分確認のみで足り、テストの再フル実行は CI に委ねてよい。
