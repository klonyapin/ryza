---
reviewed_sha: 81e236dc66cd8d18e2d079a996ccda48b500a8ad
review_date: 2026-08-04
verdict: conditional_approve
---

# 独立役員意見書 — reviewed_sha の審査主体化(意見書 front matter を正とする)

- 日付: 2026-08-04 / 対象: ブランチ最終コミット 81e236d(1 commit, 8 files, +951/-25)
- 審査者: 独立役員(非執行・批判専任。起草者の選好は不知)
- 根拠: CLAUDE.md 不変原則6、docs/design/06-constitution.md 第3条・第5条、docs/design/07-development.md §3-1、ops/reminders.yaml `reviewed-sha-from-review-agent`、docs/reviews/g-a18-protect-independent-review.md 重要-3
- 検証(審査者が実行): `pytest tests/test_reviews.py tests/governance/test_decisions.py` 115 passed / `pytest tests/audit/test_a18.py` 180 passed / 敵対的プローブ 20 件 / ミューテーション 2 件 / 既存意見書 22 本の後方互換走査
- **本意見書は新様式(front matter)の最初の実例である**。上の `reviewed_sha` は本審査が実際に読んだツリーの SHA であり、起票者の申告ではない

## 判定: 条件付き承認 — マージ前必須 3 件(C-1・C-2・C-3)

本 PR は統制を net で強くしている。従来ゼロだった「起票者の申告を外から検証する地点」を初めて作り、
様式不備を「旧様式」に読み替えない設計は正しく、後方互換は実測で無傷である。しかし**「審査側の記録が正」
という本 PR の主張は、実装のままでは成立しない**。審査記録は 1 文字で無音に迂回でき(C-2)、
その迂回を見える化するはずの由来指標は事後に製造でき(C-3)、審査記録が `reject` と書いていても
発効が進み通知は当該意見書を審査の裏付けとして掲示する(C-1)。3 件はいずれも小さな是正で閉じる。

## マージ前必須(3 件)

### C-1(重大)`verdict: reject` の意見書で発効し、通知がそれを「独立役員審査」として掲示する

実走(`--deemed --review <reject を宣言した意見書> --dry-run`)で確認した。CLI は stderr に
「審査記録の判定: reject(発効の可否判断には使っていない)」と出したうえで exit 0 し、生成された
通知 embed は本文に `審査対象コミット: aaaa…` と `独立役員審査: docs/reviews/zz-probe-reject.md`
を並べて掲示した。判定 `reject` は embed にも `governance.decisions` にも `meta.runs` にも残らない。

これは本 PR が**新たに作った偽の保証**である。変更前は `reviewed_sha` も `review_ref` も起票者の申告に
すぎず、システムは意見書の中身を読んでいなかった。変更後はシステムが意見書を読み、否認を見て、
それを捨てたうえで「独立役員審査: <その意見書>」と掲示する。48h 異議期間に代表が見る唯一の成果物が
通知である以上、これは手続の逆転にあたる(定款第3条・第5条は独立役員審査を**前置**する手続を定める)。

起草者の「判定名の揺れで発効が止まると様式そのものが忌避される」という理由(reviews.py `VERDICTS`
付近のコメント)は**語彙外の値**については妥当であり、私はそこに反対しない。しかし `reject` は
語彙内の一意な値であり、揺れの問題ではない。この論拠は `reject` まで届かない。

是正(いずれか): ①`verdict: reject` は `ReviewedShaConflictError` と同格で発効を中止する(fail-safe
一貫)、または最低でも ②判定を embed 本文と run params に載せ、`reject` のまま発効した事実を DB に残す。
①を推す。

### C-2(重大)fail-safe は参照の書式を変えるだけで無音に迂回できる

同一の正当な新様式意見書を指す 5 つの参照表記を `resolve_reviewed_sha(ref, <嘘の SHA>)` に与えた結果:

| 参照表記 | 結果 |
|---|---|
| `docs/reviews/x.md` | **中止**(ReviewedShaConflictError) |
| `docs/reviews//x.md` | **中止** |
| `docs/reviews/../reviews/x.md` | 通過(sha=嘘, source=`argument`) |
| `./docs/reviews/x.md` | 中止 |
| `https://github.com/o/r/blob/main/docs/reviews/x.md` | 通過(sha=嘘, source=`argument`) |

`is_repo_path_ref` が `..` とスキーム付きを読まないため、意見書は存在し新様式で正しい SHA を宣言して
いるのに「審査記録なし」として扱われ、起票者の申告がそのまま採用される。**迂回はどの層からも見えない**:
`missing_review_ref_warning` は `(root / ref).exists()` で判定するため `..` 形式を実在と見なし無警告
(実測: `None` を返す)、A-18-8 は compared=1 / from_review_artifact=0 / violations=0 で、正直な旧様式
意見書と区別がつかない(実測)。

