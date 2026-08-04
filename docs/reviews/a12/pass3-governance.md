# 第1回フル実装監査(A-12)報告書

**監査コード**: A-12
**監査対象**: 保護領域制度・A-18 監査・承認記録・意見書処理
**監査人**: 独立監査部門(実装系統外モデル)
**監査日**: 2026-08-05

---

## 検査対象一覧と検査完了状況

| 検査項目 | 状態 |
|---|---|
| ① 保護領域変更の検出に穴がないか | 検査完了(所見あり) |
| ② 承認記録の偽装耐性 | 検査完了(所見あり) |
| ③ A-18 の各検査が「実装から独立した参照」を持つか | 検査完了(所見あり) |
| ④ 意見書 front matter 処理の迂回口 | 検査完了(所見なし) |
| ⑤ 監査コード自身は誰が監査するか | 検査完了(所見あり) |

---

## 所見一覧

### A-12-01: PRVerifier の初回到達性確認が NotFound を error に丸める競合状態

**重大度**: [重要]

**根拠**:

`src/ryza/audit/a18.py` `PRVerifier._unreachable_reason` メソッド:

```python
def _unreachable_reason(self) -> str | None:
    if self._reachable is None:
        status, detail = self.api_get(f"repos/{self.slug}")
        self._reachable = status == "ok"
        self._reach_reason = (
            None
            if self._reachable
            else (
                f"リポジトリ {self.slug} に API でアクセスできない"
                f"(認証不備・不達の可能性: {detail if status == 'error' else 'HTTP 404'})"
            )
        )
    return self._reach_reason
```

本メソッドは `_reachable` フィールド(`field(default=None)`)をキャッシュとして使用し、初回のみ `repos/<slug>` への到達性確認を行う。

**問題**: `_github_api_get` は GitHub API のレート制限(HTTP 403)や一時的なネットワーク障害を `("error", 理由)` として返す。`_unreachable_reason` は `status != "ok"` を全て「到達不能」として `_reachable = False` に固定する。一度でもレート制限に触れると、**プロセスの生存期間中は全ての PR 照合が `unverifiable`(fail-open)になる**。実運用では週次監査が別プロセスで起動するため影響は限定的だが、同一プロセスで複数コミットを走査する際(A-18-7 など)は、最初の API 呼び出しが一時障害に当たると以降全件が縮退する。

`PRVerifier` の docstring は「404 を『不在』と解釈しない」ことを設計意図として明記するが、`status == "not_found"` と `status == "error"` の区別を `_unreachable_reason` 内で行っておらず、404 の場合は `_fetch` 側で個別に処理されるため実害はないものの、error の一時障害まで到達不能として永続キャッシュする挙動は docstring の意図と一致しない。

**推奨是正**:

`_unreachable_reason` において、`status == "error"` の場合は `_reachable` を `None` のまま(未確定)として保ち、次回の呼び出しで再試行を許可する。到達不能のキャッシュは `status == "not_found"` のみに限定する。

---

### A-12-02: A-18-7 の PR 番号抽出が `verified_pr_merge` を経由せず件名のみに依存する

**重大度**: [重要]

**根拠**:

`src/ryza/audit/a18.py` `check_unrecorded_protected_prs` 関数:

```python
for sha in _rev_list(repo, since_commit, "--first-parent", "--merges"):
    subject = _git(repo, "log", "-1", "--format=%s", sha).strip()
    pr_number = pr_number_from_subject(subject)
    if pr_number is None:
        continue
    if len(_git(repo, "log", "-1", "--format=%P", sha).split()) != 2:
        continue
    # ... файлов проверка ...
    checked += 1
```

本関数は `pr_number_from_subject(subject)` で件名から PR 番号を抽出した後、その番号が GitHub 上で実在するかの検証を**行わず**に `checked`(緑の分母)へ加算し、承認記録の照合へ進む。docstring の最後で「件名偽装は本検査では封じられない。偽装の封鎖は A-18-1 の `PRVerifier` が担う。A-18-7 はその照合に依存する」と明記されているが、`PRVerifier` は `check_unrecorded_protected_prs` に渡される `repo_path` から復元された `repo_slug` に依存しており、本関数内では `pr_verifier` を一切参照しない。

一方 A-18-4 の `check_direct_pushes` は:

```python
is_pr, not_pr_detail = verified_pr_merge(subject, pr_verifier, sha)
```

と `verified_pr_merge` 経由で PR 実在照合を行っている。A-18-7 はこれと同じ経路を使わず、件名の自己申告を信用して承認記録の有無を判定している。

**影響**: A-18-7 の緑の分母(`checked`)は、架空の PR 番号を件名に持つマージコミットを含む。保護領域に触れた架空 PR マージが存在し、対応する承認記録も当然ない場合、これは「`--deemed-for-pr` の実行忘れ」として正しく所見に出るため、結果論として検出漏れにはならない。しかし緑の分母が実在しない PR を含むことで、「N 件中 M 件の記録漏れ」という報告の信頼性が損なわれる。また、実在する PR の番号を件名に流用した自作マージは、その PR に対応する承認記録が存在すれば緑を通過する(その記録が別の PR の変更を承認したものであっても)。

