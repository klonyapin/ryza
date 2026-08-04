# T-027: governance CLI に --repo-root を追加(Issue #132)

## 背景(自己完結)

`python -m ryza.governance.decisions --deemed-for-pr N --review docs/reviews/X.md` は、
意見書(審査記録)を読んで front matter の `reviewed_sha` を採用する。意見書の探索ルートは
`_repo_root()`(src/ryza/governance/decisions.py 内)が決めるが、これは module `__file__`
起点で `git rev-parse --show-toplevel` するため**常にメイン checkout** を見る。

開発フローでは意見書はマージ前の PR ブランチ(worktree)にしか存在しないため、
CLI からは「参照が見つからない」となり、`reviewed_sha` が PR head SHA へフォールバックする。
これが decision 24 の sha_conflict(A-18-8 で毎週開示される恒久ノイズ)を生んだ。
現行の回避策はメイン checkout の docs/reviews/ に一時コピーして実行後に削除する運用だが、
運用手順であってコードの是正ではない。

Issue #132 の是正案のうち **案1(`--repo-root` オプション追加)** を採用する。
理由: `resolve_review_path` / `load_review_artifact` / `resolve_reviewed_sha` など既存関数は
すべて `repo_root=` 引数を受け取れる設計になっており、CLI が公開していないだけである。
案2(CWD 起点)は「どこで実行したか」で挙動が変わる暗黙依存を増やし、
案3(gh api)はネットワーク依存と認証を CLI に持ち込む。明示オプションが最小・最安全。

## 作業ディレクトリ

git worktree(あなたに割り当てられたもの)。ブランチはそのまま使い、**push はしない**
(検収者が行う)。コミットメッセージは日本語+
`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` トレーラ。

## 変更内容

### 1. argparse に `--repo-root` を追加

- `--deemed-for-pr` 系のパーサに `--repo-root PATH` を追加(任意)。
  help 文言: 「意見書(--review)を解決するリポジトリルート。マージ前の PR ブランチを
  checkout した worktree を指す用途。省略時は本 CLI の設置場所から自動決定」
- 指定された場合の検証(fail-closed):
  - ディレクトリが存在しない → エラーで発効中止
  - `PATH/config/governance.yaml` が存在しない → 「Ryza リポジトリの checkout に見えない」
    としてエラーで発効中止(`_repo_root()` のフォールバックと同じ目印を使う)
- 検証済みの `Path` を、意見書解決に関与する既存関数呼び出し(`resolve_reviewed_sha`、
  `load_review_artifact`、`missing_review_ref_warning` / `resolve_review_path` を使う経路)
  の `repo_root=` に渡す。**未指定時の挙動は完全に不変**(`_repo_root()` に委譲)。

### 2. 記録への痕跡

- `--repo-root` を使った場合、決定の `note` に `repo_root=<渡された絶対パス>` を追記する
  (既存の note 組み立てに追加。監査時に「どの checkout の意見書を読んだか」を追える)。
  note への追記形式は既存の書式に倣うこと。

### 3. モジュール docstring / usage の更新

- decisions.py 冒頭の usage 例(L44 付近)に `--repo-root` の例を 1 行追加。
- 「別ブランチにしか無い意見書」の既存記述(`--review-missing-ok` を案内している箇所)に、
  worktree がローカルにあるなら `--repo-root` が正道である旨を追記。

## テスト(tests/governance/test_decisions.py に追記)

DB を使うテストは既存の conftest 慣行に従う。実行は
`RYZA_TEST_DATABASE_URL=postgresql://ryza:ryza@localhost:15432/ryza PYTHONPATH=$PWD/src \
/Users/mmiyazaki/Projects/sukifura/ryza/.venv/bin/python -m pytest tests/governance/ -q`
(**共有 DB のため全体スイートは実行しない**。tests/governance/ のみ)。

1. `--repo-root` に有効な worktree 相当ディレクトリ(tmp_path に `config/governance.yaml`
   と `docs/reviews/x.md`(front matter 付き)を作る)を渡すと、意見書の `reviewed_sha` が
   採用され、由来が review_artifact になる
2. `--repo-root` が存在しないパス → 発効せずエラー(DB に決定が書かれないこと)
3. `--repo-root` は存在するが `config/governance.yaml` が無い → 発効せずエラー
4. `--repo-root` 未指定の従来経路が不変(既存テストが green のままであることで担保。
   既存テストの書き換えはしない)
5. note に `repo_root=` が記録される

## 受け入れ基準(合否判定)

- [ ] 上記テストがすべて green、既存 tests/governance/ が green
- [ ] `--repo-root` 未指定時の挙動が diff 上も不変(既存関数のシグネチャ・既定値を変えない)
- [ ] ruff check が clean
- [ ] governance コードは保護領域 → 独立審査が入る前提で、変更は Issue #132 の範囲に限定
      (1提案=1決定制約や veto 系のロジックに触れない)

## 触ってはいけないもの

- `normalize_reviewed_sha` / 1提案=1決定の UniqueViolation 処理 / veto 系
- 既存テストの変更・削除
- decision 24 の既存レコード(是正しない。Issue #132 で受容済み)