先頭に空行を 1 行入れるだけでも同じ結果になる。`split_front_matter` は 0 行目だけを開始フェンスと
見なすため、`\n---\nreviewed_sha: …\n---\n` は front matter 無しとして通り(実測 `None`)、生ファイルを
読む人間には front matter が見えたままである。開始フェンスの前後の空白は許容される(` ---` は通る)一方、
空行 1 行は無効化するという非対称も、様式として説明しにくい。

07-development §3-1 の「残る限界」は「書き換える・消す・front matter の無いファイルを指す」の 3 経路を
挙げるが、**上記はいずれにも該当しない**。意見書は無改変で、front matter を持ち、指し先も正しい。
既知の限界としての文書化は不十分である(審査観点2 への回答)。

是正: ①`..` は拒否ではなく `repo_root` 基準で正規化し、解決後にルート外へ出る場合のみ拒否する、
②開始フェンスは先頭の空行を読み飛ばして判定する、③参照が「読めない形式」なのに同じ位置に実在ファイルが
ある場合は警告を出す(迂回の痕跡を残す)。

### C-3(重大)由来 `from_review_artifact` は事後に製造できる

A-18 の `_review_artifact_sha` は**監査時点の作業ツリー**を読むだけで、意見書が決定より前に存在したかを
検査しない。決定と `Approved:` トレーラを先に作り、その後で同じ SHA を宣言する意見書を commit する
プローブで、`from_review_artifact` は 0 → 1 に上がった(実測)。独立審査は一度も走っていない。

起草者は docstring で「意見書を後から削除・改名すると由来が消えるが、逆(消して増える)にはならない」と
書いているが、**製造の方向は削除ではなく追加**であり、そこは論じられていない。起票者が既に申告済みの
SHA を書き写した 4 行のファイルを docs/reviews(保護領域外)に足すだけで、由来率は 100% にできる。

これは単独の瑕疵にとどまらない。判断3(欠落は警告のみ)と判断4(監査は楽観に倒さない)は、いずれも
「A-18-8 の由来開示が翌週に可視化する」ことを安全網として成立している。その安全網の値が起票者の意思で
上げられるなら、**緑の意味を割合で限定するという ③ の設計目的は達成されていない**。

是正: 照合先を作業ツリーではなく**トレーラコミット時点の内容**にする
(`git show <trailer_sha>:<review_ref>`、または `git log --diff-filter=A --format=%H -- <path>` で追加
コミットが決定時刻より前かを検査する)。実装は数行で、`_log_messages` が既に同じ repo を扱っている。

## リマインダー登録を条件に許容(2 件)

### C-4(重要)由来指標は 4 つの原因を 1 つの数に潰しており、判断3 の逆インセンティブを検出できない

審査観点5(「新様式なのに reviewed_sha を書かない」意見書の定常化を A-18-8 が検出するか)を実走で
確かめた結果は **検出できない** である。`compared - from_review_artifact` は次を区別しない:
(a) 正当な旧様式(後方互換で意図された多数派)、(b) 新様式だが `reviewed_sha` を書かない(判断3 が
警戒する当のもの)、(c) C-2 の参照迂回、(d) 意見書の削除・改名。docs/reviews の現存 22 本はすべて
旧様式であり(実測)、当面 (a) が支配するため、由来率は良性の理由で低く張り付き、(b)(c) はその陰に隠れる。

是正: 由来なしの内訳を `old_style` / `missing_key` / `unreadable_ref` / `not_found` に分けて数える。
最低でも「旧様式」と「それ以外」の 2 分割で、判断3 が想定した検出は成立する。

### C-5(重要)`reviewed_notes` が DB に残らない — SHA-6 原則の適用漏れ

`main()` は `review_ref_warning` を run params に入れる際、コメントで「stderr は消える — SHA-6」と
明記している(decisions.py:1161-1162)。ところが同じ関数が直前で出力する `target.reviewed_notes`
(front matter の様式警告・head SHA との相違・**C-1 の `reject` 判定**)は stderr と log にしか出ず、
params にも decision の note にも入らない。既に受け入れた原則を、同じ関数の 30 行下で適用し忘れている。

最も影響が大きいのは reject 判定の消失(C-1)であり、C-1 を ② で処理する場合はここが唯一の証跡になる。

是正: `reviewed_notes` を run params に追加する(1 行)。

## 軽微(登録不要・是正推奨)

- **C-6 YAML 重複キーは後勝ちで無警告**。`reviewed_sha` を 2 回書くと後者が採用される(実測: 正直な
  SHA を先に、偽の SHA を後に置ける)。統制の入力なので重複キーは拒否が筋。front matter が短く diff で
  気づきやすいため軽微とする。