**推奨是正**:

`check_unrecorded_protected_prs` においても `verified_pr_merge` を経由して PR の実在+マージ SHA 帰属を確認した上で `checked` に加算する。実在確認が縮退した場合は分母から除外し、縮退件数を `UnrecordedPRScan` で開示する。

---

### A-12-03: `minutes.body_md` が UPDATE 可能であり、追記オンリーが DB 制約でない

**重大度**: [重要]

**根拠**:

設計文書 `00-system-design.md` §4 は「判断来歴の全保存」(設計原則4)を掲げ、`governance.minutes.body_md` は議事録の証憑として扱われる。`boardroom.py` の `parse_speaker_sequence` 関数の docstring は「テーブルは追記オンリー(0013)なので保存 = 確定」と主張する:

```python
def save_office_chat_minute(...):
    """...
    テーブルは追記オンリー(0013)なので保存 = 確定。
    """
```

しかし、本監査で確認した範囲では `0013` のマイグレーションファイルが提示されていないため、`governance.minutes` テーブルに UPDATE/DELETE 禁止トリガが存在するかを確認できない。`governance.stances` については docstring が「追記オンリー(UPDATE/DELETE 禁止トリガ)」と明記するが、`minutes` テーブルについては同等の制約の存在が文書から確定しない。

**問題**: `parse_speaker_sequence` は `minutes.body_md` の内容から決議の鮮度を判定する(`minute_critic_recency` → `mark_resolution`)。もし `body_md` が UPDATE 可能であれば、決議後に本文を書き換えて話者列を変更することで、過去の議事録の「批判の鮮度」判定を事後に反転させることができる。`boardroom.py` の凍結ラベル表(`_LEGACY_LABEL_TO_SPEAKER`)や不変キーの設計は、本文が不変であることを前提としている。

`governance.yaml` の `protected_areas` には `migrations/*.sql` が登録されている(area: schema)ため、仮にトリガが存在すれば保護領域経由で変更は検出される。しかし、トリガ自体が存在しない場合、後続の migration からトリガを削除する変更は A-18-1 が検出するものの、データの UPDATE 自体を防ぐ統制はスキーマ側に宿らない。

**推奨是正**:

① `migrations/0013` の内容を監査環境で確認し、`governance.minutes` に UPDATE/DELETE 禁止トリガが存在することを検証する。② トリガが存在しない場合は追加する。③ 存在する場合は `tests/test_migrations.py`(不変条件テスト・保護領域登録済み)に存在検査を追加する。

---

### A-12-04: approved_decisions の「無条件受理」と training data poisoning の潜在的経路

**重大度**: [中]

**根拠**:

`src/ryza/governance/decisions.py` `record_deemed_approval` 関数は、3専決事項のチェックと kind 語彙チェックを行った後に INSERT を実行する:

```python
if kind in RESERVED_KINDS:
    raise ReservedMatterError(...)
if kind not in KINDS:
    raise ValueError(f"未知の提案種別: {kind}")

decided_by = f"{SYSTEM_ACTOR_PREFIX}{source}"
_raise_if_decided(conn, proposal_ref)
# INSERT ...
```

`source` パラメータは `DEFAULT_DEEMED_SOURCE = "deemed"` が既定値であり、任意の文字列を受け付ける。`decided_by` 列は `system:<source>` となる。0019 の CHECK は `decided_by LIKE 'system:%'` のみを要求すると docstring に記載される。

**問題**: `source` に SQL インジェクションや HTML インジェクション文字列が含まれていても、DB の CHECK は prefix のみを検証するため受理される。これ自体は direct SQL 実行の経路が限られるため即時の脆弱性ではないが、`decided_by` の値は監査報告 embed やダッシュボードに表示される可能性があり、後続の表示系でのエスケープ漏れがあった場合に XSS の経路になる。

ただし、本関数の呼び出し側は CLI(`decisions.py main`)と `notices.announce_deemed_approval` に限られ、CLI の `--source` 引数は `argparse` で文字列として受け取る。執行側コードからの直接呼び出しは保護領域(`src/ryza/governance/**` = area: governance_engine)にあり、A-18-1 の対象である。

**推奨是正**:

`source` パラメータに文字種制限(英数字+アンダースコア・ハイフン程度)を課す。または `SOURCE_PATTERN` 正規表現で検証する。

---

### A-12-05: `RATIFICATION_COMMIT` 定数がリポジトリに存在しない場合の例外メッセージ

**重大度**: [軽微]

**根拠**:

`src/ryza/audit/a18.py` `check_protected_commits` 関数:

```python
if since_commit and not _git_ok(repo, "cat-file", "-e", f"{since_commit}^{{commit}}"):
    raise ValueError(f"発効基準コミットがリポジトリに存在しない: {since_commit}")
```

`RATIFICATION_COMMIT = "c7af81ef85cc9f45bb7881ffc45769abfbc771dc"` は定数として定義されるが、浅い clone(shallow clone)や履歴の切り詰められた環境では、このコミットが存在しない場合がある。その際の例外メッセージは上記の通りだが、この例外が `run_and_report` で捕捉されずそのまま送出されると、監査ジョブ全体が失敗し `run.finish("failed")` になる。

