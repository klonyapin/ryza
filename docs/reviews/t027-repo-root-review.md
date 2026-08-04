---
review: t027-repo-root
reviewed_sha: f162eb93de13357f4083037cf4b336d11bff8f3b
reviewer: independent-reviewer (opus)
review_date: 2026-08-04
verdict: approve
---

# 要旨(一文アーギュメント)

本 PR は Issue #132 の主旨(worktree 内の意見書を CLI に明示させる案1)を保護領域の既存制約を保ったまま最小差分で実装しており、指示書の受け入れ基準を過不足なく満たすため承認する。

# 観点別の合否

## 1. 仕様適合(合格)

指示書 §1〜3 の要求はすべて実装されている。argparse への `--repo-root` 追加(`src/ryza/governance/decisions.py:1030-1039`)、fail-closed 検証(同 `:1117-1141` の `_validated_repo_root`)、既存 `require_existing_review` / `resolve_reviewed_sha` / `missing_review_ref_warning` への `repo_root=` 伝播(同 `:1185-1191`、`:1216-1219`、`:1226-1228`、`:1316`)、note と meta.runs 双方への痕跡(同 `:1272-1281` と `:1363-1365`、`:1367` の呼び出し)、モジュール docstring L46-56 の usage 追記と L444-448 の `require_existing_review` docstring への「worktree があるなら --repo-root が正道」の追記。スコープ逸脱は無い(`git diff main...HEAD --stat` は decisions.py・test_decisions.py・タスク文書の3ファイルのみ)。

## 2. fail-closed 性(合格)

`main()` の実行順(`decisions.py:1299-1308`)は「`_validated_repo_root` → `_resolve_deemed_args` → `missing_review_ref_warning` → `start_run` → `connect()`」であり、`connect()` は `:1368` にある。したがって不正な `--repo-root` は DB 書き込み(governance.decisions への INSERT)に到達する前に `return 1` で止まる。目印は `config/governance.yaml` の実在(`:1136`)で、これは既存 `_repo_root()` フォールバック(`:581`)と一致するため二重定義にはならない。`Path.resolve()` を通しているので相対パス・シンボリック名(macOS の `/var` → `/private/var` など)は絶対化されて監査に残る。テスト `test_cli_repo_root_nonexistent_path_aborts_before_db_write` と `test_cli_repo_root_missing_governance_yaml_aborts` が DB SELECT で「決定行が存在しない」ことを実体で確認しており、fail-closed の主張が実験に裏付けられている。

## 3. 既存挙動の不変性(合格)

`--repo-root` 未指定時は `_validated_repo_root(None) is None`(`:1128-1129`)。その `None` は `_resolve_deemed_args` を素通りして各 helper に渡り、`_review_path` の `repo_root or _repo_root()`(`:421`)と `resolve_reviewed_sha` の `repo_root=repo_root or _repo_root()`(`:521`)で従来の `_repo_root()` に落ちる。helper シグネチャの既定値は `repo_root: Path | None = None` で追加されており、既存呼び出し側(未指定)には差が出ない。note 側も `_note_with_repo_root(note, None)` で `note` を素通しする(`:1278-1279`)。meta.runs には `"repo_root": None` が新規キーとして常時記録されるため、これは params の拡張であり(値は None なので下流の意味論に影響しない)、既存テスト 338 件が green のまま通ったことで担保される。

## 4. 記録の完全性(合格)

`--repo-root` を使った場合の痕跡は二重に残る。note には `REPO_ROOT_NOTE_PREFIX = "[repo_root] "` 付きで絶対パスが行として追記され(`:1280`)、meta.runs.params には `"repo_root": <str>` が入る(`:1365`)。前者は決定を直接読む監査(A-18)から届き、後者は run 側から追える —— 監査面が二重化されているのは既存の `[審査参照の警告]` と対称で、`_note_with_repo_root` の実装(`:1272-1281`)は `_note_with_warning`(`:1258-1269`)の書式に完全に倣っている。`test_cli_repo_root_is_recorded_in_decision_note` が実 DB に対する SELECT で `REPO_ROOT_NOTE_PREFIX` と `tmp_path.resolve()` の在存を確認している。

## 5. 回帰(合格)

