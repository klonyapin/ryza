---
reviewed_sha: 109a03d550868057c88c73f45c1cf58c12f2f481
review_date: 2026-08-04
reviewer: 独立役員審査エージェント(プロンプト分離)
verdict: approve
---

# A-12 是正(F-2 / F-3 / F-9)独立役員審査意見書

## 総評

保護領域「監査コード」(`src/ryza/audit/a18.py`)への変更を、起草者の見解を知らされない独立の立場でインスペクションした。F-2(PRVerifier の一時障害を永続キャッシュしない)、F-3(A-18-7 が件名の PR 番号を A-18-4 と同じ経路で照合してから分母に加算する)、F-9(dry-run 実行の照合制限をタイトルに明示する)の3是正は、いずれも要求仕様を満たしており、監査が黙る方向の穴・迂回可能な経路・非退行のいずれも検出されなかった。テストは追加7件を含む 192 件が全件パスした(ローカル実行、DB 接続あり)。ruff もクリーン。**承認する**。ただし後述の [中-1] と [軽微-1〜3] は次の是正パスまでに拾うことを推奨する。

## 検査環境

- pytest: `PYTHONPATH=<worktree>/src .venv/bin/python -m pytest tests/audit/test_a18.py -q` → **192 passed in 288.87s**
  - 補足: venv のエディタブルインストールが main の src を指しているため、単純に `.venv/bin/python -m pytest` を叩くと worktree の変更ではなく main の a18.py が読まれる。この点は本監査の再現手順として意見書に残す(次回審査者が「変更が入っていないコードでテストが失敗した」を誤って所見にしないため)
- ruff: `.venv/bin/python -m ruff check src/ryza/audit/a18.py tests/audit/test_a18.py` → **All checks passed!**

## 所見(検査観点ごと)

### 検査観点1: 各是正が仕様を満たすか — 迂回可能な穴の有無

#### F-2(一時障害の永続キャッシュ回避): 検査したが所見なし

`_unreachable_reason`(a18.py:444-474)は次の3値遷移を実装している:

- `ok` → `_reachable = True` を永続キャッシュ
- `not_found` → `_reachable = False` を永続キャッシュ
- `error` → キャッシュせず、今回の呼び出しに対してだけ理由を返す

3値の分岐は網羅的で、`status` が上記いずれでもない未定義文字列で来た場合も else 節に落ちて error 扱いのフォールバックとなる(fail-open 縮退)。retry を毎回 `repos/<slug>` に投げるコストはある(下記 [軽微-2])が、仕様の要求「プロセス生存中の全 PR 照合が縮退する経路を塞ぐ」は満たしている。

**迂回経路の検討**: 一時障害の直後に `repos/<slug>` が `not_found` を返す状況(認証情報の期限切れなど)では、次回呼び出しで `_reachable = False` がキャッシュされる。これは仕様どおりで問題ない。逆向き(`not_found` キャッシュ後に回復)は起きないため考慮不要。

#### F-3(A-18-7 の PR 番号照合を A-18-4 と統一): 検査したが所見なし(2点の傍証確認込み)

`check_unrecorded_protected_prs`(a18.py:1898-2032)は `pr_verifier` が渡された場合に `pr_verifier.check(pr_number, sha)` の3値判定に基づいて次のように分岐する:

- `ok` → 分母(`checked`)に加算 → 承認記録帰属の従来検査へ
- `bad` → 分母から除外(`continue`)
- `unverifiable` → 分母から除外・`unverified` に計上

**「監査が黙る経路が新設されていないか」の重点検証**: F-3 の `bad → continue` は「借用 PR 番号偽装(実在する別 PR の番号を件名に付けた自作マージ)」を A-18-7 の所見リストから消す方向の変更である。この変更で監査が全体として黙るかを確認した:

