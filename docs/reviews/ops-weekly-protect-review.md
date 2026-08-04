# ops-weekly 保護領域登録(weekly.py / test_weekly.py)— 独立役員審査

- 審査日: 2026-08-04 / 審査者: 独立役員(非執行・批判専任)
- 対象: worktree `ops-weekly-protect` の `origin/main..HEAD` 1 コミット(`e7bab41`)—
  `config/governance.yaml` に `src/ryza/ops/weekly.py`(area: ops_engine 新設)と
  `tests/ops/test_weekly.py`(area: invariant_tests)を登録、`ops/reminders.yaml` の
  陳腐化エントリ 1 件を done 化+スキーマ注釈に notify 型を追記
- 判定: **条件付き承認**(重要 2 件の是正をマージ前提条件とする)
- 検証方法: 実証主義 — 全主張をミューテーション・A-18 実走・git 履歴で自分で確かめた。
  伝聞での受理は無い

## 検証済み — 起草者の主張のうち実測で裏が取れたもの

登録の骨子(weekly.py はリマインダー制度の唯一の執行点であり、発火しない改変は例外も
CI 赤も出さず静かに空回りする)は正しく、遡及・前向きの両方向で A-18-1 の実効性を確認した。

1. **遡及主張は正確**: 批准コミット `c7af81ef`(2026-08-03 15:54 JST)以後に weekly.py /
   test_weekly.py へ触れたのは 9 コミット+統合マージ `2e5ef930` の計 10 件で、全て PR
   ブランチ内(first-parent 外)。`run_a18('.', verify_prs=False)` の実走で main の承継
   9 件 → 登録後 10 件、weekly 起因の violations 0 を確認。`2e5ef930` の起点は
   PR #80 マージ(`580be474`)で主張どおり。初版 `78e9cc8`(2026-08-02 15:45)は批准前で
   検査範囲外 — 日時を git で確認した。**受容登録不要の主張は成立する**
2. **前向き検出は機能する**(プローブ実施): weekly.py へトレーラ無しの直コミットを積んで
   run_a18 を実走 →「main への直接コミットで Approved トレーラなし」として violation に
   計上された(プローブ後に reset 済み)
