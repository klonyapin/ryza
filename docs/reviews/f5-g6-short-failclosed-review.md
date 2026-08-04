---
review: f5-g6-short-failclosed
reviewed_sha: 80cc22590b9662bb93b6f44ec613074e5930916b
reviewer: independent-officer
review_date: 2026-08-04
verdict: approve
---

# 独立役員審査:F-5 / G-6 新規ショート現金下限の fail-closed 化

**最終判定(第3ラウンド・80cc225): approve。** R-1(開示の事実訂正)・R-2(フリップ売りの即時 fail-closed 化)はいずれも充足(§9)。起草側が採った `post_cash = min(cash − delta×price, cash)` 定式化は、審査側が §8.5 で例示した単純丸め(`post_cash = cash`)より正しい — 単純丸めは部分カバー(ネットショート残の cover)の現金流出を無視して fc6e7bb 比 **fail-open** になることを counterfactual 実測で確認した(§9.3)。min 定式化は fc6e7bb に対する厳格な単調強化であり、全象限の実測で新たな抜け穴は見つからなかった。

## 0. 審査経緯

| 日付 | 対象 SHA | 判定 |
|---|---|---|
| 2026-08-04(初回) | `fc6e7bbed9e418cf6cd466ef4d63506085a3f686` | 条件付き承認(C-1 必須・C-2 推奨。§4–5 に原記録を保存) |
| 2026-08-04(再検証) | `eda02d7955bd12f44c1b75bedab10759043bd765` | request_changes(§8。開示の中心的主張「G-9 が全 block」が発効 config 下の実測で偽) |
| 2026-08-04(最終) | `80cc22590b9662bb93b6f44ec613074e5930916b` | **approve**(§9) |

## 1. 審査対象と手続き(初回・fc6e7bb)

- reviewed_sha `fc6e7bbe…` を `/tmp/review-f5` に checkout し、`f5-g6-short-failclosed` ブランチ head が同 SHA であることを `git rev-parse` で確認した(一致)
- 単一コミットの diff を `git show fc6e7bbe -- src/ryza/gate/compliance.py tests/gate/test_rules.py ops/reminders.yaml` で全量読解した
- `compliance.py` 全体(842 行)を読み、`ctx.delta` / `state.cash` / `side` を参照する全箇所を grep で列挙し、G-6 以外に同種の現金モデル依存箇所がないことを確認した
- `PYTHONPATH=/tmp/review-f5/src` で venv の python を用い、`pytest tests/gate/test_rules.py -q` を実行(69 passed / 0.09s)。DB 依存の `test_store.py` を除いて `pytest tests/gate/ -q` も 70 passed
- `ops/reminders.yaml` に `g6-short-margin-model` が登録され、date_after 2026-09-15、action=issue_create、対象=実弾移行前 精緻化、として記載されていることを確認した

## 2. 初回判定(fc6e7bb 時点の記録)

**approve-with-conditions**(条件は §5)。

修正は監査 A-12 の所見 F-5 に対して**方向性としては正しく厳しくなる**変更であり、追加テストは境界(cash = floor)を含めて挙動を固定している。ただし、コードモデル(`side == "short"` 文字列でのみ判定)には**フリップ売り(long→net short via sell)経由の抜け穴**が残る。この抜け穴は今回の PR の直接責任ではなく既存の問題だが、修正の主張(F-5 是正)を*ほぼ完遂した*と読ませるコメントと commit message の書き方には注意が必要で、担保モデル未実装の間に別途明示追跡すべきである。

## 3. 確認事項と根拠(初回)

### 3.1 fail-closed の方向性(観点 1)

- **修正前**: `post_cash = cash - delta*price`。`side == "short"` では `delta = -qty`(:778)。したがって `post_cash = cash + qty*price ≥ cash`。次行の早期リターン `if post_cash >= ctx.state.cash: return []` により G-6 は必ずスキップされていた
- **修正後**: `if ctx.proposal.side == "short": post_cash = ctx.state.cash`。早期リターンをバイパスし、`post_cash < floor` の判定に進む
- 修正前の G-6 判定結果は常に `[]`(pass)。修正後は `cash < floor` のときのみ `block`、それ以外は `pass`。したがって修正は**厳格な単調強化**(旧より緩くなるケースは存在しない)。適合

