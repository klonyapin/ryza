---
review: t028-a18-followups
reviewed_sha: b4654b9a118edfbd8bd1bd3285705be68b6f1a7d
reviewer: independent-reviewer (opus)
review_date: 2026-08-04
verdict: approve
---

# 意見書: T-028 A-18 フォローアップ(Issue #131)独立審査

## 総合判断

**approve**。変更は指示書のスコープ(開示の固定・再試行間隔・dry-run 判定キーの分離)に厳密に閉じており、A-18-1〜8 の判定・`has_findings`・`pr_verification_degraded` の意味は一切変わっていない。F-2 是正の性質(一時障害を `_reachable=False` に永続キャッシュしない)は backoff 追加後も保たれている。tests/audit/ は 214/214 green(9:32)、ruff は clean。

## 観点別合否

### 観点1: 検査ロジックの意味不変

**合格**。以下を確認した:

- `has_findings` の判定式は変更なし(a18.py:3244-3267)。何を違反として集計するかは同一
- `pr_verification_degraded` の判定は変更なし(a18.py:3232-3241)。`prs_verified is False` または `pr_verification.failed_open > 0` で True
- A-18-1〜8 各検査の scanner 本体(check_versions・check_deemed 系・check_reviewed 系・check_unrecorded 系・reminder tamper 系)には差分なし(`git diff main...HEAD -- src/ryza/audit/a18.py` の該当行は `_unreachable_reason` の backoff 追加と `run_a18` の `db_connected` 追記のみ)
- 変更は 3 箇所に限定される: (i) `PRVerifier` に backoff フィールド 2つと L444 の再問い合わせ抑制、(ii) `run_a18` 返却 dict に `db_connected` を1行追加、(iii) `build_alert_embed` の dry_run 判定を `decision_refs_verified` から `db_connected` へ差し替え(a18.py:3665)

### 観点2: F-2 是正の性質の保持

**合格**。backoff は `_reachable=False` に固定する方向へは作用しない:

- `status == "error"` 系のパスで `self._reachable` に代入する箇所は追加されていない(a18.py:486-528)。error は依然として `_reachable` を None のまま残す
- backoff は `_last_probe_error_at` に monotonic を記録するだけで、`_reachable` は触らない
- interval 経過後は `if elapsed < _REACH_RETRY_INTERVAL_SEC` が偽になり、必ず API を叩く(fail-open の拡大なし)
- `status == "ok"` および `status == "not_found"` の分岐で `_last_probe_error_at` / `_last_probe_error_reason` を `None` にリセットするため、回復後の状態が汚染されない(a18.py:507-517)
- テスト `test_repo_reachability_error_retries_after_backoff_interval` が interval 経過後の再呼び出しを固定している

副作用として backoff 期間中は `_last_probe_error_reason` を返すが、これは `_reach_reason` とは独立キャッシュで、`_reachable` を汚さない(照合を殺す方向ではなく、同じ結果を返しつつ API 負荷だけ下げる設計)。

### 観点3: 軽微-3 の分離の正しさ

**合格**。以下を確認した:

- `decision_refs_verified` の値は `conn is not None` のまま不変(a18.py:3054)
- `decision_refs_verified` を参照する既存 3 箇所(A-18-5: L3484, A-18-7: L3538, A-18-8: L3584 — いずれも緑側 elif 分岐)は差分なし。値と利用箇所が意図どおり保持されている
- `db_connected` は `run_a18` の唯一の return 経路(a18.py:3030)で `conn is not None` として付与される。build_alert_embed に渡る他の result 生成経路は無い(`grep build_alert_embed` の結果: 定義 3290 と enqueue 経由 3695 の内部呼び出しのみ、いずれも run_a18 の返却値を使う)
- `build_alert_embed` の dry_run 判定は `not result.get("db_connected")`(a18.py:3665)。既存の A-18-5/7/8 elif 分岐は独立して `decision_refs_verified` を見るので、片方の意味が変わってももう片方は正しく振る舞う

### 観点4: テストの固定力

**合格**。回帰テストは十分に固定的である:

- **中-1**: `test_pr_verification_degraded_true_when_prs_verified_false` が `pr_verification_degraded` を直接 assert、`test_verify_prs_false_embed_discloses_disabled_verification` が embed の ⚠️ タイトル・専用 field の name・value(「照合が無効化された実行」)を三重に固定。この 3 点のどれかを緩めれば即赤になる
- **軽微-1**: `test_a18_7_verification_degraded_is_excluded_and_disclosed` の末尾に A-18-7 field の value に「照合縮退 1 件を分母から除外」が入ることを assert。docstring も実態(field 内・タイトル側は別経路)に修正されている
- **軽微-2**: backoff テスト 2 本で「interval 未満は API を叩かない・interval 経過後は必ず再試行する」の両面を固定。monotonic を monkeypatch で決定論的に進めており flaky 要因なし
- **軽微-3**: `test_dry_run_title_uses_db_connected_key` の (c) ケースで `db_connected=False` かつ `decision_refs_verified=True` の不整合入力でも DRY-RUN が付くことを固定 — 「相乗り解消」の意図を回帰から守る設計