3. **ミューテーション 4 種は全て登録テストで検出**(各実施→revert 済み):
   - `RESOLUTION_STATUS_DELEGATED` を「スキップ(...)」へ戻す →
     `test_default_resolution_line_points_at_the_reporter_not_skip` が失敗
   - `run_a18_if_configured` から `always_report=True` を外す →
     `test_run_a18_always_reports_and_shows_acknowledged_and_inherited` が失敗
   - ダイジェストから A-18 行を削る → 2 テスト失敗
   - `TERMINAL_STATUS_PREFIXES` から `done` を外す(PR #111 バグの再導入)→
     `test_done_entries_do_not_fire` が失敗
4. **done 化の根拠は正しい**: `src/ryza/risk/**`(risk_limits)・`src/ryza/gate/**`
   (compliance_gate)は main の governance.yaml に登録済みで、導入コミット `47bd497` は
   PR #67 マージ(`f08f137 Merge pull request #67 from klonyapin/l1-protect-risk`)経由 —
   リマインダー本文の PR 番号まで一致。「リマインダー更新漏れ」の整理として妥当
5. CI(`.github/workflows/ci.yml`)は保護済み(area: ci)で `pytest tests/ -q` を required と
   して実行する — invariant_tests 登録が実効性を持つ前提(テストが必ず走る)は成立している

## 重要(マージ前提条件)

- **重要-1 github.py の書き込み側迂回口は実測で開いている — 最小是正は同 PR で可能**。
  起草者自身の異議(get_file 空返し・update_file no-op 化で weekly.py に触れず空回りできる)
  を両方向プローブした:
  - 読み側: `get_file` を `("", "")` 返しに改変 → 登録済み
    `test_dry_run_real_client_makes_no_write_requests` が失敗。**塞がっている**
  - 書き側: `update_file` と `create_issue_comment` を no-op 化 → **登録済み 31 テストは
    全て緑**。検出するのは未登録の `tests/ops/test_github.py` のみ(2 テスト失敗を確認)
  つまり governance.yaml コメントの暫定担保の主張(「StubClient 群と dry-run 実クライアント
  テストが読み書きの契約を固定している」)は**半分しか正しくない** — 固定されているのは
  読みだけで、書き(status コミット・action 配送・ダイジェスト投稿)は StubClient 側の
  偽実装が担っており、実クライアントの書き込み契約はどの登録テストにも固定されていない。
  また「glob(src/ryza/ops/**)化は無関係な運用スクリプトまで巻き込む」という見送り理由は、
  単一ファイル `src/ryza/ops/github.py` の登録には当てはまらない — glob の難点を単一登録の
  見送り理由に転用するのは論理のすり替えである。
  **是正(最小・本 PR 内)**: `tests/ops/test_github.py` を invariant_tests へ追加登録する。
  CI が required なので、書き込み経路の no-op 化はテスト改変(=承認必須)なしには通らなく
  なる。github.py 本体の登録(ops_engine か単独 area)は独立審査に送ってよいが、その場合も
  テスト側の登録は今回やらない理由が無い

  **裁定: 採用(本 PR で是正)**。審査のプローブを自分で再現した — `update_file` と
  `create_issue_comment` を no-op 化すると `tests/ops/test_weekly.py` の 31 件は全て緑で、
  落ちるのは `tests/ops/test_github.py` の 2 件のみ(`test_update_file_sends_base64_and_sha`・
  `test_create_issue_comment_posts_body`)。件数まで審査の記述と一致した。
  `tests/ops/test_github.py` を `area: invariant_tests` で登録した。登録基準への適合は
  「GitHub REST API の**ワイヤ様式のゴールデン**を持つこと」に置いた —— PUT contents の body
  (base64 本文+更新対象 sha)、POST issue comments の body 形、認証・API バージョンヘッダ、
  dry_run 時に opener へ 1 件も届かないこと(`op.records == []`)。いずれも実装から導出した値では
  なく外部 API 契約の写しであり、先に書き換えれば書き込みの無害化が緑のまま通る。
  「glob の難点を単一登録の見送り理由に転用した」という論理のすり替えの指摘も**受け入れる**。
  governance.yaml の見送り理由を書き直し、(1) 本体の登録要否は別途判断(下記 重要-2 の
  リマインダー)、(2) それまでの担保は test_github.py の invariant 登録である、の2点に改めた。
- **重要-2 「独立の審査に送る」が YAML コメントにしか存在しない — CLAUDE.md 違反**。
  「将来のアクションは必ず `ops/reminders.yaml` に機械可読で登録すること」(将来アクションの
  制度化)に対し、github.py の保護判断の送付は governance.yaml のコメント(=セッション内の
  約束と同じく誰も発火させない場所)に書かれているだけである。皮肉なことに、本 PR は
  「リマインダー更新漏れ」を done 化しながら、同じ形の更新漏れを新規に作っている。
  **是正**: github.py(+中-4 の reminders.yaml 遷移監査)の保護審査を追跡する
  リマインダーを ops/reminders.yaml に追加する

  **裁定: 採用(本 PR で是正)**。指摘のとおりで、弁解の余地が無い —— 「リマインダー更新漏れ」を
  done 化する PR が同じ形の更新漏れを新規に作っていた。`ops/reminders.yaml` に
  `github-client-protect`(期限 2026-08-18・issue_create)を追加した。body は自己完結にし、
  迂回口の内容(get_file 空返し / update_file・create_issue_comment の no-op)・重要-1 の
  両方向プローブの実測・暫定担保(test_github.py の invariant 登録と CI required)・
  決めること(github.py を protected_areas に登録するか、area は ops_engine か新設か)・
  判断材料(icon_revalidate との共用、glob 登録の先例、テスト登録だけで足りるかの検証)を書いた。
  中-4 は独立の関心事(対象が reminders.yaml 自身)なので束ねず、別エントリに分けた(下記)。

## 中

- **中-3 実データ done 検査は現状 vacuous — 登録根拠 4 項目のうち 1 項目は過大表示**。
  `test_real_reminders_yaml_done_entries_never_fire` は NOW=2026-08-02 固定だが、実ファイルの
  done 55 件の条件は**全件が未来日の `date_after` のみ**(2026-08-05〜)。よって done ガードを
  外しても(ミューテーションで実証)このテストは緑のまま — 何も発火しない日付で「発火しない」
  ことを確かめている。今回の regression 防御は合成データ側(`test_done_entries_do_not_fire`)
  だけが担っている。**是正案(1 行)**: 実データテストの NOW を遠未来(例 2999-01-01)にし、
  done ガードだけが発火を防ぐ状態にする。governance.yaml の登録根拠コメントの「実データ検査」
  も現状の効力に合わせて修正すること

  **裁定: 採用(本 PR で是正)**。vacuous であることを自分で実測した。`TERMINAL_STATUS_PREFIXES`
  から `done` を外した状態(= PR #111 のバグの再導入)で、実ファイルに対する発火を両時点で比較:

  | 現在時刻 | done エントリの発火 | 検査の結果 |
  |---|---|---|
  | 旧 `NOW=2026-08-02` | 0 件 | 緑(vacuous — 何も発火しない時点で「発火しない」を確認) |
  | 新 `far_future=2999-01-01` | **55 件** | 落ちる(done ガードだけが止めていることを検出) |

  是正は審査案どおり現在時刻の遠未来固定(2999-01-01)。加えて2点を足した:
  (1) 「done エントリの `date_after` が全て far_future より前」を**前提として assert** する —
  将来 reminders.yaml にもっと先の日付が入っても、検査が黙って vacuous に戻らないようにするため。
  (2) `failures` 側も突き合わせる(done エントリは action を試行すらされないので failures にも
  現れない)。当初 `set(outcome.failures) & done_ids` と書いたが `failures` は `(id, 要約)` の
  タプル列で常に空集合になる —— **是正で新しい vacuous を作りかけた**ので、id を取り出す形に直した。
  governance.yaml の登録根拠コメントも、当初 vacuous だった経緯と是正後の効力を明記する形に修正。
- **中-4(敵対的観点・自発)制度の最安価な迂回は weekly.py でも github.py でもなく
  reminders.yaml の status 書き換えである**。pending エントリを `done` にする 1 行で、
  将来アクションは承認も検出も無しに消える(reminders.yaml は protected_areas 外)。
  本 PR 自身がこのチャンネルを行使している — 今回は根拠付きで正当だが、根拠の添付を強制する
  仕組みは何も無い。単純な保護登録は**不可**(ジョブ自身が Contents API で main へ直接
  `fired:` コミットを書くため、登録すると正常動作が毎週 A-18 違反になる)。対案: (a) 週次
  ダイジェストに「今週 status 遷移したエントリ」を diff ベースで列挙する、(b) A-18 に
  reminders.yaml の pending→done 遷移コミットの検査(ジョブの `fired:` コミットは除外)を
  足す。重要-2 のリマインダーに束ねて独立審査へ送ること

  **裁定: 採用(本 PR でリマインダー登録)**。分析に同意する —— 本 PR 自身がこのチャンネルを
  2 回行使しており(`meta-runs-status-check` の確認と
  `risk-protected-areas-registration-followup` の done 化)、正当性を担保したのは起草者の裁量と
  この審査であって仕組みではない。「単純な保護登録は不可(ジョブ自身の `fired:` コミットが
  毎週違反化する)」の指摘も正しく、保護領域化は本件の解ではない。
  `ops/reminders.yaml` に `reminders-status-tamper-detection`(期限 2026-08-25・issue_create)を
  追加した。body には中-4 の分析(最安価の迂回である理由・単純登録が不可である理由)と対案
  (a)(b) を自己完結で書き、判断のポイント((b) のジョブ除外条件が件名偽装で回避されうるので
  author/committer 側に寄せられるかの検証、(a) を先行させて遷移頻度を実測してから (b) の閾値を
  決める順序)まで残した。
  **審査の指示から1点だけ外した**: 「重要-2 のリマインダーに束ねる」ではなく別エントリにした
  (設計リード指示)。理由は対象と決定主体が異なるため —— 重要-2 は `src/ryza/ops/github.py` の
  保護登録の要否、中-4 は `ops/reminders.yaml` の遷移監査の設計であり、束ねると一方の判断が
  他方を待つ。期限も 2026-08-18 / 08-25 と分けた。

## 軽微

- **軽微-5** governance.yaml コメントの「violations は 0 のまま」はマージ後にのみ成立する。
  現ブランチ HEAD での実走は violations 1 — 登録コミット `e7bab41` 自身(governance.yaml =
  保護領域に触れ、トレーラ未付与)。承認 PR 経由のマージで (b)/(c) 救済される想定の正常な
  経過状態だが、マージ時に様式 v2(`reviewed=<sha40>`)のトレーラを忘れると自分が違反として
  列挙される

  **裁定: 了(起草者側の作業なし)**。指摘は正確で、本ブランチ HEAD での実走も violations 1
  (本ブランチ自身のコミットのみ)である。マージは様式 v2 のトレーラを付けて統合側が行う
  (設計リード指示)。governance.yaml のコメントは「登録後に実走した結果」を書いたもので
  マージ後の状態を指しており、記述としては誤っていないため据え置いた。
- **軽微-6** 登録根拠の「RESOLUTION_STATUS_DELEGATED の文言の写し」は正確には写しではなく
  性質検査(「スキップ」不含+「A-18-6」含む)。ミューテーションで落ちることは実証済みなので
  実効性に問題は無いが、「A-18-6 を含み スキップ を含まない任意の文字列」への置換は通る。
  表現は効力どおりに書くこと

  **裁定: 採用(本 PR で是正)**。governance.yaml の登録根拠を「文言の写し」→「**性質検査**」に
  改め、「逐語の写しではないため『A-18-6 を含み スキップ を含まない任意の文字列』への置換は通る」
  という限界も併記した(効力どおりに書く)。検査を逐語一致へ強化しなかったのは、
  `RESOLUTION_STATUS_DELEGATED` は報告文面であり文言の推敲が正当に起こりうるため —— 逐語固定は
  文面の改善のたびに保護領域の変更手続を呼び、統制を増やさずに摩擦だけを増やす。

## 反対意見書(この登録が間違っている場合の理由トップ3+代替案)

1. **保護の重心が誤っている可能性**: 実測どおり、書き込み経路の無害化は weekly.py に
   触れずに可能(重要-1)で、制度の取消は reminders.yaml の 1 行で可能(中-4)。weekly.py
   単独の登録は「三面ある入口の一面だけ施錠した」状態であり、施錠済みの表示が残り二面への
   注意を下げるなら、登録前より安全性の体感と実態の乖離は広がる
2. **ops_engine という area 新設の根拠が薄い**: 現状 1 ファイルのための新 area。既存の
   deploy_path(同ジョブのデプロイ経路)か audit(統制の実行主体)へ寄せる選択肢は
   検討の跡が無い。area が増えるほど棚卸しの分母が増える
3. **invariant_tests 登録は攻撃面も増やす**: test_weekly.py の変更に承認が要るようになる
   ため、テストの正当な強化(中-3 の是正を含む)にも摩擦が付く。凍結に値する参照は
   4 主張のうち実効 3 つ(中-3)であり、ファイル全体の凍結が最小手段かは自明でない
   — 代替案: 参照を持つテストだけを別ファイルへ分離して登録する

いずれも登録自体を覆すほどではない(1 は追加登録で解消可能、2・3 は運用コストの問題)と
評価するが、重要-1・2 を放置したままの承認には反対する。

## 結論

weekly.py / test_weekly.py の登録は妥当であり、遡及・前向きの実効性は実測で裏付けられた。
done 化も根拠正確。ただし**重要-1(tests/ops/test_github.py の invariant_tests 追加登録)と
重要-2(github.py 保護審査の reminders.yaml 登録)を本 PR 内で是正することをマージ前提条件
とする**。中-3(実データテストの NOW 遠未来化)は同 PR での 1 行是正を強く推奨する。

## 設計リード裁定(2026-08-04・審査後)

判定「条件付き承認」を受け、**重要-1・重要-2・中-3・中-4・軽微-6 の 5 件すべてを本 PR 内で
是正した**(各所見の下の裁定を参照)。後追いに回した所見は無い。軽微-5 のみ統合側の作業
(トレーラ付きマージ)。是正は同一ブランチの追加コミットで行い、意見書は改変せず**追記**で
裁定を残す(監査証跡の連続性 — 意見書の書換は起草者による審査結果の改変にあたる)。

是正の主張はすべて自分で再実測した(伝聞で受理しない):

- 書き込み経路の no-op 化 → test_weekly.py 31 件は全て緑、test_github.py の 2 件のみ失敗(重要-1)
- done ガード除去 → 旧 NOW では done の発火 0 件で緑(vacuous)、far_future では 55 件が発火して
  検査が落ちる(中-3)

### 反対意見書への応答

- **1(保護の重心)— 採用**。「三面ある入口の一面だけ施錠した」という指摘が本 PR の是正の
  骨格になった。書き側は test_github.py の登録で閉じ、reminders.yaml の status 経路は
  `reminders-status-tamper-detection` で追跡する。**「施錠済みの表示が残り二面への注意を下げる」
  という懸念に対しては、governance.yaml の該当コメントに残る穴と暫定担保を明記して対応した** —
  登録の事実だけが見えて限界が見えない状態にしない。
- **2(ops_engine 新設の根拠が薄い)— 一部採用・現状維持**。検討の跡が無いという指摘は正しい
  ので、ここに残す。`deploy_path` に寄せなかったのは、同 area の他の登録(deploy-*.sh・ops/lib/**・
  Dockerfile 群)が「稼働コードをどう配置するか」の面であるのに対し weekly.py は稼働コード自身で
  あり、混ぜると area 単位の棚卸しで「デプロイ経路を全部見る」ときの対象がぶれるため。`audit` に
  寄せなかったのは、週次ジョブの主務がリマインダー発火(統制の**執行**)であって監査(**発見**)
  ではないため —— A-18 の実行はジョブの一機能にすぎない。ただし「area が増えるほど棚卸しの分母が
  増える」は実コストであり、`github-client-protect` の判断時に ops_engine の範囲(weekly.py 単独か
  ops 配下のジョブ全体か)を併せて決めるのが自然なので、そのリマインダーの判断材料に含めた。
- **3(invariant_tests 登録は攻撃面も増やす)— 反証あり・不採用**。「テストの正当な強化にも摩擦が
  付く」は事実だが、**中-3 の是正が本 PR 内で摩擦なく通ったこと自体が反例**である(同一 PR・同一
  審査の中で強化できた)。「参照を持つテストだけ別ファイルへ分離」の代替案は、分離の境界自体が
  無審査で動かせる(参照を非保護ファイル側へ移せば凍結が外れる)ため、二段手順を塞ぐという
  登録の目的に対して弱い。ファイル全体の登録は phase-1/phase-2 の先例(tests/gate/test_rules.py 等)
  と同じ粒度であり、ここだけ粒度を変える理由が無い。