### 3.2 各 side の post_cash 式の妥当性(観点 2)

`delta = qty if side in ("buy", "cover") else -qty`(:778)を前提に:

- `buy`  : delta=+qty → post_cash = cash - qty*price(減少)。従来どおり。妥当
- `sell` : delta=-qty → post_cash = cash + qty*price(増加)→ 早期リターン。従来どおり。ロング清算の入金は自由現金として妥当
- `short`: delta=-qty → **本 PR の分岐**により post_cash = cash(不変)。売却代金を担保として非計上。fail-closed 近似として妥当
- `cover`: delta=+qty → post_cash = cash - qty*price(減少)。従来どおり。買戻しの現金流出評価は妥当

`compliance.py` 内で `state.cash` を参照するルールは G-6 のみ(`grep state\.cash` で 495〜501 と入力欠落チェック 729 のみ)。他 G ルール(G-8 レバレッジ、G-7 売買代金)は NAV とグロス/notional に基づくため、この現金モデル修正の波及なし。整合性を確認

### 3.3 テストの十全性(観点 3)

追加された 3 テスト(test_rules.py:392–453)を精査した:

- `test_g6_new_short_below_floor_blocks`: `cash = floor − 1`(floor=¥500,000)で block を確認。境界値の 1 円下側にヒットしている
- `test_g6_new_short_at_or_above_floor_passes`: `cash = floor` **ぴったり** で pass。実装 (`post_cash < floor`) の等号側を確実に固定している。境界の意味を「pass」側に決めていることが可視化されている(オフバイワン耐性)
- `test_g6_cover_evaluated_as_cash_decrease`: cover を「元の long 保有(qty=-500)を 100 覆う」で組み、`cash = floor+5万`(post_cash=floor-5万 → block)と `cash = floor+15万`(post_cash=floor+5万 → pass)の**両側**を検証。回帰として十分

いずれも実装の写経ではなく、`cash` と `floor` の相対関係で境界を組んでおり、実装ミスに対して独立性がある。専用の緩マンデート `_loose_short_mandate`(pod_gross_leverage_limit=10.0・concentration=0.99・short=True・universe=jp_equity_cash)は G-6 を単独で試験するために G-8/G-9 の別条項を意図的に緩めている ― これは合理的な範囲だが、テストが G-9 の short_allowed に依存していることは記録した(IPS `short_selling.allowed: true`)

追加テスト以外の既存 69 件も含めて全件 pass(70 passed in 0.54s)

### 3.4 簡略化の明示と将来の布石(観点 4)

- `_g6_cash_floor` の docstring は「新規ショートの売却代金は担保拘束であり自由現金ではない」「担保モデル導入までの fail-closed 側の近似」を明確に記述(:481–493)
- インライン `# ショートの売却代金は担保拘束 → 自由現金には加算しない(fail-closed 近似)` も配置(:497)
- docstring 末尾に `TODO(g6-short-margin-model / ops/reminders.yaml)` の対応リマインダー参照あり
- `ops/reminders.yaml` に `id: g6-short-margin-model` を新規登録。date_after 2026-09-15、action=issue_create、body に「必要担保・維持証拠金・avg_cost 考慮・実弾移行前完成」の具体条件記載。制度化の要件(CLAUDE.md「将来アクションの制度化」)を満たしている

保守的側への倒し方: 売却代金全額を「非自由」として扱うのは、実際の委託保証金率(通常 30%〜)より厳しく資金拘束を過大評価している。これは**機会損失側にのみ効く保守化**であり、G-6(現金下限を割らせない)という規則趣旨に対しては正しい方向。承認可

### 3.5 スコープと意図外変更(観点 5)

コミット(diff)は3ファイルに限定:
- `src/ryza/gate/compliance.py`(_g6_cash_floor のみ変更・+23/-4)
- `tests/gate/test_rules.py`(追加のみ)
- `ops/reminders.yaml`(+12、`g6-short-margin-model` 1件追加のみ)

意図外変更なし。保護領域(コンプラゲート)への修正が最小差分で行われている

## 4. 所見(重要度付き・初回。O-1 の重要度は §8.4 で改訂)