1. **A-18-4 側での検出継続**: `check_direct_pushes`(a18.py:1631-1686)は同じ `pr_verifier` を共有し、`verified_pr_merge(subject, pr_verifier, sha)` を叩いて `sha` を `expected_merge_sha` として渡す(a18.py:1659)。借用 PR ケースでは `PRVerifier.check` が `bad` を返し、`verified_pr_merge` が `(False, "PR #N のマージコミットは ... 実在 PR 番号の流用")` を返す。A-18-4 は "PR マージ件名だが GitHub と一致しない" として violations に追加する(a18.py:1668-1669)。**検出は失われず、担当が A-18-7 から A-18-4 に移るだけ**であることを確認した
2. **走査範囲の重なり**: A-18-4 の基準コミット `PR_RULE_BASELINE_COMMIT = 4c7f6e9`(a18.py:210)は A-18-7 の基準コミット `DEEMED_RECORD_BASELINE_COMMIT = 649c4e2`(a18.py:289)より**前**である(`git log 4c7f6e9..649c4e2 --first-parent` が 0 コミット、逆向きが複数コミット)。よって A-18-7 の走査対象は A-18-4 の走査対象の部分集合であり、**A-18-7 で drop したコミットは必ず A-18-4 の走査窓内**にある。カバレッジの穴なし
3. **`pr_verifier=None` の素通し経路**: 呼び出し側が `pr_verifier` を渡さない場合、F-3 の照合はスキップされる(a18.py:1977 `if pr_verifier is not None:`)。これは後方互換で、既存の A-18-7 テスト(`_scan_a187` ヘルパー — tests/audit/test_a18.py:2104-2112)が破綻しないための配慮である。実運用の唯一の入口 `run_a18`(a18.py:2394-2398)は必ず `pr_verifier` を渡すため、素通しは**呼び出し側の明示的な選択**でしか起きない

**別の疑わしい経路**: `pr_verifier` の `slug` が A-18-7 の `repo_slug` と異なる場合の挙動を検討した。`pr_verifier` は自身の slug で API を叩くため、A-18-7 に渡した `repo_slug` は分母カウントの表示にしか使われない。ゆえに、slug 不一致で照合が「意図せず甘くなる」経路はない。

#### F-9(dry-run タイトル明示): 検査したが所見なし

`build_alert_embed`(a18.py:2960)の `dry_run = not result.get("decision_refs_verified")` は、`decision_refs_verified` が `conn is not None`(a18.py:2418)で立つため「DB 接続の有無」と一致する。`.get()` が None を返した場合(古い result dict)も `not None == True` で「dry-run 扱い」= fail-safe 側に倒れる。誤判定の余地は次の1点のみ:

- `run_a18(conn=<some conn>, verify_prs=False)`: DB 接続はあるが PR 実在照合を無効化した実行。この場合は `decision_refs_verified = True` で dry-run 判定にならず、⚠️ フィールド "GitHub PR 実在照合が成立していない"(a18.py:2924)で開示される。妥当

`dry_run` の識別を `decision_refs_verified` に一本化した判定は、旧実装で「connection の有無」「conn がクローズドかどうか」など複数の識別子が混在するリスクを潰しており、単一の起点を持つ設計として支持できる。

### 検査観点2: 「監査が黙る」経路の新設 — 詳細検査

F-3 の `bad → continue` について、以下のケースを列挙して確認した:

| ケース | A-18-4 での検出 | A-18-7 の所見 | 監査全体で黙るか |
|---|---|---|---|
| 実在しない PR 番号を件名にした自作マージ | ✅ "PR マージ件名だが GitHub と一致しない: PR #N が GitHub に存在しない"(bad 経由) | 分母から除外(所見なし) | **鳴る** |
| 実在するが merge_commit_sha が別の自作マージ(番号流用) | ✅ "PR マージ件名だが GitHub と一致しない: PR #N のマージコミットは ... 実在 PR 番号の流用"(bad 経由) | 分母から除外(所見なし) | **鳴る** |
| API 不達(縮退) | 縮退 fail-open(件名を信用)→ 所見にはならないが `pr_verification.failed_open` に計上・⚠️ フィールドで開示 | 分母から除外 → `unverified` に計上・embed で開示 | **開示される** |
| 正常な PR マージで承認記録漏れ | 該当なし(A-18-4 は保護領域無関係) | ✅ 従来どおり所見 | **鳴る** |

