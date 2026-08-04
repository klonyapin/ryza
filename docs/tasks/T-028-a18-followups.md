# T-028: A-18 監査コードのフォローアップ4件(Issue #131)

## 背景(自己完結)

PR #129(A-12 是正 F-2/F-3/F-9)の独立役員審査で「合否に含めない」とされた所見4件の
フォローアップ。対象は `src/ryza/audit/a18.py` と `tests/audit/test_a18.py`。
**監査コードは保護領域(config/governance.yaml area: audit)** — 変更は Issue #131 の
範囲に厳格に限定し、検査ロジックの意味を変えないこと。

重要な前提: 所見 [中-1] が指摘した「verify_prs=False でも embed が緑」は、審査と
並行して入った別是正(commit 36e9325、`pr_verification_degraded` の導入)で**既に本質
解消されている**。現行 main では `prs_verified=False` →
`pr_verification_degraded()` が True(a18.py L2598-2599)→ `has_findings()` True →
タイトル ⚠️+専用 field「⚠️ GitHub PR 実在照合が成立していない」(L2913-2925)。
したがって [中-1] の作業は**再実装ではなく回帰テストによる固定**である。実装前に必ず
現行コードで上記経路を自分で確認し、もし開示が欠ける経路が実在すれば(想定外)、
その最小修正を先に行うこと。

## 作業ディレクトリ

git worktree(あなたに割り当てられたもの)。ブランチはそのまま使い、**push はしない**
(検収者が行う)。コミットメッセージは日本語+
`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` トレーラ。

## 変更内容

### 1. [中-1] verify_prs=False の開示を回帰テストで固定(tests のみ)

`tests/audit/test_a18.py` に追加:

- `run_a18(..., verify_prs=False, conn=...)` 相当の result dict(既存テストの流儀で、
  実際に `run_a18` を呼ぶか最小の result を組み立てるかは既存テストに倣う)に対し
  `a18.build_alert_embed(result)` を呼び、以下を assert:
  - タイトルに `⚠️`(所見あり=緑でない)
  - fields 内に name が「⚠️ GitHub PR 実在照合が成立していない」の field が存在し、
    value に「照合が無効化された実行」を含む
- `pr_verification_degraded({"prs_verified": False, ...})` が True である単体 assert
  (将来この経路が緩められたら即赤になる固定点)

### 2. [軽微-1] test_a18_7_verification_degraded_is_excluded_and_disclosed の過大主張

同テストの docstring は「報告 embed のタイトルに『照合縮退』を明示する」と主張するが、
本体は `check_unrecorded_protected_prs` の scan 結果しか assert していない。是正は
**assertion を足す**方向(docstring の主張を弱めるのではなく実態を強くする):

- 同テスト末尾(または独立した新テスト)で、`unverified_protected_prs=1` を含む
  result dict から `build_alert_embed` を作り、A-18-7 の field(緑側 elif 分岐
  a18.py L2835-2851)の value に「照合縮退 1 件を分母から除外」が含まれることを assert
- docstring は実態(field での開示。embed の「タイトル」ではない)に合わせて修正

### 3. [軽微-2] repos/<slug> 再問い合わせの backoff(a18.py)

現状: `PRVerifier._unreachable_reason()`(L444-474)は一時障害(`status == "error"`)を
キャッシュしない(F-2 是正 — 1回のレート制限で全照合を殺さないため)。副作用として、
障害が継続している run では PR 照合のたびに `repos/<slug>` へ即時再問い合わせし、
レート制限をさらに悪化させる。

是正(最小・プロセス内):

- `PRVerifier` に `_last_probe_error_at: float | None` フィールド(dataclass field、
  repr=False)と、モジュール定数 `_REACH_RETRY_INTERVAL_SEC = 60.0` を追加
- `_unreachable_reason()` で `status == "error"` のとき `time.monotonic()` を記録し、
  前回エラーから `_REACH_RETRY_INTERVAL_SEC` 未満の呼び出しは API を叩かずに
  前回のエラー理由を返す(理由文字列は保持しておく)
- `ok` / `not_found` の永続キャッシュ挙動、および「エラーを `_reachable=False` に
  固定しない」という F-2 是正の性質は**変えない**(interval 経過後は必ず再試行する)
- docstring に backoff の意図(レート制限中の自己増悪防止・審査 #131 軽微-2)を追記
- テスト: 呼び出し回数を数える fake api_get で
  (a) エラー直後の2回目の check では repos/<slug> が再呼び出しされない、
  (b) `_REACH_RETRY_INTERVAL_SEC` 経過後(monkeypatch で time.monotonic を進める)は
  再呼び出しされる、を assert

### 4. [軽微-3] dry-run 判定の独立化(a18.py)

現状: `build_alert_embed` のタイトル `[DRY-RUN(照合制限あり)]` は
`not result.get("decision_refs_verified")`(L2960)で判定しており、「DB 接続なし」の
意味に相乗りしている。将来 decision_refs_verified の意味が変わるとタイトルが偽る。

是正(最小):

- `run_a18` の返却 dict に `"db_connected": conn is not None` を追加
  (`decision_refs_verified` は既存の意味・利用箇所のまま**変更しない**)
- `build_alert_embed` の dry_run 判定を `not result.get("db_connected")` に変更し、
  コメントを更新(「decision_refs_verified への相乗りを解消 — #131 軽微-3」)
- テスト: `db_connected=False` の result でタイトルに DRY-RUN プレフィクスが付き、
  `db_connected=True` では付かないことを assert(decision_refs_verified の値に
  依存しないことも1ケースで固定)

## テスト実行

`RYZA_TEST_DATABASE_URL=postgresql://ryza:ryza@localhost:15432/ryza PYTHONPATH=$PWD/src \
/Users/mmiyazaki/Projects/sukifura/ryza/.venv/bin/python -m pytest tests/audit/ -q`
(**共有 DB のため全体スイートは実行しない**。tests/audit/ のみ。)
仕上げに `ruff check src tests`。

## 受け入れ基準(合否判定)

- [ ] 上記の新規テストがすべて green、既存 tests/audit/ が green
- [ ] 検査ロジック(何を違反とするか)の意味変更がない — 変更は開示・再試行間隔・
      判定キーの分離のみ
- [ ] `decision_refs_verified` の既存の値・利用箇所(A-18-5/7/8 の elif 分岐)が不変
- [ ] F-2 是正の性質(一時障害を永続キャッシュしない)が保たれている
- [ ] ruff check が clean

## 触ってはいけないもの

- 各検査(A-18-1〜A-18-8)の判定ロジック本体
- `has_findings` / `pr_verification_degraded` の判定内容(テストで固定するのみ)
- 既存テストの削除(docstring 修正と assertion 追加は可)
- governance / ledger など他モジュール