### O-1【中 → 高に改訂(§8.4)】フリップ売り経由の抜け穴が残る(既存問題・本 PR の直接責任ではないが要追跡)

判定は `ctx.proposal.side == "short"` の文字列一致のみで行われる。しかし、既存ロング(qty=+A)を保有した状態で `side="sell"` かつ `qty > A` を送信すると、post_pod_qty は負(=新規ショートポジションが生じる)にもかかわらず、G-6 は `sell` として `post_cash = cash + qty*price` を計上する(超過分の売却代金も自由現金として)。当該フリップ売りにおいて超過分は担保拘束されるべき代金だが、G-6 は素通しする

これは既存の設計であり本 PR で導入されたものではないが、コミット message や docstring の「新規ショート建て」表現は**この抜け穴の存在を隠す**書き方になっている。修正の主張は「short 経路の穴埋め」にとどまり、`post_pod_qty < 0` を条件に含める踏み込みは行われていない

- 参考: G-9(_g9_short)は `shorting = ctx.proposal.side == "short" or ctx.post_pod_qty < 0` として両方を捕捉している(:574)。G-6 だけが `side` 単独判定であり非対称
- 提案: 完全 fix は `if ctx.proposal.side == "short" or ctx.post_pod_qty < 0: … 担保拘束分を差し引く` の踏み込み。今 PR で行わないなら、抜け穴の存在を docstring / reminders に明示する

### O-2【低】docstring の「現金が増える注文は除く」の説明が現状不完全

修正後の 501 行のインラインコメント「売り等で現金が増える注文は現金下限を悪化させない」は sell を念頭にした従来文言だが、`else` 節に入ったあとの条件付きスキップになっており、「short 以外」の全 side を対象とする早期リターンとしては誤解を招かない書きぶりになっているものの、`sell` と `cover` と `buy` の3経路が同一ロジックに合流している点は明示すると読解が楽になる。ミス誘発ではないので低

### O-3【低】テストで cover の初期条件が独立

`test_g6_cover_evaluated_as_cash_decrease` は既存ショート `PositionState(qty=Decimal(-500))` を持たせる。cover の判定に「事前ショート保有」は G-6 の post_cash 計算には不要(cover は buy と同じ `delta=+qty` で処理される)。テストはより保守的に事前状態を作っているだけで、G-6 単独評価としては qty=+100 の buy と等価。読解時に「なぜ既存ショートが要るのか」を迷わせるので、コメント一行で補足すると親切(必須ではない)

### O-4【情報】G-6 の等号扱い(pass 側)は暗黙の決定

`post_cash < floor` なので `cash == floor` は pass。IPS §4.2 の文言(NAV の 5% 以上)と整合。テスト(`test_g6_new_short_at_or_above_floor_passes`)がこの規約を固定している。妥当

### O-5【情報】担保モデル未実装の期間中の運用上の帰結

短期のショートは実質的に「現金が floor 以上あるときのみ許容」される。デモ / paper では安全側に倒れているが、実弾では機会損失側に効く。reminders.yaml の date_after=2026-09-15 は実弾移行前提。IPS の short_selling.allowed=true が生きていることと整合

## 5. 条件付き承認の条件(初回の記録)

- **C-1【必須・軽量】**: 所見 O-1 の抜け穴(sell によるフリップ→net short)の存在を、`_g6_cash_floor` docstring と `ops/reminders.yaml` `g6-short-margin-model.body` の両方に**明示追記**すること。追加 issue を切るまでもなく、テキストで残せば十分。担保モデル導入時に併せて修正することが条件
- **C-2【推奨】**: 所見 O-1 の完全 fix(`or ctx.post_pod_qty < 0`)を、本 PR ではなく担保モデル(g6-short-margin-model)導入時のスコープに正式に含めること

C-1 を満たせば approve(担保モデル本体は別 PR で構わない)。

## 6. 保護領域手続き

本変更はコンプラゲートに触れる。定款第5条・governance.yaml の protected_areas の対象。独立役員審査 → #承認 通知 → 48h みなし承認、または代表の明示承認、いずれかで発効させること。実弾ではないため 3 専決の明示承認対象ではない(みなし承認可)。**判定が request_changes の間は発効不可**(src/ryza/reviews.py BLOCKING_VERDICTS)。