**唯一の懸念**: 実運用でユーザーが `run_a18(..., verify_prs=False, ...)` を叩いた場合、F-3 の統一照合が動かず A-18-4 側も verified_pr_merge が `bad` を返せない(pr_verifier=None のため常に True)。この場合、`GitHub PR 実在照合は無効化されている(verify_prs=False)— 件名は自己申告のまま` という notes が出る(a18.py:2465)ため、読み手が識別できないわけではない。ただし tests の `_run_a18_deemed` ヘルパー(test_a18.py:2119-2124)は既定で `verify_prs=False` を使う設計で、開発運用でうっかり本番も同じ設定にする事故は想定しづらいが**なし** とは言えない — この点は後述 [中-1] で言及する。

### 検査観点3: 縮退開示が embed で本当に読み手に届くか

- **所見あり(unrecorded > 0)**: フィールド名 `⚠️ A-18-7 保護領域 PR の承認記録漏れ N/M 件(照合縮退 K 件を分母から除外 — 緑の範囲外)(--deemed-for-pr の実行忘れ)` に含まれる(a18.py:2828-2829)。届く
- **所見なし・分母 > 0(checked_prs > 0)**: フィールド値 `✅ 記録漏れなし(検査対象 M 件)(照合縮退 K 件を分母から除外 — 緑の範囲外)`(a18.py:2843)。届く
- **所見なし・分母ゼロ(checked_prs == 0)**: フィールド値 `対象 PR なし ...` の末尾に条件式で suffix を連結(a18.py:2845-2847)。**Python の演算子優先順位**で `"文字列A" + (X if Y else "")` は正しく評価される。縮退件数ゼロなら空文字列連結、非ゼロなら " (照合縮退 K 件 ...)" が末尾に追加される。届く

`unverified_prs = result.get("unverified_protected_prs") or 0`(a18.py:2811)は False/None/0 のいずれでも 0 に落ちる。悪い値で表示が壊れる経路なし。

### 検査観点4: F-9 の dry_run 判定の妥当性

前述 F-9 の項に記載のとおり、判定に穴なし。ただし `decision_refs_verified` は将来的に「トレーラ参照の突合が全件通ったか」など別の意味を持たされる可能性があり、そうなると dry-run 識別が崩れる。**現状は問題なし**だが、専用のフラグを分離するか、意味の固定をコード側に明記する余地はある([軽微-3])。

### 検査観点5: テストが実装の契約を固定しているか — vacuous test / 偽実装が通る余地

追加テスト7件を1件ずつ検証した:

1. `test_repo_reachability_temporary_error_is_not_permanently_cached`: 3応答(error → ok → ok)で回復を確認。calls の順序を `[repos, repos, pulls/9]` まで固定 → **偽実装で「error を返しても実は cache する」実装は 2回目 repos を叩かないので落ちる**。契約固定 ok
2. `test_repo_reachability_not_found_is_cached`: 404 が1回のみ叩かれることを assertion。偽実装で「404 もキャッシュしない」に戻すと 2回叩くので落ちる。契約固定 ok
3. `test_a18_7_nonexistent_pr_number_is_not_counted_as_checked`: `checked == 0` と `findings == []` を固定。偽実装で「pr_verifier を無視して checked に加算」すると `checked >= 1` で落ちる。ok
4. `test_a18_7_borrowed_pr_number_is_not_counted_as_checked`: 借用ケース。`checked == 0` と `findings == []`。偽実装で「SHA 帰属を確認しない」実装は `check == 1` で落ちる。ok
5. `test_a18_7_verification_degraded_is_excluded_and_disclosed`: `checked == 0`、`unverified == 1`、`findings == []`。偽実装で「縮退でも checked に入れる」は落ちる。ok。ただし**docstring は「報告 embed のタイトルに『照合縮退』を明示する」と主張しているが、assertion にタイトル(または field 名)は含まれていない**。これは [軽微-1] で言及する
6. `test_a18_7_ok_pr_verification_matches_checked`: 非退行テスト。ok の PR は従来どおり分母に加算。ok
7. `test_dry_run_title_marks_limitation`: `embed["title"]` に `DRY-RUN(照合制限あり)` を含むことを固定。偽実装で「タイトルではなく description に入れる」は落ちる。ok
8. `test_normal_run_title_has_no_dry_run_marker`: 通常実行時に "DRY-RUN" 文字列が含まれない negative test。通常実行の title が誤って DRY-RUN 化する偽実装を検出する。ok

