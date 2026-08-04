---
review: t029-jquants-fundamentals-r2
reviewed_sha: 81b161f
reviewer: independent-officer
review_date: 2026-08-04
verdict: approve
---

# 独立審査意見書(再検証 r2): PR #147(T-029 J-Quants 財務サマリの構造化数値化)

**アーギュメント**: 修正コミット 81b161f は r1 の必須所見(C-1: 配線テスト未更新による CI red、C-2: DiscTime 欠測時の look-ahead 経路、C-6: 将来アクションの reminders 未登録)を設計リードの裁定どおりに解消し、任意所見(C-3 部分/C-5/C-8/C-9/C-10)も裁定と一致する形で修正、繰延 2 件(C-3 残・C-4)は機械可読リマインダーとして正しく登録され、CI が審査対象 SHA で green(run 30913650896)・ローカル再実行も preprocess 65 件/daily 25 件全 pass であることを確認したため、判定は approve とする。修正差分が新規に持ち込んだ問題は、C-5 のループガードが実質デッドコードである点(軽微・非ブロッキング)のみである。

## 検証方法

- 審査対象: `81b161f`(r1 の reviewed_sha `bd495e0` からの修正差分 4 ファイル +196/-46 を精読。`config/jquants_fields.yaml`・`src/ryza/jobs/daily.py` は無変更であることを diff --stat で確認)
- CI: PR #147 の checks は head SHA 81b161f で `test pass`(run 30913650896)— 受け入れ基準1「CI green」充足を客観確認
- テスト再実行(共有 DB・各 1 回): `tests/preprocess/` 65 passed(r1 時 62 + 新規 3)、`tests/jobs/test_daily.py` 25 passed。Issue #142 系の環境起因失敗は今回発現せず、照合不要
- reminders.yaml: yaml.safe_load でパース確認、t029 系 2 エントリの条件型 `date_after`・アクション型 `issue_create` はいずれも `src/ryza/ops/weekly.py` の条件エバリュエータ/`execute_action`(weekly.py:146-156)が実装済みの v2 様式であり、発火可能であることを確認

## r1 所見の解消判定表

| 所見 | 裁定 | 判定 | 検証内容 |
|---|---|---|---|
| C-1(重大: 配線テスト未更新で CI red) | 修正 | **解消** | `test_default_ingest_sources_are_wired` の expected に `jquants_fundamentals` を追加(tests/jobs/test_daily.py:693-696)+ 「jquants の直後(index+1)」の順序アサート(test_daily.py:700)。裁定の付帯指示(順序固定)まで実装。CI green |
| C-2(中: DiscTime 欠測 as_of の look-ahead) | 修正 | **解消** | `_parse_as_of` は欠測時に翌日 00:00 JST(開示日の JST 終端)へフォールバック(fundamentals.py:190-196)。値固定テスト追加(2026-05-14 欠測 → 2026-05-14 15:00 UTC = 2026-05-15 00:00 JST、存在時 15:00 JST → 06:00 UTC の両経路)。旧実装の `or "00:00:00"` と異なり空文字 DiscTime も欠測側に落ちる(改善) |
| C-3(中: 予想修正開示の全 skip) | 部分修正 | **解消(裁定どおり)** | skip キーを `not_statement`(財務諸表本体でない)/`no_basis`(basis 未定)に分割(fundamentals.py:139-149, 244-247)、テストで固定。全スキップ見直しは `t029-initial-intake-verification`(2026-08-12)の body ②に明記 |
| C-4(中: NonConsolidated 開示の NC* 所在) | 繰延 | **繰延妥当** | 同リマインダー body ①に NC* 6 フィールドの具体名・確認手順・追補方針まで記載。機械可読・自己完結 |
| C-5(中: limit 500/日の遅延蓄積) | 修正 | **解消(軽微所見 r2-1 あり)** | `run_promotion` が未処理ゼロまでループ(fundamentals.py:521-540)。裁定「未処理ゼロまでループ(進捗ゼロで break)」に文言どおり一致。ただしガード自体は実効性なし(下記 r2-1) |
| C-6(中: reminders 未登録) | 修正 | **解消** | `t029-backfill-execution`(date_after 2026-08-05, labels: ops)と `t029-initial-intake-verification`(date_after 2026-08-12, labels: design)を登録。日付・ラベル・内容とも裁定と一致。weekly.py が実行可能な v2 様式 |
| C-7(軽微: FY_NEXT の二重符号化) | keep-as-is | **無変更妥当** | fundamentals.py の FY_NEXT(docstring 11-12 行目・286 行目)は r1 記載の状態と一致。裁定どおり所見としない |
| C-8(軽微: docstring と挙動の不一致) | 修正 | **解消** | `_basis_from_doctype` は `tuple[str | None, str | None]` を返し、docstring は REIT/Foreign 通過・not_statement/no_basis の分離・返り値の不変条件まで実挙動どおりに訂正(fundamentals.py:118-149) |
| C-9(軽微: 非有限値の受理) | 修正 | **解消** | `_num` に `math.isfinite` ガード(fundamentals.py:224-226)。"NaN"/"Infinity"/"-Infinity"/"1e999" の拒否と正常値の通過をテストで固定 |
| C-10(軽微: FY_NEXT 書込テスト欠落) | 修正 | **解消** | `test_promotes_next_fiscal_year_forecasts` 追加。NxF* 実値 → written 16(実績 6+現行予想 5+翌期予想 5)、ts=NxtFYEn、series `NxFcst*:FY_NEXT`、as_of 開示時刻を値で固定。仕様の変則表記 `NxFNp` も踏む |