`check_direct_pushes` と `check_unrecorded_protected_prs` と `check_reviewed_sha_agreement` も同様の検証を持つが、それぞれ `PR_RULE_BASELINE_COMMIT` と `DEEMED_RECORD_BASELINE_COMMIT` を使用する。これらの定数は全て同一ファイル内にハードコードされており、リポジトリの履歴が改変された場合(例: rebase による SHA 変化)に全ての基準コミットが同時に無効になる。

**推奨是正**:

浅い clone 環境での実行を明示的にサポートするか、不支持の場合はデプロイ手順に full clone の要件を明記する。基準コミットのハードコードは変更耐性が低いため、`config/governance.yaml` への外部化(ただし設定ファイル自体が保護領域であるためトレードオフ)を検討する。

---

### A-12-06: 承認トレーラの `reviewed` が同一行に複数ある場合の過剰拒絶

**重大度**: [軽微]

**根拠**:

`src/ryza/audit/a18.py` `reviewed_shas` 関数:

```python
for line in approval_trailers(message, trailer):
    if _REVIEWED_KEY in line.duplicates:
        return (), "同一トレーラ行に reviewed が複数ある(どれが審査対象か確定できない)"
```

1行に `reviewed=<sha1> reviewed=<sha2>` と書かれた場合、様式不備として空タプルと理由を返す。これは `A-12-06` で指摘した偽装対策(低-7)として妥当である。ただし、同一コミットメッセージ内に**別の行**で複数の `Approved:` トレーラがあり、それぞれに異なる `reviewed=` がある場合は正当な運用(複数承認記録の併記)として受理される。この区別は `approval_trailers` が行単位でパースするため正しく機能する。

本件は所見というより設計の妥当性確認である。`TrailerLine.duplicates` が同一行内の重複のみを捕捉し、行をまたぐ重複は捕捉しないことは、docstring の意図(低-7 の封鎖)と整合する。検査したが所見なし。

---

### A-12-07: ④ 意見書 front matter 処理の迂回口

**重大度**: 検査したが所見なし

**検査内容**:

意見書(`docs/reviews/`)の処理について以下を検証した:

1. `decisions.py` `_resolve_deemed_args` 関数の `--review` 引数は、`--deemed-for-pr` と `--kind pr` の組み合わせで必須とされ、`--notice` では代替できない(`raise ValueError`)。この検査は関数の冒頭で行われ、後続の処理に進む前に拒否される。
2. `review_ref` は `build_pr_notice` で通知本文に挿入され、`_with_review_line` で手書き文面にも追加される。両経路で審査参照の行が本文に残る。
3. `review_ref` は `decisions.review_ref`(0029)に構造化して記録される。
4. `missing_review_ref_warning` はリポジトリ内パス形式の `review_ref` が実在しない場合に警告を返すが、URL 形式(`http://` 等)は検査をスキップする。これは設計意図(ネットワーク越しの実在確認を CLI の責務にしない)と一致し、遡及登録やリポジトリ外の審査を塞がない。

**結果**: front matter の構文解析に依存する処理は存在せず、`review_ref` は文字列として扱われる。迂回口は発見されなかった。

---

### A-12-08: ③ A-18 の各検査が「実装から独立した参照」を持つか

**重大度**: 検査完了(所見あり・下記 A-12-09〜A-12-12 に分離)

**検査内容**:

設計文書は A-18-1 を「保護パスの変更コミットの Approved トレーラと承認記録の突合」と定義する。各検査が実装コードの書き換えによって黙る(自己参照になる)かを検証した。

`governance.yaml` の `protected_areas` には広範なテストファイル群が `area: invariant_tests` として登録されており、「実装から独立した参照」を持つテスト(ゴールデンファイル・凍結 SQL・発効値の写し)を保護している。この設計は、テストの参照を先に書き換えてから実装を緩める二段手順を封じることを明示的に意図している。

以下に個別の所見を分離する。

---

### A-12-09: A-18-8 `check_reviewed_sha_agreement` が `_log_messages` で全コミットを走査するが、緑の分母が「0件」を隠す

**重大度**: [中]

**根拠**:

`src/ryza/audit/a18.py` `check_reviewed_sha_agreement` 関数と `ReviewedShaScan` dataclass:

```python
@dataclass(frozen=True)
class ReviewedShaScan:
    """...
    ``compared`` は**緑の分母**(両方の値が揃っていて実際に突合できた決定数)である。
    ...
    """
    findings: list[dict[str, Any]]
    compared: int
    trailer_only: int
    record_only: int = 0
```

`check_reviewed_sha_agreement` は `_log_messages` で `since_commit..HEAD` の全コミットを走査する。各コミットのトレーラから `reviewed=` を読み、対応する決定の `reviewed_sha` と突合する。