## 7. 検証記録(初回)

- `pytest tests/gate/test_rules.py -q` → 69 passed / 0.09s(新規 3 件を含む)
- `pytest tests/gate/ -q --ignore=tests/gate/test_store.py` → 70 passed / 0.54s
- DB 依存の test_store.py は本 PR の変更点に触れていない(store 側は無変更)ので未実行

## 8. 再検証(eda02d7・2026-08-04)

### 8.1 対応内容の確認

`git show eda02d7` は docs のみの変更(`src/ryza/gate/compliance.py` の docstring +11 行、`ops/reminders.yaml` の `g6-short-margin-model.body` 追記)。判定ロジックの変更なし。`pytest tests/gate/ -q` は **89 passed**(test_store.py 含む全件)を worktree head = eda02d7 で確認した。

### 8.2 C-1 判定: **未充足** — 開示の中心的主張が実測で偽

追記された開示文(docstring :495–497、reminders body (4))は次を主張する:

> 現状このフリップは G-9 が `side == "short" or ctx.post_pod_qty < 0` で全て block するため無防備な経路は存在しないが、ショート解禁(G-9 緩和)の際に G-6 だけこの非対称が残ると穴になる。

これは**発効 config に対して事実に反する**。ショートは既に解禁されている(`config/ips.yaml` §5 `short_selling.allowed: true`、`config/mandates/stan.yaml` `short: true`)。G-9 はフリップを「捕捉」した上で、IPS 不許可・個別銘柄上限超過・マンデート禁止(peter/ben の `short: false`、jim の `hedge_futures_only` 非充足)の**いずれかに該当するときだけ** block する。`short: true` の stan には該当条項がなく素通しする。

**実測(意見は証拠で解決・議論規約4)**: worktree head = eda02d7、発効 config(`load_and_validate()`)、`evaluate()` 直接呼び出しで再現した。NAV ¥10,000,000、floor = ¥500,000、cash = floor − 1:

| ケース | 注文 | 結果 |
|---|---|---|
| A | stan・index_etf・保有 +100 株・`side="sell"` qty=300(→ post_pod_qty = **−200**) | **pass**(G-9 も G-6 も無反応) |
| B | stan・index_etf・`side="short"` qty=200(同一のネットショート) | **block**(G-6: 約定後現金 ¥499,999 < NAV の 5%) |

同一の経済行為(現金 < floor でのネットショート 200 株形成)が、side 文字列だけで block/pass に分かれる。抜け穴は「将来の G-9 緩和時に生じる」のではなく、**今日、発効 config 下で live** である。誤った安全主張(「無防備な経路は存在しない」)を保護領域の docstring と将来 issue の本文に恒久化することは、C-1 が求めた「抜け穴の明示開示」の逆であり、初回審査 O-1 の記載(「G-6 は素通しする」)とも矛盾する。

### 8.3 C-2 判定: 形式充足・実質未充足

対称化(`or ctx.post_pod_qty < 0`)を `g6-short-margin-model` のスコープに含めた点は形式的には C-2 のとおり。しかし「**G-9 緩和と同時に実装、単独で先行しない**」という条件は、8.2 のとおりショートが既に解禁済みである以上、**発生しない将来イベントの後ろに是正を無期限に先送りする**指示になっている。誤前提の上に是正計画を固定するため、実質未充足と判定する。

### 8.4 所見の改訂

- **O-1 の重要度を【中】→【高】に引き上げる**。初回審査は経路の存在を正しく記載した(§4 O-1「G-6 は素通しする」)が、「テキスト開示で十分【軽量】」とした C-1 の重み付けは、経路が実際に到達可能かを発効マンデート(stan `short: true`)まで突合せずに行ったものだった。実測により live と確認されたため改訂する。この点は審査側の自己訂正として記録する

### 8.5 要修正事項(request_changes の解消条件)