## 修正が新規に持ち込んだ問題(軽微・非ブロッキング)

**r2-1(軽微)C-5 のループガードは実質デッドコード**。`batch_processed` は promote_document を呼ぶたび無条件にインクリメントされる(fundamentals.py:531)ため、ループ本体に入った時点で必ず `len(docs) > 0` に等しく、`batch_processed == 0` の break(fundamentals.py:538-540)には到達し得ない。コメントの「全件が processed マーカーを刻めなかった」という条件はこのカウンタでは検出できない — マーカーを刻めない文書があっても反復数は増えるからである。実際の無限ループ安全性は「promote_document が全経路(no_evidence / payload_not_json / no_symbol / no_disc_date / 正常)で `_stamp_processed` を刻むか、さもなくば例外を上げる」という別の不変条件(fundamentals.py:428-479 で確認、現に成立)に依存しており、コードは正しく停止する。ただし将来 stamp が条件付きになった場合にこのガードは機能しない。実効的なガードにするなら「直前バッチの最大 doc_id と同一なら break」等の進捗比較にする(2〜3 行)。現時点で誤動作しないため approve を妨げない — 修正はマージ後の任意タイミングでよい。

**r2-2(些末)予想修正開示の REIT 変種は not_statement でなく no_basis に計上される**。仕様には `EarnForecastRevision_REIT` 等の REIT 変種があり(r1 審査時に WebFetch で確認済み)、"_" 分割で 2 要素になるため `parts[1]="REIT"` → `no_basis` 側に落ちる(fundamentals.py:142-149)。データ上の帰結は同一(全 skip・誤書込なし)で、診断集計の分類が 1 カテゴリずれるのみ。この論点自体が `t029-initial-intake-verification` の body ②(予想修正開示の扱い再検討)の射程内なので、同リマインダー処理時に合わせて扱えばよい。

## 反対意見書(この approve が間違っている場合の理由トップ3)

1. **r2-1(デッドコードのガード)は「裁定と異なる実装」であり request_changes 相当だ** — 裁定は「進捗ゼロで break」を求めたが、実装のカウンタは進捗(マーカー刻印)でなく反復数を数えており、裁定の意図する防御を提供していない。*反論*: 裁定文言「進捗ゼロで break」の字義には一致しており、かつ現行コードでは promote_document の全経路 stamp により無限ループが構造的に起き得ないことを審査側でコード精読により確認した。防御の実効性欠如は「誤ったデータが書かれる」「停止しない」のどちらの実害も生まない。*代替案*: ガードを doc_id 進捗比較に置換してから approve — 往復 1 回分のコストに対し得られるのは仮想的な将来リスクの低減のみで、釣り合わない。マージ後修正で足りる。
2. **ローカルテストの pass は共有 DB の偶然で、クリーン環境の網羅性を保証しない** — Issue #142 系の環境失敗が今回たまたま出なかっただけで、テスト実行の証拠力は弱い。*反論*: 合否の正は指示書 §4 が明記するとおり CI(クリーン DB)であり、CI は審査対象 SHA 81b161f で green(run 30913650896)を客観確認済み。ローカル実行は補助証拠にすぎない。*代替案*: CI を再トリガして再現性を確認 — CI は決定論テストであり、同一 SHA での再実行に追加の証拠力はほぼない。
3. **C-2 の「翌日 00:00 JST」フォールバックは as_of を実開示より最大 24 時間遅らせ、データ鮮度を犠牲にする** — 保守化の代償として、DiscTime 欠測開示は当日の point-in-time 読出しに乗らなくなる。*反論*: 不変原則4 の非対称性(look-ahead は設計違反、遅延は単なる品質低下)に照らせば、遅い側に倒すのが唯一の安全な選択であり、r1 の裁定・修正指示そのものである。実開示の DiscTime 欠測率は初回取込検証(t029-initial-intake-verification)で実測される。*代替案*: 欠測を error 集計に落として書かない — r1 が併記した選択肢だが、書かない方が情報量で劣り、遅延付きでも書く現実装が優る。

## 判定

**approve**。r1 の全所見が裁定どおりに解消・繰延・維持されており、CI green(受け入れ基準1)を審査対象 SHA で確認した。新規所見は r2-1(デッドコードのガード、軽微)と r2-2(REIT 変種の skip 分類、些末)のみで、いずれも誤データを書く経路ではなく、マージを妨げない。r2-1 は次に fundamentals.py に触るタスクで直せばよい(必要なら reminders 登録は設計リードの判断に委ねる)。