**問題**: `compared`(緑の分母)は「両方の値が揃って突合できた決定数」である。0029(reviewed_sha 列の追加)以前の決定は `reviewed_sha` が NULL のため `trailer_only` に計上され、`compared` には入らない。移行期において `compared = 0` は正常状態(まだ1件も突合対象が無い)であり、報告 embed は:

```python
"突合対象なし(トレーラの reviewed= と承認記録の reviewed_sha が"
"揃った決定が 0 件 — 一致の確認ではない)"
```

と表示する。この文言は妥当である。ただし、**squash マージへの移行**などで `Merge pull request` 件名が消失した場合、A-18-1 の PR 承継が効かなくなり、トレーラを持つコミット自体が減る。その結果 `compared` と `trailer_only` が同時に 0 になり、A-18-8 が静かに無音になる経路が存在する。docstring の SHA-2 注記は `record_only` についてこの経路を指摘しているが、`trailer_only + record_only == 0` の場合(両方の経路が同時に消える)には報告 embed に明示的な警告が出ない。

**推奨是正**:

`trailer_only == 0 and record_only == 0 and compared == 0` の場合、報告 embed に「突合対象が1件もない状態が継続している — squash マージ移行等でトレーラ付きコミットが消失していないか確認」という注記を追加する。

---

### A-12-10: `decisions_for_pr_number` の SQL が `LIKE` を使用し、DB インジェクションは安全だが件数誤認の経路がある

**重大度**: [軽微]

**根拠**:

`src/ryza/audit/a18.py` `decisions_for_pr_number` 関数:

```python
def decisions_for_pr_number(conn: Any, pr_number: int) -> list[dict[str, Any]]:
    """...
    ``LIKE '%%/pull/<N>'`` は末尾固定なので
    ``/pull/1`` が ``/pull/12`` に誤一致しない。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT decision_id, proposal_ref, recorded_decision, effective_decision "
            "FROM governance.current_decisions "
            "WHERE proposal_ref LIKE %s ORDER BY decision_id",
            (f"%/pull/{pr_number}",),
        )
```

`pr_number` は `int` 型であるため SQL インジェクションの危険はない(`f"%/pull/{pr_number}"` は整数埋め込み)。しかし、docstring は「``/pull/1`` が ``/pull/12`` に誤一致しない」と主張する。実際、`LIKE '%/pull/1'` は `/pull/1` で終わる文字列のみに一致し、`/pull/12` は `/pull/12` で終わるため一致しない。この主張は正しい。

ただし、`proposal_ref` にクエリパラメータが付いている場合(`https://github.com/owner/repo/pull/1?diff=1` など)は `LIKE '%/pull/1'` に一致しない。これは `pr_proposal_ref` 関数が生成する URL(`https://github.com/<slug>/pull/<N>`)にはクエリパラメータが含まれないため、通常運用では問題ない。しかし、手書きの `proposal_ref` にクエリ文字列が含まれている場合は照合から漏れる。

**推奨是正**: 実害は限定的(手書きの `proposal_ref` にクエリパラメータが付くことは稀)。docstring に `proposal_ref` の形式前提(クエリパラメータなし)を明記する。

---

### A-12-11: ② 承認トレーラ偽装 — `trailer_approves` の戻り値が `None` の場合の呼び出し側の分岐

**重大度**: [中]

**根拠**:

`src/ryza/audit/a18.py` `check_protected_commits` 関数内の承認判定:

```python
verdict = trailer_approves(conn, message, trailer, pr_verifier=pr_verifier)
trailer_reason: str | None = None
if verdict is not None:
    if verdict.accepted:
        # ... continue (違反としない)
        continue
    trailer_reason = (
        "Approved トレーラの承認記録が有効でない: " + "; ".join(verdict.problems)
    )
```

`trailer_approves` はトレーラが無い場合に `None` を返す。`verdict is None` の場合は `trailer_reason` が `None` のまま下へ流れ、後続の PR 承継・evil merge 判定に進む。この分岐は正しい(トレーラが無い = 承認を主張していない = 別経由の承認を探す)。

しかし、`trailer_approves` の docstring は:

```python
def trailer_approves(...) -> TrailerVerdict | None:
    """...
    ``conn`` が ``None`` なら承認記録との突合はできないので、従来どおりトレーラの存在を
    もって受理する(ただし ``pr_verifier`` があれば PR URL の実在照合だけは効く)。
    """
```

`conn=None` の場合、`verify_decision_refs` は `resolved == 0` で `accepted=True`(従来どおり存在検査で受理)を返す。この経路では否認済みの承認があっても `conn` が無いため検出できない。これは `STANDARD_DISCLOSURES` で「DB 接続なしの実行のため…未照合」として開示されるため、隠蔽ではない。

**問題**: `conn=None` の実行で、保護領域に触れたコミットが `Approved:` トレーラを持ち、かつそのトレーラが**否認済み**の決定を参照している場合、`conn` が無いため `trailer_approves` は `accepted=True` を返し、違反として検出されない。dry-run での実行がこれに該当する。

`run_and_report` の `dry_run=True` は `run_a18` を `conn=None` で呼ぶ:

```python
if dry_run:
    result = run_a18(repo_path, ...)
```