**契約固定の総合評価**: 追加テストは実装契約をよく固定している。 [軽微-1] を除いて vacuous test なし。

### 検査観点6: 後方互換(`pr_verifier=None` 素通し経路)

`check_unrecorded_protected_prs(..., pr_verifier=None)` は F-3 照合をスキップし、従来どおり件名から取れる PR 番号を分母に加算する。これは:

- 既存の A-18-7 テストが `_scan_a187` ヘルパーで pr_verifier を渡していない挙動と整合
- `verify_prs=False` の実行(ネットワーク隔絶テスト等)を破壊しない

**問題点**: docstring(a18.py:1975-1976)は「呼び出し側が明示的に照合を無効化した場合」と表現しており、意図的な素通しである旨は明記されている。本番の唯一のエントリ `run_a18` は `verify_prs=True` を既定に持ち、必ず pr_verifier を作って渡すため、意図しない素通しは起きにくい。この設計は**支持できる**。

### 検査観点7: テスト実行(全件パス確認)

`PYTHONPATH=<worktree>/src .venv/bin/python -m pytest tests/audit/test_a18.py -q` → **192 passed in 288.87s**(DB 接続あり)。ruff もクリーン。

## 個別所見

### [中-1] `verify_prs=False` 実行時の F-3 バイパスの明示性が弱い

- **根拠**: `run_a18(verify_prs=False)` の場合、`pr_verifier is None` となり(a18.py:2365-2366 の分岐が pr_verifier を生成しない)、`check_unrecorded_protected_prs` は F-3 の統一照合をスキップする。この状態でも A-18-7 の embed 表示は「✅ 記録漏れなし(検査対象 N 件)」のように緑となり、緑の意味が「SHA 帰属を含む照合が通った」ではなく「件名の PR 番号を素通しで受理した」ことが**表向きの表示から区別できない**。開示は notes 側の "GitHub PR 実在照合は無効化されている(verify_prs=False)— 件名は自己申告のまま"(a18.py:2465)にのみ載る
- **A-12 是正の趣旨との整合**: F-9 で dry-run(DB 未接続)は**タイトルに**明示する方針を採ったが、`verify_prs=False` は notes だけで disclosure を済ませており、方針が非対称になっている
- **推奨是正**: 次パスで、`prs_verified is False` の場合も embed タイトル(または A-18-7 field 名)に「PR 照合無効」プレフィックスを付ける。あるいは A-18-7 の緑 field 名を "(PR 照合無効)" で修飾する。今回の PR ですぐ入れる必要はなく、独立の是正として起票してよい

### [軽微-1] `test_a18_7_verification_degraded_is_excluded_and_disclosed` の docstring と assertion の乖離

- **根拠**(test_a18.py:2398-2416): docstring は「報告 embed のタイトルに『照合縮退』を明示する」と主張しているが、テストは `scan.checked == 0` / `scan.unverified == 1` / `scan.findings == []` のみを assertion しており、embed の内容を検査していない。実際には embed のタイトルではなく A-18-7 の **field 名** に "(照合縮退 K 件を分母から除外 — 緑の範囲外)" が入る(a18.py:2828-2829)。docstring の「タイトル」は不正確
- **推奨是正**: (a) docstring の「タイトル」を「A-18-7 の field 名」に修正、かつ (b) `build_alert_embed(result)` を呼んで対応 field の name に上記文字列が含まれることを assertion に追加する。**embed の disclosure が消える経路のリグレッションを固定できる**

### [軽微-2] 一時障害持続時に `repos/<slug>` を PR 数だけ叩く