- **C-7 「リポジトリ外を読みに行かない」という docstring の保証が symlink で成立しない**。
  `docs/reviews/link.md` → リポジトリ外のファイルという symlink を読めた(実測)。実害は
  `reviewed_sha` の採用元が外部ファイルになる点に限られるが、docstring は持っていない保証を主張している。
- **C-8 保護登録先送り(`reviews-parser-protect`)は結論として許容するが、先送りの理由が事実として誤り**
  (審査観点6 への回答)。reminder body は「本モジュールは判定を持たない読み取り専用のパーサ」と書くが、
  `is_repo_path_ref` の戻り値が fail-safe の発火可否そのものである(C-2 が実証)。加えて本 PR は判定
  コードを保護済み `src/ryza/governance/**` から未保護 `src/ryza/reviews.py` へ外出しした形になっている。
  許容する根拠は**ミューテーションが捕まること**である: `is_repo_path_ref` を常に False にする改変で
  9 テストが失敗し(M1)、`load_review_artifact` の例外を握り潰す改変で 1 テストが失敗した(M2)。
  したがって無承認の無音化にはテストの同時改変を要する。ただし `tests/test_reviews.py` は保護領域外
  (`invariant_tests` は `tests/test_ips.py` のみ)なので、期限 2026-08-18 を待たず本テストを
  `invariant_tests` に登録するほうが安い。

## 反対を探して見つからなかった箇所

- **判断1(head SHA との相違で止めない)は妥当**。意見書のコミット自身が head を進める以上、審査対象
  SHA と head は構造的に一致しない。ここを致命にすれば新様式は採用されずに終わる。注記に落とす扱いは、
  A-18-1 の承継範囲が既に記録側を採用する挙動(`resolve_reviewed_scope`)とも整合する。反対材料なし。
- **判断2(検査を writer でなく CLI に置く)は妥当**。`notices.announce_deemed_approval` は接続と値だけを
  受ける設計で、Bot・ジョブ経路にワークツリーは無い。writer に置けばリポジトリを持たない正当な経路が
  一律に落ちる。裏返しの「CLI を通さない発効は無検査」は既存の `deemed-auto-announce` 未実装と同じ穴で、
  本 PR が新たに開けたものではない。
- **様式不備を「旧様式」に読み替えない設計は正しい**。閉じフェンス欠落・YAML 破損・非マッピング・空
  front matter・短縮 SHA はすべて中止になり(実測)、M2 の握り潰し改変はテストで捕まる。
- **後方互換は無傷**。既存 22 本すべてが `None`(旧様式)を返し、旧様式 + head SHA / 旧様式 + 明示指定の
  経路は従来値を保つ。295 テスト全通過。
- **パーサの既知の落とし穴は塞がれている**(審査観点3): `yaml.safe_load` により `!!python/object/apply`
  はタグ未定義で拒否、40 桁 hex 検査により数値化 SHA(先頭 0 落ち・`123e45…` の float 表記)・
  bool・list は拒否、BOM・CRLF・`...` 終端・フェンス行末空白はいずれも正常処理。例外型は
  `ReviewArtifactError`(= `ValueError`)に統一され、CLI の `except (…, ValueError)` が確実に捕捉して
  exit 1 になる(実書込経路で DB 接続前に停止することを、到達不能な DB URL で確認済み)。

## この意見書が間違っている場合の理由トップ3

1. **C-2 の迂回は「敵対的起票者」を前提するが、統制の設計目標は誤り検出である**。もしそうなら
   C-2 は軽微に落ちる。ただし本 PR 自身が「起票者が両側に同じ嘘を書けば通る」ことを解決対象として
   掲げている(reviews.py 冒頭)以上、脅威モデルは敵対的起票者である、と読んだ。
2. **C-3 の事後製造は docs/reviews への commit を要し、それ自体が PR 経路を通る**。レビュアが
   「独立審査を経ていない意見書の追加」を目視で弾くなら残存リスクは下がる。しかし docs/reviews は
   保護領域外であり、A-18-1 の突合対象でもないため、自動の歯止めはゼロである。
3. **C-1 の `reject` 発効は運用上そもそも起きない**(否認された PR は起票されない)。とはいえ統制は
   「起きないはず」ではなく「起きたときどうなるか」で評価すべきで、現状の答えが「通知が審査の裏付けと
   して掲示する」である点は、頻度と独立に問題である。

## 是正確認の観点(再審査時)

1. C-1: `verdict: reject` で exit≠0、または embed 本文と run params に判定が載ること
2. C-2: `docs/reviews/../reviews/x.md` と GitHub blob URL の双方で発効が中止されるか、少なくとも警告が
   出ること。先頭空行付き front matter が読まれること