`tests/governance/` を 1 回実行して 340 passed / 2 failed / 6 errors。失敗と errors の全件が Issue #142 の既知環境要因リスト内である(`test_boardroom.py::test_record_chat_stances_marks_office_chat_source`、`test_governance_schema.py::test_blind_mode_is_allowlist_not_denylist`、`test_devchat.py` の 6 件 = dashboard ロール権限差)。新規 5 テストと関連する既存 1 テストは全通(`-k repo_root` で 6 passed)。`ruff check src/ryza/governance tests/governance` は `All checks passed!`。

# 所見

## 重大(0 件)

なし。

## 中(0 件)

なし。

## 軽微(2 件)

### M-1. `_validated_repo_root` の例外型が汎用 `ValueError`(`decisions.py:1129, 1135`)

**根拠**: 既存 `_resolve_deemed_args` も `ValueError` を投げるため、`main()` の except 節が両者を同じ扱いにしている(`:1301-1303` と `:1306-1308` が実質同じ本文)。**現状で害はない**が、`RepoRootError(ValueError)` の派生を作っておくと、将来 `--repo-root` 由来の失敗件数だけを A-18 で数えたいときに例外型で分離できる。今回は指示書の範囲外(スコープを絞る指示に従うのが正)ため見送ってよい。

### M-2. `test_cli_repo_root_is_recorded_in_decision_note` の後片付けが緩い(`test_decisions.py:1287-1296`)

**根拠**: `finally` 節で `meta.runs` から `DELETE` して commit しているが、`inner.rollback()` の後で `DELETE` を発行してから `commit()` している。common な流儀としては rollback だけで済ませたかったところ(governance.decisions は rollback で消えるが meta.runs は autocommit で書かれる可能性がある — 実際 `start_run` は自前接続を持つ)。実装上は動くのだが、共有 DB でテスト間の meta.runs の増加を招く可能性は残る。指示書のテスト方針(既存 conftest 慣行に従う)に沿っているためこれ以上は求めない。

# 反対意見書(議論規約2)— この PR が間違っている場合の理由トップ3

## R-1. 「案1(明示オプション)」より「案2(CWD 起点)」を選ぶべきだった

**採否: 不採用**。**根拠**: 指示書 §背景 の理由付け(「CWD 起点は『どこで実行したか』で挙動が変わる暗黙依存を増やす」)は正当である。CI や cron から呼ぶときに CWD が worktree に一致する保証はなく、CWD 依存は fail-open(誤ったルートを黙って採用)へ倒れやすい。明示オプションは指定漏れが目視でわかり、指定した場合は fail-closed で検証されるため、監査可能性(A-18)に優る。

## R-2. `config/governance.yaml` の実在チェックは目印として弱い(誤検出しうる)

**採否: 部分的に妥当だが本 PR で扱わない**。**根拠**: 別リポジトリでも偶然 `config/governance.yaml` を作れば通ってしまう。ただし (a) 攻撃面としては CLI 実行者がすでにローカルファイル書込権限を持つため事実上の脅威ではない、(b) 既存 `_repo_root()` フォールバック(`:581`)と目印を揃えることに主眼があり(二重定義の回避)、より強い目印(例: `.git/config` の origin URL 検査)を入れると `_repo_root()` 側と食い違いが生じる。より頑健な検証は「両側同時に」変えるべきで、本 PR の範囲(Issue #132)を超える。

## R-3. `--repo-root` は `--review-missing-ok` を実質不要にするので後者を deprecate すべきだった

**採否: 不採用**。**根拠**: `require_existing_review` docstring の追記(`:444-448`)が示す通り、`--review-missing-ok` は「worktree すら無い遡及登録」(Discord スレッド・Issue コメントで審査した過去分の後日登録)を塞がないための逃げ道であり、`--repo-root` とは目的が異なる。deprecate は監査ゲート C-2(c) の統制を弱めるため、本 PR で触れないのが正しい。

# 追加所見

「反対すべき点を探して見つからなかった」箇所は指示書 §1〜3 の実装本体である。既存関数のシグネチャに互換保持で `repo_root=` を追加するだけの最小差分で目的を達しており、`_note_with_repo_root` が既存の `_note_with_warning` の書式に忠実に倣うことで監査面(接頭辞での検索性)も既存パターンに合流している。差分の大部分がテスト(171 行)と docstring(コード変更ではない解説文)であり、実質的なコード変更は約 60 行に収まっている。