dry-run で否認済み承認の検出が働かないことは、報告 embed の notes に「DB 接続なしの実行のため…未照合」として開示される。しかし、dry-run の結果を本番と同じ信頼度で扱う運用者がいる場合、否認済みの変更が見逃される。

**推奨是正**:

dry-run の報告 embed のタイトルに「DRY-RUN(照合制限あり)」を明記し、`has_findings` が `False` でも dry-run であることを報告から読み取れるようにする。または dry-run を廃止し、常に DB 接続を要求する。

---

### A-12-12: `_resolve_trailer_decision` が否認済みの決定も返すが、A-18-7 でこの挙動が暗黙に依存される

**重大度**: [軽微]

**根拠**:

`src/ryza/audit/a18.py` `_resolve_trailer_decision` 関数:

```python
def _resolve_trailer_decision(conn: Any, ref: str) -> dict[str, Any] | None:
    """...
    否認済みでも行は返す。本検査が見るのは**記録の有無と帰属**であって有効性ではない
    —— 否認済みの承認を参照するコミットは A-18-1 が既に無承認変更として列挙しており、
    ここで二重に鳴らすと「CLI の叩き忘れ」という本検査の信号が別種の違反に埋もれる。
    """
```

この設計意図(二重鳴らしの回避)は妥当である。A-18-7 は「記録の有無と帰属」を見て「CLI 叩き忘れ」を検出し、有効性は A-18-1 の担当と明確に分離されている。検査したが所見なし。

---

### A-12-13: ② 承認記録偽装 — `proposal_ref` に URL 以外の任意文字列が入る経路

**重大度**: [中]

**根拠**:

`src/ryza/governance/decisions.py` `record_deemed_approval` 関数:

```python
def record_deemed_approval(
    conn: psycopg.Connection,
    proposal_ref: str,
    kind: str,
    notice_ref: str,
    ...
) -> DeemedApproval:
    _require_text(proposal_ref, "proposal_ref")
    ...
```

`proposal_ref` は `_require_text` で空文字チェックを受けるのみで、URL 形式の検証がない。`UNIQUE(proposal_ref)` 制約は重複を防ぐが、形式は問わない。

CLI(`_resolve_deemed_args`)の `--proposal-ref` または `--deemed-for-pr` から渡される。`--deemed-for-pr` の場合は `fetch_pull_request` が GitHub から `html_url` を取るため URL 形式が保証される。しかし `--proposal-ref` で手動指定する場合は任意の文字列が入る。

**問題**: `proposal_ref` が `https://github.com/owner/repo/pull/99` でなく `my-proposal` のような短い文字列だった場合、A-18-1 のトレーラ照合(`trailer_approves` → `_verdict_for_ref`)は `current_decision(conn, ref)` で `proposal_ref` の完全一致を探す。短い文字列は偶然一致の可能性が高まる。例えば `test` という `proposal_ref` の決定が既にあり、新しい PR のトレーラに `Approved: test` と書かれた場合、無関係の決定で照合が通る。

ただし、`_verdict_for_ref` は裸の数字(`_BARE_NUMBER_RE`)を「照合不能」として弾くため、数字のみの偶然一致は防がれる。英字文字列の偶然一致は低確率だが、理論的には可能。

**推奨是正**:

`record_deemed_approval` で `proposal_ref` が GitHub PR URL 形式(`https://github.com/<owner>/<repo>/pull/<N>`)または `decision:<id>` 形式のいずれかに合致することを検証する。手作業の短縮参照を許可する場合は、名前空間(例: `manual:<name>`)を要求し、偶然一致を防ぐ。

---

### A-12-14: ⑤ 監査コード自身の監査 — `a18.py` が `org`・`bot` モジュールに依存する

**重大度**: [中]

**根拠**:

`src/ryza/audit/a18.py` の冒頭:

```python
from ryza import org
from ryza.bot import COLOR_FLASH, COLOR_NORMAL, DISCLAIMER
from ryza.bot.outbox import enqueue
```

`a18.py` は報告 embed の構築(`build_alert_embed`)のために `org`(役職アイコン等の台帳)・`bot`(色定数・免責事項)・`bot.outbox`(通知投入)に依存する。

これらのモジュールのうち `org` と `bot.outbox` は保護領域に登録されていない:

- `org` → `config/org.yaml`(保護領域の `config/*.yaml` に含まれるが、area は `L4` 層で `design_lead` 審査)および `src/ryza/org.py`(保護領域未登録)
- `bot.outbox` → `src/ryza/bot/outbox.py`(保護領域未登録)

**問題**: 報告 embed の内容(タイトル・色・author アイコン)は `org` と `bot` の定数で決まる。これらのモジュールが改変された場合、監査報告の見た目が変わる。例えば `COLOR_FLASH` が `COLOR_NORMAL` と同じ値に改変されれば、警告の有無が視覚的に区別できなくなる。`DISCLAIMER` が書き換えられれば、報告の文脈が変わる。

ただし、報告 embed の**内容**(所見の有無・違反の一覧)は `a18.py` 内部で決定され、`org`・`bot` は装飾的な要素(色・アイコン・免責)のみを提供する。`has_findings` と `build_alert_embed` の判定ロジックは `a18.py` に閉じており、外部モジュールの改変では「所見あり」が「所見なし」に反転することはない。