3. C-3: 決定・トレーラの後に意見書を足したケースで `from_review_artifact` が増えないこと

---

## 設計リード裁定(2026-08-04)

**総合裁定: 全指摘(C-1〜C-8)を採用**。マージ前必須 3 件だけでなく、リマインダー登録許容の 2 件・
軽微 3 件のすべてに対応する。理由は 3 つ:

1. C-1 は「新設した偽の保証」であり、新様式が既存統制を上回るという本 PR の主張が成立するためには
   fail-safe の一貫が不可欠。`request_changes` も同格で扱う(語彙内の否定判定)
2. C-2・C-3 は「起票者の申告を外から検証する地点を初めて作る」という本 PR の設計目的の直接の穴で
   あり、C-4 の内訳開示が入っても本体が漏れていれば由来指標は依然として起票者の意思で上げられる
3. C-5・C-8 は既に本 PR 内で述べた原則(SHA-6・reviews.py の統制上の位置づけ)を、同じ関数・
   同じ判断の隣接箇所で適用し忘れているだけであり、対処のコストが極めて低い

### 各項目の対応(実装 SHA は本コミット追加分)

- **C-1(重大)裁定=採用**: `verdict: reject` / `request_changes` を `ReviewedShaConflictError` と同格で
  発効中止(fail-safe)。CLI は exit 1 で DB 書込前に停止する。`BLOCKING_VERDICTS` として語彙を切り出し、
  意味の起点を 1 箇所にした(`src/ryza/reviews.py`)。①案(是正候補)を採用
- **C-2(重大)裁定=採用**: (a) `..` を含む参照は `resolve_review_path` で正規化し、リポジトリ外へ出る
  ものはエラー、(b) 自リポジトリの GitHub blob URL は相対パスに変換、(c) `--kind pr` は `--review` の
  実在を検査(`--review-missing-ok` で救済)、(d) 先頭空行を許容、(e) `..` を含んでも `is_repo_path_ref`
  は「リポジトリ内パス」と判定し正規化は `resolve_review_path` の責務に集約
- **C-3(重大)裁定=採用**: A-18 の `_classify_review_provenance` を新設し、`git log --follow --format=%cI
  --reverse` で意見書の**初出コミット時刻**を取り、`decided_at` と datetime 比較する。決定より後に
  現れた意見書は `post_hoc` として由来から外し、内訳注記で別枠開示する。旧実装が読んでいた作業ツリー
  ベースの判定は捨てた
- **C-4(重要)裁定=採用**: `ReviewedShaScan` を 6 カテゴリ(`from_review_artifact` / `post_hoc` /
  `old_style` / `missing_sha` / `sha_conflict` / `unreadable`)に分割。由来なしの内訳は
  「旧様式 → 新様式で SHA 欠落 → SHA 食い違い → 参照不読 → 事後製造」の順で毎週 embed の A-18-8 注記に
  カテゴリ別件数を出す。リマインダー登録許容の下限に留めず本 PR で解決
- **C-5(重要)裁定=採用**: `target.reviewed_notes` を `governance.decisions --deemed(-for-pr)` の
  run params に格納。SHA-6 原則の隣接箇所適用漏れを閉じる
- **C-6(軽微)裁定=採用**: front matter のトップレベルキー重複は `_raise_on_duplicate_keys` で拒否
  (`yaml.safe_load` の後勝ちで無警告に採用値が入れ替わる経路)
- **C-7(軽微)裁定=採用**: `resolve_review_path` で symlink 経由のリポジトリ外参照を拒否
  (`resolve()` 後にルート判定)。C-2(c) の中で解決した
- **C-8(軽微・保護登録)裁定=採用**: `src/ryza/reviews.py` を `config/governance.yaml` の
  `protected_areas` に(`area: governance_engine`)、`tests/test_reviews.py` を `invariant_tests` に
  登録した。`ops/reminders.yaml` の `reviews-parser-protect` は body の誤った理由(「判定を持たない
  読み取り専用のパーサ」)を訂正して `status: done` に落とした

### 反対すべき点を探して見つからなかった箇所

独立役員の C-1 に対する反論「起草者の VERDICTS 揺れ許容は語彙外にとどまり `reject` には届かない」は
論理として完結しており、書き換えるべき箇所が無い。C-4 のカテゴリ分割案(4 分割)を本 PR では 6 分割
(post_hoc・sha_conflict を独立させた)で採用したが、これは C-3 の反映と C-1 の判定の派生であり、
独立役員の分割案に**加える**方向の変更なので反対にあたらない。「反対を探して見つからなかった箇所」の
判断1・判断2・様式不備の設計・後方互換・パーサの既知の落とし穴の 5 項目は、いずれも設計リード側の
判断と一致するため書き換えなし