テストの内部密結合の懸念: `_minimal_result` と `test_a18_7_...` の inline dict は多数のキーを列挙するが、これは build_alert_embed が触るキー集合そのもので、実装の内部都合ではなく契約に近い。過剰結合ではない。

### 観点5: スコープ逸脱

**合格**。`git diff main...HEAD --stat`:

```
docs/tasks/T-028-a18-followups.md | 110 ++++++++
src/ryza/audit/a18.py             |  44 +++-
tests/audit/test_a18.py           | 235 +++-
```

3 ファイルのみ。governance / ledger / migrations / IPS 等の他保護領域には触れていない。

## 所見

**重大**: なし。
**中**: なし。
**軽微**: なし(強いて挙げれば下記の観察のみ。所見扱いにはしない)。

### 観察(所見にしないメモ)

- `_REACH_RETRY_INTERVAL_SEC = 60.0` はモジュール定数だが、テストの一部が `a18._REACH_RETRY_INTERVAL_SEC` を参照している(tests/audit/test_a18.py 内の backoff テスト)。定数名が変わればテストは追随できる。runtime API ではない(過剰なテスト密結合ではない)
- `_last_probe_error_reason` は `not_found` パスでも None にリセットされる(a18.py:515-517)。not_found は `_reachable=False` に永続キャッシュされる終端状態なので実質到達しないが、防御的でよい

## 反対意見書(この PR が間違っている場合の理由トップ3+代替案)

議論規約2に従い、この approve が誤っている場合のリスクトップ3を列挙する。

### 反対1: backoff の 60 秒固定値は運用条件によっては短すぎる/長すぎる

- **主張**: `_REACH_RETRY_INTERVAL_SEC` を定数固定にすると、GitHub API rate limit の実際の窓(secondary rate limit は 60 秒〜数分)より短ければ増悪が続き、長すぎれば回復検知が遅れる
- **採否**: 却下
- **根拠**: (i) A-18 は週次バッチで、実行時間はせいぜい数分。60 秒は増悪防止としては十分機能する(1 run 内で「毎 PR 照合ごとに叩く」から「最悪でも数回」に減る)。(ii) 週次バッチなので回復検知の遅れは事実上ゼロ影響(次週の run で必ず再試行)。(iii) 環境変数化は YAGNI で、指示書のスコープ(最小・プロセス内)を逸脱する

### 反対2: dry_run 判定を `db_connected` に切り替えたことで、既存の DRY-RUN 開示が意図せず外れるケースがある可能性

- **主張**: 従来は `decision_refs_verified=False` を条件にしていたが、今後 `db_connected=True` かつ `decision_refs_verified=False` の状態(将来的に conn はあるが決議参照検証だけ落ちた)が現れると、以前は DRY-RUN 表示していた実行が消える
- **採否**: 却下
- **根拠**: (i) 現在 `decision_refs_verified` の定義は `conn is not None` で `db_connected` と同一。値の乖離は将来仕様変更が入った場合のみ発生し、そのときは変更者が dry_run 判定の意味も見直すべき(まさに軽微-3 が防ごうとしている「相乗り」の逆パターン)。(ii) 変更後の意味は「DB につながっていない=否認検出不能」の一点に純化されており、意味論的に正しい。(iii) `test_dry_run_title_uses_db_connected_key` (c) ケースが「decision_refs_verified の値には依存しない」ことを回帰で固定している

### 反対3: `_minimal_result` / 手組み result dict のテストが多く、実装内部形状に対する結合が強い

- **主張**: build_alert_embed に渡す result dict のキーを 20 個以上手で並べるテストは、`run_a18` の返却 dict の形状が変わるたびに壊れる。結果として「実装を変えるとテストも直す」保守負担が増える
- **採否**: 一部採用(所見にはしない)
- **根拠**: (i) 実装契約テストとしては正しい方向 — build_alert_embed が読むキーは実質的に公開 API に近い。(ii) ただし将来キーを追加する側は、`_minimal_result` を1箇所更新すれば済む集約になっている(手組み dict は 2 箇所のみ)。(iii) 代替案として conftest.py の fixture 化は考えられるが、指示書の「既存テストの流儀に倣う」に反するため今回のスコープでは不採用でよい

## 結論

反対すべき点を探して、実装スコープ・意味論・テスト実効性・F-2 の性質保持のいずれにおいても実質的な弱点を見つけられなかった。approve とする。