- **根拠**(a18.py:454-474): `_reachable = None` のまま error が続くと、各 `check()` 呼び出しごとに `_unreachable_reason` が `api_get("repos/<slug>")` を叩く。1回のレート制限で全 PR が縮退する経路は塞げたが、逆に**縮退中の API コール数が(PR 数 × 2)へ倍増する**。GitHub API のレート制限が既に枯渇している状況ではさほど痛くないが、5xx 系の一時障害では復旧までの間 API を叩き続けることになる
- **推奨是正**: `_reach_reason` に加えて「最後に error を返した時刻」を持ち、短時間(例: 60秒)は同じ error 理由を返すようにする(小さいバックオフ)。実装コストは低い。今回の PR での対応は不要 — 独立の是正として次パスで検討

### [軽微-3] `decision_refs_verified` に dry-run 判定の意味を兼務させている

- **根拠**(a18.py:2418, 2960): F-9 の dry-run 判定は `not result.get("decision_refs_verified")` を使う。現状は `decision_refs_verified = conn is not None` なので「DB 未接続 ⇔ dry-run」で正しいが、将来的にこの key が「トレーラ突合の実行有無」など別の意味を持たされたときに dry-run 判定が壊れる
- **推奨是正**: `result["dry_run"] = conn is None` を明示的に追加する(現在の判定と数値は同じ)。あるいは `decision_refs_verified` の意味を「conn の有無と一致する」ことを docstring/コメントに明記する

## 検査したが所見なし(明示)

- **F-2 の3値遷移の網羅性**: ok/not_found/error の3ケースで caching の有無が正しく分岐している。retry 経路も適切
- **F-3 の bad → continue が監査を黙らせる方向にならないか**: 走査範囲重なりと A-18-4 側の bad ハンドリングを確認。カバレッジの穴なし
- **F-9 のタイトル判定の誤検出**: `.get()` の None fallback は fail-safe 側に倒れる
- **後方互換**: `pr_verifier=None` の素通しは意図的で、既存テストとの整合を保つ
- **embed の縮退 disclosure**: 3ケース(所見あり / 所見なし & checked > 0 / 所見なし & checked == 0)すべてで届く。演算子優先順位のミスもなし
- **ruff / pytest**: いずれもクリーン(192 passed)
- **ドキュメンテーション**: 変更部分の docstring は変更理由(F-2/F-3/F-9)と後方互換の設計意図を明記しており、後続の読み手が変更の意図を追える

## 承認条件

なし(approve)。個別所見はいずれも「今回の PR とは独立に是正できる」性質のものであり、[中-1] は次の是正パス(F-2/F-3/F-9 に類する A-12 派生)で拾うことを推奨する。[軽微-1] のテスト強化は本 PR に追加してもよいが、必須ではない。

## 反対意見書(追従禁止規範に基づく)

本 PR に「approve」を出したので、反対すべき点を探した。以下は反対意見の候補として検討した3点だが、いずれも承認を覆すには至らない:

1. **反対候補1**: 「F-3 の bad → continue は A-18-7 の可視性を下げる。借用 PR 偽装は A-18-7 の所見として明示すべきだ」 → 反論: A-18-4 が同じ偽装を "PR マージ件名だが GitHub と一致しない" として鳴らす。二重計上を避ける設計は妥当。もし「二重計上を避ける」ことに反対するなら、A-18 全体の設計(検査ごとに担当を分離する)を覆す必要がある。ここでの是正には含めるべきでない
2. **反対候補2**: 「一時障害を毎回問い合わせる設計は API 消費が多すぎる」 → 反論: レート制限で API が既に枯渇している状況では追加コストは実質ゼロ。5xx 障害の場合はコストがあるが、これは [軽微-2] として独立に是正すればよい。F-2 の趣旨(fail-open 縮退の永続化を防ぐ)は満たしている
3. **反対候補3**: 「dry-run の識別を `decision_refs_verified` に相乗りさせるのは設計として不明瞭」 → 反論: 現時点で数値は正しく、コメントで意図が明記されている。将来の変更に対する脆さは [軽微-3] で言及した。是正パスで拾える

以上、反対の余地はあるが、いずれも本 PR の承認を覆さない。**approve** で確定する。