`enqueue` 関数のシグネチャ変更(引数の追加・削除)があれば `a18.py` の `enqueue_alert` が実行時エラーになるが、これは検査の改変でなく報告経路の障害であり、別種の問題である。

**推奨是正**:

監査報告の**判定ロジック**(`has_findings`・`build_alert_embed` の field 構築)は `a18.py` に閉じているため、現状で監査の信頼性は保たれている。ただし `COLOR_FLASH`・`COLOR_NORMAL`・`DISCLAIMER`・`org.author_for_role` が報告の視覚的識別に寄与するため、これらが保護領域外にあることは攻撃面を増やす。`src/ryza/org.py` を保護領域に追加するか、監査報告用の定数を `a18.py` 内に定数化する(モジュール間依存を断つ)ことを検討する。

---

### A-12-15: ① `ops/reminders.yaml` が保護領域に登録されていない

**重大度**: [重要]

**根拠**:

`config/governance.yaml` の `protected_areas` を走査した結果、`ops/reminders.yaml` は保護領域に登録されていない。しかし、`governance.yaml` のコメントと `decisions.py` の docstring は複数の箇所で `ops/reminders.yaml` のリマインダーを参照する:

- `trailer-v1-sunset`(2026-09-01)
- `ack-supersede-mechanism`
- `governance-deemed-notice-wiring`
- `deemed-auto-announce`
- `protect-equivalence-tests`
- `protect-invariant-tests-phase2`
- `protect-execution-config`

これらのリマインダーは統制の発火や是正の期限を持つシステムコンポーネントである。例えば `trailer-v1-sunset` は v1 トレーラの打ち切り期限を持ち、`ack-supersede-mechanism` は受容の承継機構を追加した理由を記録する。

`ops/**` は保護領域 `ops/deploy-*.sh` 等のスクリプトが個別に登録されているが、glob `ops/**` としての登録はなく、`ops/reminders.yaml` はどの保護パスにも一致しない。

**問題**: `ops/reminders.yaml` を無承認で変更することで、統制の発火タイミング(リマインダーの実行日・内容)を操作できる。例えば `trailer-v1-sunset` の期限を遠い未来に変更すれば、v1 トレーラの打ち切りが実質的に無期限延期される。これは保護領域の変更承認制度そのものの緩和を、保護領域外のファイル変更で達成できることを意味する。

**推奨是正**:

`ops/reminders.yaml` を `protected_areas` に追加する。`area` は `governance_engine` または新設の `ops_config` を検討する。統制の発火タイミングを定義するファイルは、統制そのものと同格に保護する必要がある。

---

### A-12-16: `src/ryza/governance/boardroom.py` の `_AMOUNT_PATTERN` が科学的記数法を拾わない

**重大度**: [軽微]

**根拠**:

`src/ryza/governance/boardroom.py`:

```python
_AMOUNT_PATTERN = re.compile(
    r"[¥￥$]\s*[\d,.]+"
    r"|[\d,.]+\s*(円|万|万円|億|億円|株|口|%|％|パーセント|ベーシスポイント|bp|倍|"
    r"jpy|usd|ドル)",
    re.IGNORECASE,
)
```

本パターンは「単位付きの数値」を重要決定の兆候として検出する。ただし `1e6 円`・`1.5e3%` のような科学的記数法を拾わない。また `1.5×10⁶` のような乗算表記も拾わない。

実運用での影響は限定的である(役員室チャットで科学的記数法が使われることは稀)。しかし、決定論ガードの設計意図が「金額・比率の表記を検証対象にする」ことであれば、科学的記数法のバイパスは意図しない抜け穴になる。

**推奨是正**:

`_AMOUNT_PATTERN` に `[eE][+-]?\d+` の科学的記数法パターンを追加する。ただし過検出の害(独立役員が1回多く発言する)は小さいため、優先度は低い。

---

### A-12-17: ⑤ メタ監査(年次)の実行基盤が未定義

**重大度**: [中]

**根拠**:

設計文書 `00-system-design.md` §6「監査部門自体の更新機構(メタ監査)」:

> 2. **年次メタ監査**: 年1回、**さらに別系統の AI** が「監査部門の監査」を実施 — 監査手続きの網羅性・実効性・形骸化(指摘の質の劣化)を評価する。IIA 実務の外部品質評価(EQA)の移植

本監査(A-12)は「第1回フル実装監査」であり、設計文書が定義するメタ監査(年次)とは別のものである。メタ監査は「監査部門の監査」であり、A-18 監査コード自体の網羅性・実効性を評価する。

**問題**: メタ監査の実行基盤(誰が・どの頻度で・どのプロンプトで監査コードを評価するか)が設計文書に記載されるのみで、実装や運用カレンダーに具体化されていない。`ops/reminders.yaml` にメタ監査のリマインダーが存在するかは、`ops/reminders.yaml` が提示されていないため確認できない。