- **R-1【必須】開示の訂正**: `_g6_cash_floor` docstring と `g6-short-margin-model.body` の「現状は G-9 が全て block する/無防備な経路は存在しない」「ショート解禁(G-9 緩和)の際に」を削除し、事実に合わせる — フリップ経路は発効 config(IPS `short_selling.allowed: true` + `short: true` マンデート)下で**現に pass する**こと、G-9 が block するのはショート禁止マンデート(peter/ben)と `hedge_futures_only` の非充足(jim)に限られること
- **R-2【必須】是正の再スケジュール**: 「G-9 緩和と同時・単独先行しない」を削除する。live な非対称であるため、(a) 本 PR(または直後の同系 PR)で fc6e7bb と同型の fail-closed 化 — `if ctx.proposal.side == "short" or ctx.post_pod_qty < 0: post_cash = ctx.state.cash` — と境界テスト(フリップ売りで cash < floor → block / cash ≥ floor → pass、通常 sell の回帰)を実装する、または (b) 実装を先送りするなら reminders の期日を「G-9 緩和時」ではなく直近の独立期日にする。**推奨は (a)**: 変更は fc6e7bb と同じく厳格な単調強化であり、担保モデルの完成を待つ理由がない(フリップ中のロング解消分まで非計上にする過剰保守は、担保モデル導入時に精緻化すればよい)
- R-1・R-2 を満たしたコミットで再審査に付すこと。(a) を採る場合、当該コード変更もコンプラゲート(保護領域)の変更として本審査ラインで確認する

### 8.6 再検証の検証記録

- `git rev-parse HEAD` = `eda02d7955bd12f44c1b75bedab10759043bd765`(worktree、branch f5-g6-short-failclosed)
- `PYTHONPATH=src pytest tests/gate/ -q` → 89 passed / 1.34s
- フリップ実測スクリプト: `evaluate()` を発効 config で直接呼び出し(§8.2 の表)。stan マンデート v2(universe に index_etf・`short: true`・`additional_prohibitions: [single_name_equity]`)を使用し、`is_single_name=False`・`universe_tags=("index_etf",)` で G-2/G-3/G-9 の他条項に抵触しないことを確認済み

## 9. 最終ラウンド(80cc225・2026-08-04)

### 9.1 対象と手続き

- worktree head = `80cc22590b9662bb93b6f44ec613074e5930916b`(`git rev-parse HEAD` 一致・working tree clean)
- `git show 80cc225` で diff 全量を読解(3ファイル: `src/ryza/gate/compliance.py` の `_g6_cash_floor` のみ、`tests/gate/test_rules.py` +3 テスト、`ops/reminders.yaml` body 1件)。意図外変更なし
- `PYTHONPATH=src pytest tests/gate/ -q` → **92 passed / 1.22s**(test_store.py 含む全件。89 + 新規 3)

### 9.2 R-1 判定: **充足** — 開示が事実に訂正された

- docstring(:491–504): 「G-9 が全て block するため無防備な経路は存在しない」「ショート解禁(G-9 緩和)の際に」を削除し、「IPS はショート許可・stan マンデートも `short: true` のため G-9 はこの経路を止めず、**抜け穴は現に有効だった**」と §8.2 の実測どおりに記述。min 定式化の理由(単純丸めは部分カバーで fail-open)と残る過保守(ロング解消分の代金も数えない)も開示している
- `ops/reminders.yaml` `g6-short-margin-model.body` (4): 偽の安全主張を削除し、「対称化済み(F-5 是正で実装 — 発効 config 下で抜け穴が現に有効だったため即時 fail-closed 化)」に書き換え。残課題を「フリップ売りのロング解消分代金の算入(担保モデル導入時に約定を分解)」に正しく縮小
- 軽微(非ブロッキング): body 冒頭の「現状: side == "short" で post_cash = ctx.state.cash」は min 定式化前の文言のまま。side == "short" では min = cash となるため文字どおりには偽ではなく、(4) が補正しているが、担保モデル実装時に読み替えに注意

### 9.3 R-2 判定: **充足** — min 定式化は審査側の例示より正しい

R-2 は §8.5 で `post_pod_qty < 0` 時に `post_cash = ctx.state.cash` とする例示を示したが、起草側はこれを退けて `post_cash = min(cash − delta×price, cash)` を採用した。**起草側が正しい**(意見は証拠で解決・議論規約4):