現在の A-18 監査コードは、自己申告(`reviewed=`・`Approved:` トレーラ・承認記録)の突合を中心に設計されており、各検査が「実装から独立した参照」を持つよう慎重に設計されている。しかし、この設計自体の妥当性(例えば「`reviewed=` は申告であり証明ではない」という限界をどこまで受け入れるか)を評価するのはメタ監査の責務である。メタ監査が実行されなければ、A-18 の設計の限界が放置される。

**推奨是正**:

① `ops/reminders.yaml` にメタ監査(年次)のリマインダーを登録する。② メタ監査の実行手順(入力 = A-18 コード + 設計文書、出力 = 形骸化評価レポート)を `docs/design/` に文書化する。③ メタ監査の実行記録を `governance.decisions` または別テーブルに残し、実施されたことを証跡化する。

---

### A-12-18: `acknowledged_findings` の同一キー重複検出が「後のエントリを無効」にするが、逆順読み込みへの耐性がない

**重大度**: [軽微]

**根拠**:

`src/ryza/audit/a18.py` `acknowledged_index` 関数:

```python
for entry in gov.get("acknowledged_findings") or []:
    ...
    key = _ack_key(commit, paths)
    if key in index:
        # 同一キーの重複追記(独立役員審査 低-1)。後勝ち上書きを許すと...
        notes.append(...)
        continue
    ...
    index[key] = entry
```

`acknowledged_findings` の YAML リストを前から順に走査し、同一キーの重複を検出したら「後のエントリを無効」とする。YAML のリスト順序は意味を持つことが `config/governance.yaml` のコメントで明示される:

> `# 同一キーの重複追記も「書換」とみなし禁止**(独立役員審査 低-1)`

この設計は妥当である。YAML リストの順序はファイル内で固定されるため、逆順で読まれることはない。

**問題**: もし将来 `acknowledged_findings` が YAML リストから他の形式(例: YAML マッピングのキー)に移行された場合、順序の保証が失われる。YAML マッピングのキー順序は YAML 1.1 では保証されない(実装依存)。現在の YAML リスト形式では問題ないが、形式変更時の移行リスクがある。

**推奨是正**:

現状の YAML リスト形式を維持する限り問題はない。形式変更時に順序保証を失わないよう、移行手順に明記する。

---

### A-12-19: `sanitize_speech` の引用化がコードブロック内の話者行を拾う

**重大度**: [軽微]

**根拠**:

`src/ryza/governance/boardroom.py` `sanitize_speech` 関数:

```python
def sanitize_speech(text: str) -> str:
    """発言テキストの話者ラベル行・フェンス記号を無害化する(冪等)。
    ...
    """
    without_fence = prompting.neutralize_fences(text)
    return _SPEAKER_LABEL_LINE.sub(_quote_speaker_line, without_fence)
```

`_SPEAKER_LABEL_LINE` は `re.MULTILINE` で行頭一致する話者ラベル行を引用化する。Markdown のコードブロック(``` `...` ``` または `~~~...~~~`)内の行は、内容が「表示用の文字列」であって Markdown 構造の一部ではないため、コードブロック内の `代表:` で始まる行も引用化の対象になる。

**影響**: 役員が発言内で「以前の議事録を引用する」ときにコードブロックを使うと、その内側の話者行が引用化(`> `)され、表示が崩れる。これは統制の迂回ではなく表示上の問題である。

**推奨是正**:

`sanitize_speech` がコードブロック内をスキップするよう、コードブロックの開始・終了を追跡する。ただし、コードブロックの入れ子・言語指定等を完全にパースするのは複雑であり、表示上の問題に過ぎないため優先度は低い。

---

### A-12-20: `parse_speaker_sequence` の新書式判定が `MINUTE_META_HEADING` の文字列完全一致に依存する

**重大度**: [軽微]

**根拠**:

`src/ryza/governance/boardroom.py` `parse_speaker_sequence` 関数:

```python
keyed = [
    m.group("key")
    for m in _MINUTE_KEY_LINE.finditer(body_md)
    if m.group("key") in _MINUTE_SPEAKER_KEYS
]
...
if keyed:
    # 議事録の構造(進行メタ節)を伴わない新書式行は、自由記述本文へ混ぜられた
    # 1行と区別できない。``transcript_markdown`` は常にメタ節を書く。
    return keyed if MINUTE_META_HEADING in body_md else []
```

`MINUTE_META_HEADING = "## 進行メタ(発言者の選定経路)"` の完全一致で、議事録が `transcript_markdown` によって書かれたものかを判定する。この判定は、`MINUTE_META_HEADING` が本文に1文字でも違う形で書かれていた場合(例: Markdown レンダラによる整形・手書きの修正)に `[]`(判定不能 → NULL → fail-closed)を返す。

**問題**: fail-closed に倒れているため統制上の危険はない。しかし、`transcript_markdown` が将来マイナーチェンジ(見出しレベルの変更・文言の微調整)を受けた場合、過去の議事録の判定が一斉に「判定不能」に変わる可能性がある。

**推奨是正**:

`MINUTE_META_HEADING` を `transcript_markdown` と `parse_speaker_sequence` の両方で参照する定数として共有する現状の設計は妥当。見出しの変更時は両方を同時に更新する規律をテストで担保する(例: `test_boardroom.py` で `transcript_markdown` の出力を `parse_speaker_sequence` に通して往復復元を検証する)。

---

### A-12-21: `resolve_deemed_view` が DB 照合例外を `except Exception` で握りつぶす

**重大度**: [中]

**根拠**:

`src/ryza/governance/notices.py` `resolve_deemed_view` 関数:

```python
def resolve_deemed_view(conn: psycopg.Connection, embed: dict[str, Any]) -> DeemedViewTarget:
    """...
    照合できない場合は ``ref=None`` と警告文を返す — **fail-closed**。...
    """
    ref = parse_deemed_notice(embed)
    if ref is None:
        return DeemedViewTarget(None)
    try:
        row = decisions_mod.current_decision(conn, ref)
    except Exception as exc:  # noqa: BLE001
        return DeemedViewTarget(None, f"deemed 決定の照合に失敗したため否認ボタンを付けない: {exc}")
```

DB 照合で発生した例外(接続断・タイムアウト・SQL エラー等)を全て `except Exception` で捕捉し、`DeemedViewTarget(None, 警告文)` を返す。docstring はこれを「fail-closed」と説明する。

**問題**: fail-closed の設計(否認ボタンを出さない)は妥当であるが、例外の種類を区別せず全て「ボタンを出さない」に倒すため、DB 障害中は**全てのみなし承認通知で否認ボタンが消える**。代表は `/veto` コマンドで否認できるが、UI 上のボタンが消えることで「否認できない」と誤認されるリスクがある。

警告文は `DeemedViewTarget.warning` に格納されるが、この warning が UI(dashboard/app.py)でどのように表示されるかは提示されたファイルからは確認できない。warning が単にログに記録されるだけで UI に表示されない場合、代表はボタンが消えた理由を知覚できない。

**推奨是正**:

① DB 障害時は `DeemedViewTarget(None, 警告文)` ではなく、UI に「DB 障害中・ボタンは一時的に利用不可・`/veto` で否認可能」という明示的なメッセージを返す。② `except Exception` を `psycopg.OperationalError` 等、より狭い例外クラスに絞り、予期せぬ例外(プログラミングエラー等)は再送出する。

---

## 検査したが所見なしの領域

### ④ 意見書 front matter 処理

意見書の処理は `review_ref` 文字列を中心とした設計で、front matter の構文解析に依存しない。`missing_review_ref_warning` の実在検査はリポジトリ内パスに限定し、URL を除外する設計が妥当(遡及登録を塞がない)。迂回口なし。

### A-18 の判定ロジック(`has_findings`)の自己参照性

`has_findings` は `result` dict のキーを参照する純粋関数であり、外部モジュールに依存しない。実装修飾(`org`・`bot`)は `build_alert_embed` 経由のみで、判定結果を反転させない。自己参照なし。

### 承認トレーラの複数行併記の処理

1コミットに複数の `Approved:` トレーラがある場合、`trailer_approves` → `verify_decision_refs` は「1つでも有効な承認があれば受理」する。否認済みの参照を併記している場合は `problems` に残し、`vetoed_trailer_findings` で報告する。設計妥当。

### `_compile_keywords` の語彙分離(ASCII / 日本語)

ASCII 語は語境界(`(?<![0-9a-z])...(?![0-9a-z])`)で、日本語語は部分一致で検出する。`tips` が `ips` に誤一致する問題(C-1)は語境界で封じられている。設計妥当。

### 受容の承継(`supersedes`)の正当性検査

`_supersede_is_legitimate` は「同一コミット・旧エントリが自分より前に存在・パス集合が真に拡張・理由あり」の4条件全てを要求する。縮小・入替・理由なしの差し替えは「受容の隠蔽と区別できない」として却下する。fail-safe の設計が妥当。

---

## 総括

21件の所見を挙げた。うち**重大度[重要]は4件**(A-12-01・A-12-02・A-12-03・A-12-15)、**[中]は6件**、**[軽微]は8件**、所見なしは3領域である。

最も重要な所見は以下の3点である:

1. **A-12-15**(`ops/reminders.yaml` 未保護): 統制の発火タイミングを定義するファイルが保護領域外にあり、無承認で変更できる。これは保護領域制度自体の枠組みを薄弱化させる経路である。

2. **A-12-03**(`minutes.body_md` の追記オンリーが未確認): 議事録の証憑性が DB 制約ではなく実装慣習に依存している可能性がある。確認不能な場合は即時の検査を要する。

3. **A-12-02**(A-18-7 の PR 番号が `verified_pr_merge` を経由しない): A-18-1 が封じたはずの件名偽装の経路が A-18-7 では開いたままになっている。

実装全体の統制設計は極めて高い水準にある。各検査が「実装から独立した参照」を持つように保護領域を積層する設計、fail-safe / fail-closed の原則の一貫適用、申告の限界を docstring で毎回開示する規律は、実務の監査基準に照らしても劣らない。上記の所見は、この強固な土台の上に残る隙間の指摘である。