- 審査側例示の単純丸めでは、部分カバー(cover 後も `post_pod_qty < 0`)が分岐に入り、買い戻しの現金流出(`delta > 0` → 実 post_cash = cash − qty×price)を cash に丸めて無視する。fc6e7bb は cover を常に現金減として評価していたため、これは**厳格化ではなく緩和(fail-open 回帰)**であり、既存回帰テスト `test_g6_cover_evaluated_as_cash_decrease` が検出する — counterfactual 実測(下表 E1′)で確認した
- min 定式化の単調性(fc6e7bb 比): sell/short は `delta < 0` → 生の post_cash > cash → min = cash(旧早期リターン箇所に評価を追加 = 厳格化のみ)。cover/buy で `post_pod_qty < 0` の場合は `delta > 0` → 生の post_cash ≤ cash → min = 生値(旧 else 節と同一)。分岐非該当は旧コードと同一。**旧より緩くなる象限は存在しない**

**全象限の実測**(worktree head = 80cc225、発効 config `load_and_validate()`、stan・index_etf・NAV ¥10,000,000・floor ¥500,000・price ¥1,000。スクリプト `/tmp/g6-min-quadrants.py`):

| ケース | 注文 | cash | 結果 | 判定 |
|---|---|---|---|---|
| A | 保有 +100・sell 300(フリップ → −200) | floor − 1 | **block**(G-6) | §8.2 の live 抜け穴が閉鎖(eda02d7 では pass) |
| B | short 200 | floor − 1 | block(G-6) | 回帰(fc6e7bb と同一) |
| C | 保有 +100・sell 300(フリップ) | floor | pass | side="short" と等号側も一致 |
| D1 | 保有 −100・short 100(増し玉) | floor − 1 | block(G-6) | 担保拘束扱い |
| D2 | 保有 −100・sell 100(sell 経由の増し玉) | floor − 1 | block(G-6) | side 文字列迂回も閉鎖 |
| E1 | 保有 −500・cover 100(部分カバー) | floor + 5万 | block(G-6) | 現金流出を計上(fc6e7bb と同一) |
| E2 | 同上 | floor + 15万 | pass | 両側境界 |
| E1′ | E1 を単純丸め版(counterfactual・monkeypatch) | floor + 5万 | **pass** | **単純丸めの fail-open を実証** — 起草側主張は正 |
| F | 保有 −100・cover 100(全量カバー・post=0) | floor + 5万 | block(G-6) | 分岐非該当・旧来の現金減評価のまま(担保解放の非計上は過保守側 — reminders (3) の既存スコープ) |
| G | 保有 +100・sell 100(全量売却・post=0) | floor − 10万 | pass | 現金増の早期リターン回帰 |
| H | 保有 +300・sell 100(通常の一部売却) | floor − 10万 | pass | 通常 sell 無影響 |

追加テスト 3 件(フリップ下限未満 block / 下限以上 pass / 全量売却境界回帰)は A・C・G を固定しており、E1/E2 は既存回帰テストが固定している。テストと実測の不一致なし。

### 9.4 残存事項(いずれも非ブロッキング・開示済み)

- フリップ売りでロング解消分の代金(真に自由現金になる部分)も数えない過保守 — docstring・reminders (4) に開示済み。担保モデル導入時(`g6-short-margin-model`・date_after 2026-09-15・実弾移行前)に精緻化
- 全量カバー(F)が担保解放を見ずに block し得る過保守 — 本コミット由来ではなく fc6e7bb 以前からの挙動。reminders (3) の既存スコープ
- §9.2 の body 冒頭文言の軽微な陳腐化

### 9.5 保護領域手続(発効条件)

本判定(approve)により審査ブロックは解消。コンプラゲート変更として、#承認 通知 → 48h 異議なしのみなし承認、または代表の明示承認で発効させること(実弾ではないため 3 専決対象外)。

### 9.6 検証記録(最終)

- `git rev-parse HEAD` = `80cc22590b9662bb93b6f44ec613074e5930916b`(branch f5-g6-short-failclosed)
- `PYTHONPATH=src pytest tests/gate/ -q` → 92 passed / 1.22s
- 象限実測+counterfactual: `/tmp/g6-min-quadrants.py`(counterfactual は `ryza.gate.compliance._g6_cash_floor` を単純丸め版に一時差し替え — `evaluate()` は規則関数をモジュールグローバル経由で解決するため有効。実行後復元)
