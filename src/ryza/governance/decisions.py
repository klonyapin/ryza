"""承認記録の writer(みなし承認・事後否認)— 定款 v0.4 第3条。

``src/ryza/bot/approvals.py`` が扱うのは**代表がボタンを押した明示の決定**
(approve/reject/question)である。本モジュールはその対になる2つの記録を扱う:

- :func:`record_deemed_approval` … みなし承認(``decision='deemed'``)。``#承認`` への
  PR リンク付き通知と同時に発効する(第3条2号)。押下者は存在せず、``decided_by`` は
  ``'system:<source>'``(0019 の ``decisions_deemed_system_actor_check``)
- :func:`record_veto` / :func:`record_revert_completion` / :func:`record_veto_withdrawal`
  … 代表による事後否認と、その取消完了報告・撤回(``governance.decision_vetoes`` — 0021)。
  「代表はいつでも否認できる」(第3条2号)の証跡

**なぜ writer をここに集めるか**: 0019 でスキーマは ``'deemed'`` を許容したが、
実際にその行を書くコードが無く、第3条3号の記録要件(「みなし承認も
governance.decisions に deemed として記録し、監査対象とする」)は未充足だった
(独立役員審査 0019 C-3)。承認・否認は監査 A-18-1 の ``Approved:`` トレーラ突合の
参照先そのものなので、書き込みの形式(decided_by の表記・3専決の除外・冪等性)を
散らさず1箇所に閉じる。

**二重の検証**: 3専決事項(定款第3条)への ``'deemed'`` はスキーマの CHECK が拒否するが、
本モジュールでも INSERT 前に弾く。理由は2つ — (1) CheckViolation はどの制約に触れたか
呼び出し側に伝わりにくく、運用時に「なぜ通知が失敗したか」の切り分けが遅れる、
(2) CheckViolation はトランザクションを中断させるため、通知と同一トランザクションで
記録する設計(第3条: 通知を欠いた発効は A-18 の無承認変更)では通知側の書込も巻き添えに
なる。**スキーマ側の CHECK が一次統制であり、本モジュールの検証はその代替ではない**
(アプリ検証だけに寄せると、別経路の INSERT で穴が開く)。

呼び出し側でトランザクションを制御する(本モジュールは commit しない)。

**CLI**(みなし承認の発効通知を機械化する経路 — ops/reminders.yaml
``governance-deemed-notice-wiring``)::

    python -m ryza.governance.decisions --deemed \\
        --proposal-ref https://github.com/klonyapin/ryza/pull/99 \\
        --kind pr --notice "保護領域 X の変更。独立役員審査は ... で完了"

``#承認`` への通知投入と ``deemed`` 行の記録を1トランザクションで行う
(実装は :mod:`ryza.governance.notices`)。手で ``#承認`` に投稿してから記録を忘れる、
という「通知なき発効/記録なき通知」を経路として消すのが目的である。

PR 番号だけで発効させる簡易形(参照・種別・文面を ``gh api`` の結果で埋める)::

    python -m ryza.governance.decisions --deemed-for-pr 99 \\
        --review docs/reviews/xxxx-independent-review.md

意見書がマージ前の PR ブランチ(worktree)にしか無い場合は ``--repo-root`` を指定する
(Issue #132)。CLI は ``__file__`` 起点で ``git rev-parse --show-toplevel`` するため、
省略時は**メイン checkout**を意見書探索の起点にする。開発フローでは意見書はマージ前の PR
ブランチにしか存在しないため、指定なしでは「参照が見つからない」となり ``reviewed_sha`` が
PR head SHA へフォールバックする(A-18-8 の sha_conflict 恒久ノイズ)。``--repo-root`` は
その worktree を明示するオプションで、指定時は ``PATH/config/governance.yaml`` の実在を
確認して fail-closed で検証する。決定の ``note`` に ``repo_root=<絶対パス>`` を残すため、
どの checkout の意見書を読んだかが事後に追える::

    python -m ryza.governance.decisions --deemed-for-pr 99 \\
        --review docs/reviews/xxxx-independent-review.md \\
        --repo-root /path/to/pr-worktree

``--review``(独立役員審査の参照)は ``--deemed-for-pr`` と ``--kind pr`` で必須である。
値は通知本文に残るだけでなく ``governance.decisions.review_ref`` に構造化して記録し、
``--deemed-for-pr`` では PR の head SHA を ``reviewed_sha`` として自動で埋める(0029)。
これにより監査 A-18-8 が「トレーラの ``reviewed=<sha>``」と「承認記録の ``reviewed_sha``」を
突合できる —— **別経路で書かれた2つの申告**なので、片方だけを書き換えた偽装は不一致で出る。

**審査記録からの採用(2026-08-04・reminder ``reviewed-sha-from-review-agent``)**: ``--review``
がリポジトリ内の意見書を指し、そのファイルが front matter(:mod:`ryza.reviews`)で
``reviewed_sha`` を宣言している場合、``reviewed_sha`` は**審査側の記録を採る**。起票者が
``--reviewed-sha`` で別の値を渡していれば **発効を止める**(fail-safe)—— 起票者の申告と
審査側の記録が食い違う発効は、どちらが正しいにせよ人が確認すべき事象であり、片方を黙って
採ると「どちらの値で発効したのか」が事後に判別できなくなる。front matter を持たない旧様式の
意見書では従来どおり(起票者の申告 / PR の head SHA)に落ちる。

**この検査を writer(:func:`record_deemed_approval`)ではなく CLI に置く理由**: 意見書は
リポジトリ内のファイルであり、読めるのは作業ツリーを持つ実行だけである。writer は Bot・
ジョブなど**チェックアウトを前提できない経路**からも呼ばれるため、そこで意見書の実在を
発効条件にすると、リポジトリを持たない正当な経路が一律に落ちる(fail-closed の副作用が
統制の意図を超える)。事後の観測は監査側(A-18-8 の ``from_review_artifact``)が担う。

**発効を中止する条件**(いずれも DB へ何も書かない・exit 1。独立役員審査 2026-08-04):

- 起票者の ``--reviewed-sha`` が審査記録と食い違う(:class:`ReviewedShaConflictError`)
- 審査記録の判定が ``reject`` / ``request_changes``(:class:`ReviewVerdictBlocksError` — C-1。
  否認された審査を「独立役員審査」として ``#承認`` に掲示させない。強行経路は無い)
- ``--kind pr`` で審査参照がリポジトリ内に実在しない(:class:`MissingReviewArtifactError`
  — C-2(c)。リポジトリ外の審査・遡及登録は ``--review-missing-ok`` の明示で通し、
  その事実を ``meta.runs`` に残す)
- 参照がリポジトリ外へ出る・symlink 経由・front matter が壊れている
  (:class:`ryza.reviews.ReviewArtifactError` — C-2/C-6/C-7)

**残る限界**: 意見書はリポジトリ内の平文であり審査エージェントの署名は無い。起票者が
front matter を書き換える・消す・front matter の無いファイルを ``--review`` に指す経路は
残る(いずれも意見書そのものの改変であり diff に残る)。本配線が足すのは「食い違えば
止まる」ことと、A-18-8 が**審査記録に由来する ``reviewed_sha`` の割合**を毎週開示すること
(由来のない申告が緑に埋もれない)である。

**この CLI を叩き忘れると通知なき発効になる**。自動起票(PR イベント駆動)は未実装で
(ops/reminders.yaml ``deemed-auto-announce``)、叩き忘れは監査 A-18-7(保護領域 PR の
承認記録漏れ)が週次で事後検出する —— 簡易形はその頻度を下げるための入口側の手当てである。
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from ryza.bot.approvals import KINDS, NotOwnerError, is_owner

log = logging.getLogger("ryza.governance.decisions")

# 定款第3条の3専決事項(config/governance.yaml の representative_reserved)と
# governance.decisions.kind の対応。0019 の decisions_deemed_not_reserved_check と
# **同じ集合でなければならない**(tests/governance/test_governance_schema.py の
# test_reserved_matters_cover_governance_yaml が governance.yaml との乖離を検出する)。
# kind='constitution' は現 kind 語彙(0012)には未登録 — 将来追加時に穴が開かないよう
# 0019 が先回りで列挙した値をここでも保持する。
RESERVED_KIND_BY_MATTER: dict[str, str] = {
    "constitution_amendment": "constitution",  # 定款の制定・改廃
    "live_money": "budget",                    # 出資・増資・実弾投入・実弾移行
    "kill_switch_resume": "breaker_resume",    # Kill Switch からの復帰
}

# みなし承認を付けられない kind(明示承認のみで発効する)。
RESERVED_KINDS: frozenset[str] = frozenset(RESERVED_KIND_BY_MATTER.values())

# みなし承認の decided_by 接頭辞(0019 の CHECK: decision <> 'deemed' OR
# decided_by LIKE 'system:%')。0012 の killswitch_events.actor と同じ表記法。
SYSTEM_ACTOR_PREFIX = "system:"

# 既定の発効源。「#承認 への通知により自動発効した」ことを表す。
DEFAULT_DEEMED_SOURCE = "deemed"

# governance.decision_vetoes.kind の語彙(0021 の CHECK と一致させる)。
VETO_KINDS: tuple[str, ...] = ("veto", "revert_complete", "withdrawal")

# governance.decision_vetoes.origin の語彙(0030 の CHECK と一致させる)。
#   discord_button  … #承認 の否認ボタン(+理由モーダル)
#   discord_command … /veto ・ /unveto
#   cli             … 人手のスクリプト・保守作業からの直接呼び出し
#   job             … 自動ジョブ内からの記録
# **既定値を持たせない**(writer は origin を必須の引数で受ける)。既定を置くと、出所を
# 渡し忘れた新しい経路が黙って既定値として記録され、経路の一次識別という列の目的が
# エラーも警告も無いまま失われる。run_id では代替できない —— ボタン経路と /veto は同じ
# job_name で Run を開くため、meta.runs を辿っても両者は区別できない(0030 / 審査 重要-5)。
VETO_ORIGINS: tuple[str, ...] = ("discord_button", "discord_command", "cli", "job")

# 審査対象 SHA の様式(0029 の decisions_reviewed_sha_check と一致させる)。短縮 SHA を
# 許すと A-18-8 の突合が「一致とも不一致とも言えない」状態を作るため完全 SHA のみ。
# 監査側(audit/a18._FULL_SHA_RE)と同じ様式だが、audit は被監査モジュールを import しない
# 方針なので定数は共有せず各々が持つ。
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)

# proposal_ref の様式(F-10 / A-12-13 / A-12-04)。書き込み時のみ検証し、既存 DB 行には
# 触れない(migration による遡及書き換えは追記オンリー原則の逸脱にあたる)。目的は
# 「短い任意文字列が 0007 の UNIQUE(proposal_ref) に偶然一致して重複判定を誤らせる」
# 経路を塞ぐこと。3形式:
#   (a) 本リポジトリ規則で使う PR URL — `https://github.com/<owner>/<repo>/pull/<数字>`
#   (b) `decision:<数字>` — 決定 id の直接参照(否認・撤回の起点として `record_veto`
#       などが `decision_id` を保持しているので、対応する ref を機械生成できる)
#   (c) `manual:<スラッグ>` — 上記いずれでもない遡及登録・手作業(戦略昇格の非 PR 経路など)。
#       スラッグは短すぎる衝突を防ぐため下限を持つ(先頭 1 文字 + 残り 2〜63 文字 = 計 3〜64)
# **なぜ writer 側で弾くか**: proposal_ref は UNIQUE キーそのもので、書き込みの様式が
# 揃わないと「二重記録の防止」が偶然一致の可否に依存する。CLI 側で弾く方式は、Bot 経路や
# 別スクリプトからの直接呼び出しに穴が残る(A-18-1 が事後検出することになるが、統制の
# 一次責任は writer に置くのが本仕様の原則 — decisions.py モジュール冒頭 §二重の検証)。
_PROPOSAL_REF_PR_URL_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*/pull/[1-9][0-9]*$"
)
_PROPOSAL_REF_DECISION_RE = re.compile(r"^decision:[1-9][0-9]*$")
_PROPOSAL_REF_MANUAL_RE = re.compile(r"^manual:[a-z0-9][a-z0-9\-_]{2,63}$")


def validate_proposal_ref(value: str) -> str:
    """``proposal_ref`` の様式を検証する純粋関数(F-10)。

    許可される 3 形式:

    - ``https://github.com/<owner>/<repo>/pull/<数字>`` — GitHub の PR URL
    - ``decision:<数字>`` — 決定 id の直接参照
    - ``manual:<スラッグ>`` — スラッグは ``[a-z0-9]`` で始まり、続く 2〜63 文字は
      ``[a-z0-9\\-_]``(計 3〜64 文字)

    いずれにも合致しない場合は ``ValueError`` を送出する(空文字・空白は
    :func:`_require_text` が先に弾く想定だが、単体でも使えるよう再検査する)。

    設計判断: **正規表現を writer に閉じる**。呼び出し側で複雑な検証を書かせると、
    経路が増えるたびに解釈が分岐して同じ「短い任意文字列で UNIQUE を素通り」を再現しうる。
    """
    if not isinstance(value, str):
        raise ValueError(f"proposal_ref は文字列である必要がある: {type(value).__name__}")
    if not value or value != value.strip():
        raise ValueError(
            "proposal_ref は前後空白を持たない非空文字列で、以下 3 形式のいずれか: "
            "`https://github.com/<owner>/<repo>/pull/<数字>` / "
            "`decision:<数字>` / `manual:<スラッグ>`"
        )
    if _PROPOSAL_REF_PR_URL_RE.match(value):
        return value
    if _PROPOSAL_REF_DECISION_RE.match(value):
        return value
    if _PROPOSAL_REF_MANUAL_RE.match(value):
        return value
    raise ValueError(
        f"proposal_ref='{value}' は許可された様式に一致しない。"
        "許可: `https://github.com/<owner>/<repo>/pull/<数字>` / "
        "`decision:<数字>` / `manual:<[a-z0-9][a-z0-9\\-_]{2,63}>`。"
        "旧来の短い任意文字列は 0007 の UNIQUE(proposal_ref) の重複判定と衝突するため不可"
    )

# 否認できる決定(0021 の check_veto_target トリガと一致させる)。
# reject / question を否認可能にすると「却下されている」という阻止の根拠が消え、
# 現決定を読む将来の判定が fail-open で外れる(独立役員審査 0021 C-2)。
VETOABLE_DECISIONS: frozenset[str] = frozenset({"approve", "deemed"})


class ReservedMatterError(ValueError):
    """3専決事項(定款第3条)にみなし承認を付けようとした。

    定款第3条: 定款の制定・改廃 / 実弾マネー / Kill Switch 復帰 は代表の明示承認が
    必須であり、通知による自動発効の対象外。
    """


class DuplicateDecisionError(ValueError):
    """同一 ``proposal_ref`` の決定が既に記録されている(0007 の UNIQUE)。

    1提案=1決定。二重通知・リトライで承認記録が重複しないための冪等制約であり、
    「既に決まっている提案をもう一度決め直す」ことはできない(決定の変更は
    :func:`record_veto` による否認 + 新提案で表現する)。
    """


class NotVetoableError(ValueError):
    """否認できない決定(却下・質問)を否認しようとした。

    否認は「発効している決定を止める」操作であり、却下・質問には適用できない。
    却下を否認可能にすると、現決定を読んで発効を止める判定が fail-open で外れる。
    """


class ReviewedShaConflictError(ValueError):
    """起票者の ``--reviewed-sha`` が審査記録(意見書の front matter)と食い違う。

    **なぜ発効を止めるか**(fail-safe): 0029 + A-18-8 の突合は「起票者が書いた2つの値」の
    比較でしかなく、同じ嘘を両方に書けば通る。審査側が独立に書いた ``reviewed_sha`` が
    手元にある場面は、その申告性を初めて外から検証できる唯一の地点である。ここで起票者の
    値を黙って採る(または審査側の値へ黙って寄せる)と、食い違いの事実がどこにも残らない。
    """


class ReviewVerdictBlocksError(ValueError):
    """審査記録の ``verdict`` が発効を許さない値(``reject`` / ``request_changes``)。

    **なぜ中止か**(独立役員審査 2026-08-04 C-1): 旧実装は判定を読んだうえで捨て、通知に
    「独立役員審査: <その意見書>」を掲示して発効していた。これは**本実装が新たに作った
    偽の保証**である —— 変更前はシステムが意見書の中身を読んでいなかったので、否認を見て
    無視するという事態自体が存在しなかった。48h 異議期間に代表が見る唯一の成果物が通知で
    ある以上、否認された審査を裏付けとして掲示するのは定款第3条・第5条が定める「審査を
    前置する」手続の逆転にあたる。**強行フラグは作らない** —— 是正して意見書を更新するのが
    正規の経路であり、抜け道を用意すればこの検査は儀式になる。
    """


class MissingReviewArtifactError(ValueError):
    """``--kind pr`` の発効で審査参照がリポジトリ内に実在しない(審査 C-2(c))。

    警告のままにすると ``--review docs/reviews/存在しない.md`` が審査の代用になる。
    リポジトリ外の審査・遡及登録は ``--review-missing-ok`` の明示で通し、その事実を
    ``meta.runs`` に残す。
    """


class ProposalRefMismatchError(ValueError):
    """``expected_proposal_ref`` が対象決定の ``proposal_ref`` と一致しない。

    否認は代表の手操作であり、``decision_id`` の取り違えは無関係な承認を恒久的に
    「否認済み」に汚染する。呼び出し側の意図を DB と突合してから書く。
    """


@dataclass(frozen=True)
class DeemedApproval:
    """記録されたみなし承認。"""

    id: int
    proposal_ref: str
    kind: str
    decided_by: str  # 'system:<source>'
    notice_ref: str
    #: 審査対象コミット(40 桁 hex・小文字)。申告が無ければ None(0029)
    reviewed_sha: str | None = None
    #: 独立役員審査の参照(docs/reviews/... のパス・URL 等)
    review_ref: str | None = None


@dataclass(frozen=True)
class Veto:
    """記録された否認系の1行(veto / revert_complete / withdrawal)。"""

    veto_id: int
    decision_id: int
    kind: str
    vetoed_by: str
    reason: str
    revert_commit: str | None
    derived_effects_ref: str | None
    #: 記録経路(:data:`VETO_ORIGINS` の1つ。0030)
    origin: str


def _require_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} は必須(空文字不可)")
    return value


def record_deemed_approval(
    conn: psycopg.Connection,
    proposal_ref: str,
    kind: str,
    notice_ref: str,
    *,
    source: str = DEFAULT_DEEMED_SOURCE,
    note: str | None = None,
    reviewed_sha: str | None = None,
    review_ref: str | None = None,
) -> DeemedApproval:
    """みなし承認を ``governance.decisions`` に ``decision='deemed'`` で記録する。

    Args:
        proposal_ref: 提案の一意参照(PR URL 等)。UNIQUE で二重記録を防ぐ
        kind: 提案種別(``approvals.KINDS``)。3専決の kind は拒否する
        notice_ref: ``#承認`` へ投稿した通知の参照(メッセージ ID / URL)。
            **必須** — 定款第3条は通知を発効要件とし、通知を欠いた発効は
            A-18 の無承認変更にあたる。``channel_msg_id`` 列に記録する
        source: 発効源。``decided_by`` は ``'system:<source>'`` になる
        note: 補足(任意)
        reviewed_sha: 審査対象コミットの完全 SHA(0029)。小文字へ正規化して記録する。
            監査 A-18-8 が ``Approved:`` トレーラの ``reviewed=<sha40>`` と突合する
        review_ref: 独立役員審査の参照(``docs/reviews/...`` のパス・URL 等)

    Raises:
        ValueError: 未知の kind、proposal_ref / notice_ref / source が空、
            または ``reviewed_sha`` が 40 桁 hex の完全 SHA でない
        ReservedMatterError: 3専決事項の kind(定款第3条)
        DuplicateDecisionError: 同 proposal_ref の決定が既にある

    本関数は **通知の送信そのものは行わない**。呼び出し側が通知の投入(press.outbox)と
    本記録を同一トランザクションに置くことで、「通知されたが記録が無い」「記録は
    あるが通知されていない」のどちらも起こらないようにする(定款第3条3号)。

    ``reviewed_sha`` / ``review_ref`` は**任意**である。必須にしないのは、みなし承認が
    PR 以外(戦略昇格・IPS 改訂等)にも使われ、独立役員審査が前置されない手続では
    書きようがないため —— 必須化すると正当な発効経路を塞ぐ(後続配線審査 後-1 と同じ理由)。
    保護領域 PR で必須にする判断は CLI 側(``REVIEW_REQUIRED_KINDS``)に置く。
    """
    _require_text(proposal_ref, "proposal_ref")
    validate_proposal_ref(proposal_ref)
    _require_text(notice_ref, "notice_ref")
    _require_text(source, "source")
    reviewed_sha = normalize_reviewed_sha(reviewed_sha)
    if review_ref is not None:
        _require_text(review_ref, "review_ref")
    # 専決事項の判定を語彙検査より先に行う。RESERVED_KIND_BY_MATTER には現 kind 語彙に
    # 未登録の 'constitution'(0019 が先回りで列挙)が含まれるため、順序を逆にすると
    # 「未知の提案種別」という的外れなエラーになり、憲法的な禁止であることが伝わらない。
    if kind in RESERVED_KINDS:
        matters = [m for m, k in RESERVED_KIND_BY_MATTER.items() if k == kind]
        raise ReservedMatterError(
            f"kind='{kind}' は定款第3条の専決事項({'/'.join(matters)})であり、"
            "みなし承認では発効しない。代表の明示承認(approvals.record_decision)を使う"
        )
    if kind not in KINDS:
        raise ValueError(f"未知の提案種別: {kind}")

    decided_by = f"{SYSTEM_ACTOR_PREFIX}{source}"
    _raise_if_decided(conn, proposal_ref)
    try:
        # ネストした transaction() は SAVEPOINT になる。競合で UNIQUE に触れても
        # 外側のトランザクション(通知の書込)を巻き添えで中断させない。
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO governance.decisions
                    (proposal_ref, kind, decision, decided_by, note, channel_msg_id,
                     reviewed_sha, review_ref)
                VALUES (%s, %s, 'deemed', %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    proposal_ref, kind, decided_by, note, notice_ref,
                    reviewed_sha, review_ref,
                ),
            )
            decision_id = cur.fetchone()[0]
    except psycopg.errors.UniqueViolation as exc:  # 事前検査との競合(別セッション)
        raise DuplicateDecisionError(
            f"proposal_ref='{proposal_ref}' の決定は既に記録されている(1提案=1決定)"
        ) from exc
    return DeemedApproval(
        id=decision_id,
        proposal_ref=proposal_ref,
        kind=kind,
        decided_by=decided_by,
        notice_ref=notice_ref,
        reviewed_sha=reviewed_sha,
        review_ref=review_ref,
    )


def normalize_reviewed_sha(value: str | None) -> str | None:
    """審査対象 SHA を検証して小文字へ正規化する(空・None は None)。

    正規化を writer 側で行うのは、大文字と小文字の表記揺れが監査 A-18-8 の**不一致の
    誤検出**になるためである(トレーラ側 :func:`ryza.audit.a18.reviewed_shas` も lower で
    揃える)。短縮 SHA を弾くのは、曖昧な参照が「一致とも不一致とも言えない」第三の状態を
    作り、fail-safe / fail-open のどちらに倒すかの判断を突合ロジックに押し込むため。
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if not _FULL_SHA_RE.match(text):
        raise ValueError(
            f"reviewed_sha は 40 桁 hex の完全 SHA である必要がある(短縮 SHA は曖昧): {value}"
        )
    return text.lower()


def missing_review_ref_warning(
    review_ref: str | None, *, repo_root: Path | None = None
) -> str | None:
    """``review_ref`` がリポジトリ内パス形式なのに実在しないなら警告文を返す。

    **これは警告であって可否判定ではない**。``--kind pr`` の発効では
    :func:`require_existing_review` が同じ事実を**中止**として扱う(審査 C-2(c))が、
    それ以外の kind(戦略昇格・IPS 改訂など独立役員審査が前置されない手続)と
    ``--review-missing-ok`` を付けた遡及登録では、従来どおり警告にとどめる。

    参照の解決は :func:`ryza.reviews.resolve_review_path` に委ねる。旧実装は
    ``(root / ref).exists()`` で判定していたため、``docs/reviews/../reviews/x.md`` のような
    脱出表記を**実在と見なして無警告**にする一方、採用側 (:func:`resolve_reviewed_sha`) は
    同じ参照を「審査記録なし」に落としていた —— 判定が二重定義で食い違い、迂回がどの層からも
    見えなかった(審査 C-2)。両者を同じ解決関数に揃える。

    **リポジトリルートが決められない実行では検査そのものを行わない**(独立役員審査 SHA-6):
    ``__file__`` からの相対位置はソースチェックアウト前提であり、パッケージとして設置された
    実行では site-packages を指して**全参照が誤警告**になる。git 作業ツリーの外なら検査を
    諦めて ``None`` を返す —— 誤警告は「警告が出ていても実在する」学習を生み、警告そのものを
    無意味にするので、検査できないときは黙るほうが安全である。
    """
    path = _review_path(review_ref, repo_root)
    if path is None or path.is_file():
        return None
    return (
        f"--review の参照 '{review_ref.strip()}' がリポジトリ内に見つからない"
        "(パス形式に見えるが実在しない — 審査意見書の所在を確認すること)"
    )


def _review_path(review_ref: str | None, repo_root: Path | None) -> Path | None:
    """``--review`` の解決結果(リポジトリ外参照・ルート不明なら ``None``)。

    :class:`ryza.reviews.ReviewArtifactError`(脱出表記・symlink・絶対パス)はそのまま
    送出する —— 呼び出し側は中止する。
    """
    from ryza.reviews import resolve_review_path

    if not review_ref:
        return None
    return resolve_review_path(review_ref, repo_root=repo_root or _repo_root())


def require_existing_review(
    review_ref: str | None,
    kind: str | None,
    *,
    missing_ok: bool = False,
    repo_root: Path | None = None,
) -> None:
    """``--kind pr`` で審査参照が実在しないなら発効を中止する(審査 C-2(c))。

    **なぜ pr だけ中止か**: 保護領域 PR のみなし承認は独立役員審査を前置する手続であり
    (定款第5条・07-development §3-1)、意見書はリポジトリ内 ``docs/reviews`` に保存する
    義務がある。その手続で「パス形式に見えるが実在しない参照」を警告のまま通すと、
    ``--review docs/reviews/存在しない.md`` が審査の代用になる —— 警告は stderr に流れて
    消えるので、実質的に無検査と変わらない。

    **遡late な登録の口は塞がない**: リポジトリ外(Discord スレッド・Issue)の審査や、
    別ブランチにしか無い意見書を指す運用は ``--review-missing-ok`` で明示する。明示は
    ``meta.runs`` の params に残るため、「どの発効が実在検査を外したか」が事後に数えられる
    (黙って通すのとは統制上まったく違う)。

    **worktree がローカルにあるなら ``--repo-root`` が正道**(Issue #132): PR ブランチが
    ローカル worktree として checkout されている場合、``--review-missing-ok`` で実在検査を
    外すよりも、CLI に ``--repo-root <その worktree>`` を渡すほうが安全である。実在検査を
    外さないまま「マージ前の PR ブランチにしか無い意見書」を正しく読めるため、``sha_conflict``
    の恒久ノイズも消える。``--review-missing-ok`` は「worktree すら無い遡及登録」の逃げ道。

    Raises:
        ReviewArtifactError: 参照がリポジトリ外へ出る・symlink・絶対パス
        MissingReviewArtifactError: ``kind`` が pr で参照が実在せず ``missing_ok`` でない
    """
    if kind not in REVIEW_REQUIRED_KINDS or missing_ok:
        return
    path = _review_path(review_ref, repo_root)
    if path is None or path.is_file():
        return
    raise MissingReviewArtifactError(
        f"--review の参照 '{str(review_ref).strip()}' がリポジトリ内に実在しない。発効を中止した"
        "(--kind pr は独立役員審査を前置する手続 — 意見書を docs/reviews に保存するか、"
        "リポジトリ外の審査なら --review-missing-ok を明示すること)"
    )


#: ``reviewed_sha`` の由来ラベル(run params と CLI 出力で開示する)。
#: 「どこから来た値か」を記録しないと、A-18-8 の一致が審査記録の裏付けを持つのか
#: 起票者の申告どうしの一致なのかを事後に区別できない。
SHA_SOURCE_ARTIFACT = "review_artifact"  # 意見書の front matter(審査側の記録)
SHA_SOURCE_ARGUMENT = "argument"         # --reviewed-sha(起票者の申告)
SHA_SOURCE_PR_HEAD = "pr_head"           # gh api の PR head SHA(起票者側の自動取得)


@dataclass(frozen=True)
class ReviewedShaChoice:
    """``reviewed_sha`` に何を採用したか(値・由来・開示すべき注記)。"""

    sha: str | None
    source: str | None
    notes: tuple[str, ...] = ()


def resolve_reviewed_sha(
    review_ref: str | None,
    declared: str | None,
    *,
    fallback: str | None = None,
    fallback_source: str = SHA_SOURCE_PR_HEAD,
    repo_root: Path | None = None,
) -> ReviewedShaChoice:
    """審査記録・起票者の申告・PR head の3経路から ``reviewed_sha`` を決める。

    優先順位は **審査記録 > 起票者の申告 > PR の head SHA**。審査側を最優先にするのは、
    これが唯一「発効を起票した側とは別の主体が書いた」値だからである(reminders
    ``reviewed-sha-from-review-agent``)。

    Args:
        review_ref: ``--review`` の値。リポジトリ内パス形式のときだけ意見書を読む
        declared: ``--reviewed-sha``(起票者の明示指定)
        fallback: 明示指定も審査記録も無いときの既定(``--deemed-for-pr`` の head SHA)
        fallback_source: ``fallback`` を採ったときの由来ラベル
        repo_root: 意見書を探すルート。省略時は :func:`_repo_root`

    Raises:
        ReviewedShaConflictError: ``declared`` と審査記録が食い違う(発効を止める)
        ReviewVerdictBlocksError: 審査記録の判定が ``reject`` / ``request_changes``(C-1)
        ryza.reviews.ReviewArtifactError: front matter が壊れている・参照がリポジトリ外へ
            出る・symlink 経由(様式不備や表記を「旧様式」「審査記録なし」に読み替えない —
            壊すこと・書式を変えることが回避策にならないようにする)
        ValueError: SHA の様式不備(:func:`normalize_reviewed_sha`)

    **``fallback`` との食い違いは止めない**(注記のみ)。``--deemed-for-pr`` の head SHA は
    起票者の「申告」ではなく発効時点のブランチ先端であり、意見書のコミット自身が head を
    進めるため、審査対象 SHA と head はむしろ**通常一致しない**。ここを致命にすると、
    front matter を書いた PR が軒並み発効できなくなる。
    """
    from ryza.reviews import BLOCKING_VERDICTS, VERDICTS, load_review_artifact

    declared_sha = normalize_reviewed_sha(declared)
    fallback_sha = normalize_reviewed_sha(fallback)
    artifact = load_review_artifact(review_ref, repo_root=repo_root or _repo_root())
    notes: list[str] = []
    if artifact is not None:
        notes += [f"審査記録 {artifact.path}: {w}" for w in artifact.warnings]
        # C-1: 判定の検査は SHA の有無より先。reviewed_sha を書かない reject の意見書でも
        # 「否認された審査を裏付けとして掲示する」ことに変わりはない。
        if artifact.verdict in BLOCKING_VERDICTS:
            raise ReviewVerdictBlocksError(
                f"審査記録 {artifact.path} の判定は '{artifact.verdict}' であり発効できない。"
                "発効を中止した(是正のうえ意見書を更新すること — 強行経路は無い。"
                f"発効できる判定: {'/'.join(v for v in VERDICTS if v not in BLOCKING_VERDICTS)})"
            )
    if artifact is None or artifact.reviewed_sha is None:
        # 旧様式(front matter 無し)・reviewed_sha を書いていない front matter は
        # 0029 以前と同じ動作。**遡及改変しない**方針の裏返しであり、欠落を致命にすると
        # 「front matter ごと消せば通る」という逆インセンティブになる。
        if declared_sha:
            return ReviewedShaChoice(declared_sha, SHA_SOURCE_ARGUMENT, tuple(notes))
        return ReviewedShaChoice(
            fallback_sha, fallback_source if fallback_sha else None, tuple(notes)
        )

    sha = artifact.reviewed_sha
    if declared_sha and declared_sha != sha:
        raise ReviewedShaConflictError(
            f"--reviewed-sha={declared_sha[:12]} は審査記録 {artifact.path} の "
            f"reviewed_sha={sha[:12]} と一致しない。発効を中止した"
            "(審査側の記録が正 — どちらが実際の審査対象かを確認し、"
            "意見書を訂正するか --reviewed-sha を外して再実行すること)"
        )
    if fallback_sha and fallback_sha != sha:
        notes.append(
            f"reviewed_sha は審査記録 {artifact.path} の {sha[:12]} を採用した"
            f"(PR の head は {fallback_sha[:12]} — 審査後に積んだコミットは承継されない)"
        )
    if artifact.verdict:
        notes.append(f"審査記録の判定: {artifact.verdict}(発効の可否判断には使っていない)")
    return ReviewedShaChoice(sha, SHA_SOURCE_ARTIFACT, tuple(notes))


def _repo_root() -> Path | None:
    """リポジトリルート。``git rev-parse --show-toplevel`` を優先し、駄目なら ``__file__`` 相対。

    ソースチェックアウトなら両者は一致する。パッケージ設置時は git 情報が無く、``__file__``
    相対も無意味(site-packages を指す)なので ``None`` を返して検査を無効化する。
    """
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False, timeout=5,
            cwd=str(Path(__file__).resolve().parent),
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    fallback = Path(__file__).resolve().parents[3]
    # ソースチェックアウトの目印。無ければ「ルートを決められない」として検査しない。
    return fallback if (fallback / "config" / "governance.yaml").exists() else None


def _raise_if_decided(conn: psycopg.Connection, proposal_ref: str) -> None:
    """既に決定済みなら :class:`DuplicateDecisionError`。

    UNIQUE 違反を待たずに事前検査するのは、CheckViolation / UniqueViolation が
    呼び出し側のトランザクションを中断させ、同一トランザクションに置いた通知の
    書込まで巻き添えにするため。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, decision FROM governance.decisions WHERE proposal_ref = %s",
            (proposal_ref,),
        )
        row = cur.fetchone()
    if row is not None:
        raise DuplicateDecisionError(
            f"proposal_ref='{proposal_ref}' は既に decision='{row[1]}'(id={row[0]})"
            "として記録されている(1提案=1決定)。決定の撤回は record_veto で行う"
        )


def _append_veto_row(
    conn: psycopg.Connection,
    decision_id: int,
    kind: str,
    reason: str,
    *,
    vetoed_by: str,
    owner_ids: Iterable[str],
    expected_proposal_ref: str,
    origin: str,
    revert_commit: str | None = None,
    derived_effects_ref: str | None = None,
    run_id: int | None = None,
) -> Veto:
    """``governance.decision_vetoes`` へ1行追記する(否認系 writer の共通実装)。

    検証の順序は「安いものから、かつ破壊的でない順」:
    語彙 → 文字列必須 → オーナー検証 → 対象決定の実在 → ``proposal_ref`` 照合 → INSERT。
    """
    if kind not in VETO_KINDS:
        raise ValueError(f"未知の否認行種別: {kind}(既知: {', '.join(VETO_KINDS)})")
    # 語彙検査を writer 側にも置くのは、0030 の CHECK 違反がトランザクションを中断させ、
    # 呼び出し側の同一トランザクション内の書込(#運営 への通知投入)を巻き添えにするため
    # (kind / reserved kind の事前検査と同じ理由)。一次統制はあくまで DB 側の CHECK。
    if origin not in VETO_ORIGINS:
        raise ValueError(f"未知の否認の出所: {origin}(既知: {', '.join(VETO_ORIGINS)})")
    _require_text(reason, "reason")
    _require_text(vetoed_by, "vetoed_by")
    _require_text(expected_proposal_ref, "expected_proposal_ref")

    # 否認は代表の専権(定款第3条)。record_decision と同型のオーナー検証を課す。
    # DB 側では owner_ids を知り得ないため、この検証はアプリ層にしか置けない。
    if not is_owner(vetoed_by, owner_ids):
        raise NotOwnerError(f"非オーナーの否認操作を拒否: user={vetoed_by}")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT proposal_ref, decision FROM governance.decisions WHERE id = %s",
            (decision_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"否認対象の決定 id={decision_id} が存在しない")
    actual_ref, decision = row
    # decision_id の取り違えは、無関係な承認を恒久的に「否認済み」に汚染する
    # (0007 の UNIQUE(proposal_ref) により提案の再記録もできない — 審査 C-3)。
    # 呼び出し側が「どの提案を否認するつもりか」を宣言し、DB と突合する。
    if actual_ref != expected_proposal_ref:
        raise ProposalRefMismatchError(
            f"決定 id={decision_id} の proposal_ref は '{actual_ref}' であり、"
            f"指定された '{expected_proposal_ref}' と一致しない(否認対象の取り違え)"
        )
    # スキーマ側の check_veto_target トリガが一次統制。ここで先に弾くのは、
    # トリガの RaiseException が呼び出し側トランザクションを中断させるため。
    if decision not in VETOABLE_DECISIONS:
        raise NotVetoableError(
            f"決定 id={decision_id} は decision='{decision}' であり否認できない"
            f"(否認できるのは発効している決定 {'/'.join(sorted(VETOABLE_DECISIONS))} のみ)。"
            "却下・質問を否認可能にすると「却下されている」という阻止の根拠が消える"
        )

    # deemed 側と対称に SAVEPOINT で包む(審査 C-7)。制約違反が起きても
    # 呼び出し側の外側トランザクション(#運営 への報告投入など)を巻き添えにしない。
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO governance.decision_vetoes
                (decision_id, kind, vetoed_by, reason,
                 revert_commit, derived_effects_ref, run_id, origin)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING veto_id
            """,
            (
                decision_id, kind, vetoed_by, reason,
                revert_commit, derived_effects_ref, run_id, origin,
            ),
        )
        veto_id = cur.fetchone()[0]
    return Veto(
        veto_id=veto_id,
        decision_id=decision_id,
        kind=kind,
        vetoed_by=vetoed_by,
        reason=reason,
        revert_commit=revert_commit,
        derived_effects_ref=derived_effects_ref,
        origin=origin,
    )


def record_veto(
    conn: psycopg.Connection,
    decision_id: int,
    reason: str,
    *,
    vetoed_by: str,
    owner_ids: Iterable[str],
    expected_proposal_ref: str,
    origin: str,
    revert_commit: str | None = None,
    derived_effects_ref: str | None = None,
    run_id: int | None = None,
) -> Veto:
    """代表による事後否認(``kind='veto'``)を追記する(0021・定款第3条)。

    Args:
        decision_id: 否認対象 ``governance.decisions.id``
        reason: 否認理由(必須)。執行側が何を巻き戻すかの起点になる
        vetoed_by: 否認者の Discord ユーザー ID
        owner_ids: オーナー ID 集合。``vetoed_by`` がこれに含まれなければ拒否する
            (否認は代表の専権 — ``approvals.record_decision`` と同型の検証)
        expected_proposal_ref: 否認するつもりの提案参照。``decision_id`` の行と
            一致しなければ INSERT せずに失敗する(対象取り違えの防止)
        origin: 記録経路(:data:`VETO_ORIGINS`)。**必須**(既定値を置くと出所の
            渡し忘れが黙って既定値として記録され、列の目的が失われる — 0030)
        revert_commit: 取消コミット SHA。否認時点で未確定なら省略し、確定後に
            :func:`record_revert_completion` で追記する
        derived_effects_ref: 取消不能な派生効果一覧の参照(``#運営`` への報告)
        run_id: 記録したジョブ実行。否認は代表の作為でありジョブ生成物ではないため任意。
            **経路の識別には使えない** — ボタンと ``/veto`` は同じ job_name で Run を開く

    Raises:
        ValueError: 必須文字列が空、origin が語彙外、または decision_id が存在しない
        NotOwnerError: 非オーナーの否認操作
        ProposalRefMismatchError: expected_proposal_ref の不一致
        NotVetoableError: 対象が approve / deemed 以外(却下・質問は否認できない)
    """
    return _append_veto_row(
        conn, decision_id, "veto", reason,
        vetoed_by=vetoed_by, owner_ids=owner_ids,
        expected_proposal_ref=expected_proposal_ref, origin=origin,
        revert_commit=revert_commit, derived_effects_ref=derived_effects_ref,
        run_id=run_id,
    )


def record_revert_completion(
    conn: psycopg.Connection,
    decision_id: int,
    reason: str,
    *,
    vetoed_by: str,
    owner_ids: Iterable[str],
    expected_proposal_ref: str,
    origin: str,
    revert_commit: str | None = None,
    derived_effects_ref: str | None = None,
    run_id: int | None = None,
) -> Veto:
    """否認に伴う取消の完了報告(``kind='revert_complete'``)を追記する。

    定款第3条は否認された変更の「遅滞ない取消」と、取消不能な派生効果の
    ``#運営`` への報告を義務付ける。追記オンリーのため否認行を UPDATE できず、
    確定した ``revert_commit`` / 派生効果一覧は本関数で追記する。現決定 view は
    これらを**列単位**で解決するので、片方だけの追記がもう片方を消さない。

    ``origin`` は**この追記を書いた経路**であり、元の否認行の出所とは独立に記録する
    (ボタンで否認したものを CLI から取消報告する、という組み合わせが普通に起きる)。
    """
    return _append_veto_row(
        conn, decision_id, "revert_complete", reason,
        vetoed_by=vetoed_by, owner_ids=owner_ids,
        expected_proposal_ref=expected_proposal_ref, origin=origin,
        revert_commit=revert_commit, derived_effects_ref=derived_effects_ref,
        run_id=run_id,
    )


def record_veto_withdrawal(
    conn: psycopg.Connection,
    decision_id: int,
    reason: str,
    *,
    vetoed_by: str,
    owner_ids: Iterable[str],
    expected_proposal_ref: str,
    origin: str,
    run_id: int | None = None,
) -> Veto:
    """否認そのものの撤回(``kind='withdrawal'``)を追記する(審査 C-3)。

    否認は代表の手操作であり、``decision_id`` の取り違えで無関係な承認が
    「否認済み」に汚染されうる。0007 の ``UNIQUE(proposal_ref)`` により提案の
    再記録もできないため、撤回の表現が無いと復旧手段が存在しない。撤回行を
    最新行に持つ決定を、現決定 view は「否認されていない」として返す。

    ``reason`` は撤回にも必須(誤操作の是正なのか方針変更なのかが残らないと、
    否認統計 deemed_ratio の解釈が壊れる)。
    """
    return _append_veto_row(
        conn, decision_id, "withdrawal", reason,
        vetoed_by=vetoed_by, owner_ids=owner_ids,
        expected_proposal_ref=expected_proposal_ref, origin=origin,
        run_id=run_id,
    )


# 現決定 view から読む列(:func:`current_decision` / :func:`current_decision_by_id` 共通)。
_CURRENT_DECISION_COLUMNS = """
    decision_id, proposal_ref, kind, recorded_decision,
    effective_decision, is_vetoed, decided_by, decided_at,
    veto_id, veto_kind, vetoed_by, veto_reason, revert_commit,
    derived_effects_ref, vetoed_at, reviewed_sha, review_ref, veto_origin
"""


def _select_current_decision(
    conn: psycopg.Connection, where: str, param: object
) -> dict[str, object] | None:
    """現決定 view を1件読む(検索キーだけが異なる2つの読み口の共通実装)。"""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_CURRENT_DECISION_COLUMNS} "  # noqa: S608 - where は呼び出し元の定数
            f"FROM governance.current_decisions WHERE {where}",
            (param,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [d.name for d in cur.description]
    return dict(zip(columns, row, strict=True))


def current_decision(
    conn: psycopg.Connection, proposal_ref: str
) -> dict[str, object] | None:
    """現決定(``governance.current_decisions`` view — 0021)を1件読む。

    承認記録を読むコードは ``governance.decisions`` を直接読まず本関数を使う。
    否認された決定の ``effective_decision`` は ``'vetoed'`` になるため、
    「承認済み」として扱う分岐に否認済みの決定が紛れ込まない。
    """
    return _select_current_decision(conn, "proposal_ref = %s", proposal_ref)


def current_decision_by_id(
    conn: psycopg.Connection, decision_id: int
) -> dict[str, object] | None:
    """``governance.decisions.id`` から現決定を引く(A-18-1 のトレーラ突合の読み口)。

    保護領域コミットの ``Approved: <ID>`` トレーラ(定款第5条)は決定を **ID** で指す。
    監査がこれを ``governance.decisions`` の直読で解決すると、代表が否認した承認を
    「承認済み」と受理し、否認された変更が無承認変更として検出されなくなる
    (独立役員審査 0021 C-5)。したがって突合は必ず現決定 view を経由する。
    """
    return _select_current_decision(conn, "decision_id = %s", decision_id)


# ────────────────────────────────────────────────────────────────────────────
# CLI 補助: PR 番号から通知文面を組み立てる(--deemed-for-pr)
#
# 保護領域 PR の起票を検知して自動で通知する基盤(GitHub webhook)は無い
# (ops/reminders.yaml ``deemed-auto-announce``)。次善として、設計リードが PR 番号1つで
# 発効通知を出せる簡易形を用意する —— 参照(PR URL)と文面の入力を機械が埋めることで、
# 「手で打ち直すのが面倒だから後で」による叩き忘れ(= 通知なき発効)を減らす。
# 叩き忘れそのものは監査 A-18-7(保護領域 PR の承認記録漏れ)が事後に検出する。
# ────────────────────────────────────────────────────────────────────────────
#: ``gh`` 呼び出しのタイムアウト(秒)。ネットワーク待ちで CLI を固まらせない。
GH_TIMEOUT = 30

#: 通知本文に列挙する変更ファイルの上限(embed のフィールド長に収める)。
NOTICE_FILE_LIMIT = 12


class PullRequestLookupError(RuntimeError):
    """``gh`` から PR 情報を取得できない(未認証・番号違い・クローズ済み等)。"""


@dataclass(frozen=True)
class PullRequestRef:
    """みなし承認の対象となる PR の最小情報。"""

    number: int
    url: str
    title: str
    state: str
    merged: bool
    files: tuple[str, ...]
    #: ブランチ先端(``head.sha``)。発効時点の審査対象コミットとして ``reviewed_sha`` に入る
    head_sha: str | None = None


def _gh_api(path: str, *, paginate: bool = False, jq: str | None = None) -> Any:
    """``gh api`` を実行して JSON を返す(失敗は :class:`PullRequestLookupError`)。

    ``gh`` を使うのは、認証(トークンの保管・更新)を自前で持たないためである。
    監査 A-18 が GitHub API 照合を「実弾移行の前提条件」として未実装にしているのと同じ
    理由で、ここでも API クライアントは足さない。
    """
    import json
    import shutil
    import subprocess

    if shutil.which("gh") is None:
        raise PullRequestLookupError(
            "gh CLI が見つからない(--deemed-for-pr は gh api を使う)。"
            "--proposal-ref / --kind / --notice を手で指定すれば gh 無しでも発効できる"
        )
    cmd = ["gh", "api", path]
    if paginate:
        cmd.append("--paginate")
    if jq:
        cmd += ["--jq", jq]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=GH_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise PullRequestLookupError(f"gh api がタイムアウトした: {path}") from exc
    if out.returncode != 0:
        raise PullRequestLookupError(f"gh api に失敗した({path}): {out.stderr.strip()}")
    if jq:
        return [ln for ln in out.stdout.splitlines() if ln.strip()]
    return json.loads(out.stdout)


def fetch_pull_request(pr_number: int, *, repo: str | None = None) -> PullRequestRef:
    """PR のタイトル・URL・状態・変更ファイル・head SHA を ``gh api`` で取得する。

    ``repo`` 省略時は ``:owner/:repo``(gh がカレントのリポジトリへ解決する)。
    **クローズ済み(未マージ)の PR は拒否する** —— 取り下げられた提案の発効を通知しても
    取消義務(定款第3条2号)だけが残る。オープンな PR を対象にできるのは意図どおりで、
    みなし承認は「PR 起票時に通知して発効する」運用だからである。

    ``head.sha`` を取るのは、発効時点のブランチ先端が「独立審査が見た内容」であり、
    ``governance.decisions.reviewed_sha``(0029)に入る値だからである。GitHub から取る
    (手入力させない)ことで、**発効の時刻に固定された値**になる —— 後から積んだコミットは
    この SHA の祖先にならず、監査 A-18-1 の承継範囲にも A-18-8 の突合にも現れない。
    """
    slug = repo or ":owner/:repo"
    data = _gh_api(f"repos/{slug}/pulls/{pr_number}")
    if not isinstance(data, dict):
        raise PullRequestLookupError(f"PR #{pr_number} の応答が想定外の形式")
    state = str(data.get("state") or "")
    merged = bool(data.get("merged"))
    if state == "closed" and not merged:
        raise PullRequestLookupError(
            f"PR #{pr_number} はクローズ済み(未マージ)。取り下げられた提案を発効させない"
        )
    try:
        files = _gh_api(f"repos/{slug}/pulls/{pr_number}/files", paginate=True, jq=".[].filename")
    except PullRequestLookupError:
        files = []  # 一覧は文面の補助でしかない。取れなくても発効そのものは妨げない
    head = data.get("head")
    head_sha = str(head.get("sha")) if isinstance(head, dict) and head.get("sha") else None
    return PullRequestRef(
        number=pr_number,
        url=str(data.get("html_url") or ""),
        title=str(data.get("title") or ""),
        state=state,
        merged=merged,
        files=tuple(str(f) for f in files),
        head_sha=head_sha,
    )


def build_pr_notice(pr: PullRequestRef, review_ref: str, reviewed_sha: str | None = None) -> str:
    """PR 情報と独立役員審査の参照から、``#承認`` へ出す通知本文を組み立てる。

    審査参照を**引数として要求する**のは、この簡易形が「審査前の発効」を作らないためである
    (reminders ``deemed-auto-announce`` ②)。文面に審査の所在を書かせることで、審査を
    経ていない変更をワンコマンドで発効させる経路を塞ぐ。文面が気に入らなければ
    ``--notice`` で全文を差し替えられるが、そのときも審査参照の行は付く
    (:func:`_with_review_line`)。

    参照は ``governance.decisions.review_ref`` に構造化して記録され(0029)、リポジトリ内
    パス形式なら実在も検査する(:func:`missing_review_ref_warning` — 不在は警告で、拒否では
    ない)。``reviewed_sha`` を渡すと審査対象コミットも本文に出す —— ``#承認`` を読む代表が
    「どの時点の内容が発効したのか」を通知だけで確認できるようにするためである。

    変更ファイルは保護領域か否かを判定せずそのまま列挙する。glob の解釈は監査
    (``audit/a18.protected_patterns``)の責務であり、ここで二重に定義するとずれる。
    """
    lines = [f"PR #{pr.number}「{pr.title}」を保護領域の変更として発効します。", f"対象: {pr.url}"]
    if pr.files:
        shown = ", ".join(pr.files[:NOTICE_FILE_LIMIT])
        if len(pr.files) > NOTICE_FILE_LIMIT:
            shown += f" ほか {len(pr.files) - NOTICE_FILE_LIMIT} 件"
        lines.append(f"変更ファイル({len(pr.files)} 件): {shown}")
    if reviewed_sha:
        lines.append(f"{REVIEWED_LINE_PREFIX}{reviewed_sha}")
    lines.append(f"{REVIEW_LINE_PREFIX}{review_ref}")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────
# CLI: みなし承認の発効通知(記録と通知を同一トランザクションで)
# ────────────────────────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ryza.governance.decisions",
        description="みなし承認を #承認 へ通知し、同一トランザクションで記録する(定款第3条)",
    )
    parser.add_argument(
        "--deemed", action="store_true",
        help="みなし承認を発効させる(現状これが唯一のアクション。明示承認は Bot のボタン経路)",
    )
    parser.add_argument(
        "--deemed-for-pr", type=int, default=None, metavar="PR番号",
        help=(
            "PR 番号から参照・種別・文面を埋めてみなし承認を発効させる簡易形"
            "(gh api で PR タイトル・URL・変更ファイルを取得。--review が必須)"
        ),
    )
    parser.add_argument(
        "--review", default=None,
        help=(
            "独立役員審査の参照(docs/reviews/... 等)。--deemed-for-pr と --kind pr では必須。"
            "値は decisions.review_ref に構造化して記録する。リポジトリ内パス形式なら実在を"
            "検査するが、不在でも発効は妨げない(遡及登録を塞がないため — 警告のみ)"
        ),
    )
    parser.add_argument(
        "--reviewed-sha", default=None, metavar="SHA40",
        help=(
            "審査対象コミットの完全 SHA(decisions.reviewed_sha)。--deemed-for-pr では"
            "PR の head SHA が自動で入るため通常は不要。--review の意見書が front matter で"
            "reviewed_sha を宣言している場合は**審査側の記録が優先**され、本指定と食い違えば"
            "発効を中止する。監査 A-18-8 が Approved トレーラの reviewed=<sha40> と突合する"
        ),
    )
    parser.add_argument(
        "--review-missing-ok", action="store_true",
        help=(
            "--kind pr でも審査参照の実在検査を外す(リポジトリ外の審査・遡及登録の明示)。"
            "指定した事実は meta.runs の params に残り、事後に件数を数えられる"
        ),
    )
    parser.add_argument(
        "--repo-root", default=None, metavar="PATH",
        help=(
            "意見書(--review)を解決するリポジトリルート。マージ前の PR ブランチを"
            "checkout した worktree を指す用途(Issue #132)。省略時は本 CLI の設置場所から"
            "自動決定(メイン checkout を見るため、PR ブランチにしか無い意見書は解決できず"
            "reviewed_sha が PR head へフォールバックする)。指定時は PATH/config/governance.yaml"
            "の実在を確認して fail-closed で検証し、決定の note に repo_root=<絶対パス> を残す"
        ),
    )
    parser.add_argument(
        "--gh-repo", default=None, metavar="OWNER/NAME",
        help="gh api の対象リポジトリ(既定はカレントのリポジトリ)",
    )
    parser.add_argument(
        "--proposal-ref", default=None,
        help="提案の一意参照(PR URL 等)。--deemed-for-pr 指定時は省略可",
    )
    parser.add_argument(
        "--kind", default=None, choices=sorted(set(KINDS) - RESERVED_KINDS),
        help="提案種別(3専決事項の kind は選べない — 定款第3条)。--deemed-for-pr の既定は pr",
    )
    parser.add_argument(
        "--notice", default=None,
        help="通知の要旨(何がなぜ発効したか)。--deemed-for-pr 指定時は自動生成される",
    )
    parser.add_argument("--title", default=None, help="通知の見出し(省略時は既定文)")
    parser.add_argument(
        "--source", default=DEFAULT_DEEMED_SOURCE,
        help=f"発効源(decided_by は system:<source> になる。既定 {DEFAULT_DEEMED_SOURCE})",
    )
    parser.add_argument("--note", default=None, help="決定への補足(任意)")
    parser.add_argument(
        "--role", default="dev_lead", help="通知の発信者役職(config/org.yaml。既定 dev_lead)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="DB へ書かず、投稿する embed を表示するだけ"
    )
    return parser


#: ``--review`` を必須にする提案種別。保護領域 PR のみなし承認は独立役員審査を前置する
#: 手続(定款第5条・07-development)であり、審査参照なしにワンコマンドで発効させられる
#: 経路を残さない。**他の kind には課さない** —— 戦略昇格・予算・IPS 改訂などは独立役員審査が
#: 必ずしも前置される手続ではなく、一律必須化は正当な発効経路を塞ぐ(後続配線審査 後-1)。
REVIEW_REQUIRED_KINDS: frozenset[str] = frozenset({"pr"})

#: 通知本文に審査参照を残す行の接頭辞(:func:`build_pr_notice` と同じ表記)。
REVIEW_LINE_PREFIX = "独立役員審査: "

#: 通知本文に審査対象コミットを残す行の接頭辞。
REVIEWED_LINE_PREFIX = "審査対象コミット: "


def _with_review_line(notice: str, review_ref: str) -> str:
    """手書きの通知本文に審査参照の行を足す(既に含まれていればそのまま)。

    ``--notice`` で文面を差し替えたときに ``--review`` が素通りすると、必須化が
    「引数を渡させるだけ」の儀式になり ``#承認`` に審査の所在が残らない。
    """
    return notice if review_ref in notice else f"{notice}\n{REVIEW_LINE_PREFIX}{review_ref}"


def _with_reviewed_line(notice: str, reviewed_sha: str) -> str:
    """手書きの通知本文に審査対象コミットの行を足す(既に含まれていればそのまま)。

    ``#承認`` を読む代表が「どの時点の内容が発効したのか」を通知だけで判断できるようにする。
    記録側(``reviewed_sha``)にしか無いと、否認の判断のたびに DB を引く必要が出る。
    """
    return notice if reviewed_sha in notice else f"{notice}\n{REVIEWED_LINE_PREFIX}{reviewed_sha}"


@dataclass(frozen=True)
class DeemedTarget:
    """CLI 引数から解決した「何を・どう発効させるか」。"""

    proposal_ref: str
    kind: str
    notice: str
    reviewed_sha: str | None = None
    review_ref: str | None = None
    #: ``reviewed_sha`` の由来(:data:`SHA_SOURCE_ARTIFACT` 等)。開示専用
    reviewed_sha_source: str | None = None
    #: 由来にまつわる注記(head SHA との相違・front matter の様式不備など)
    reviewed_notes: tuple[str, ...] = ()


def _validated_repo_root(value: str | None) -> Path | None:
    """``--repo-root`` を検証して絶対 ``Path`` にする(未指定なら ``None`` = 従来経路)。

    **fail-closed** の理由(Issue #132): ``--repo-root`` が指す先が Ryza の checkout でなければ、
    ``resolve_review_path`` は「リポジトリ外の審査参照」に落ち **全ての意見書解決が沈黙して
    失敗する** —— 起票者は指定したつもりで通ってしまい、``reviewed_sha`` は PR head へ静かに
    フォールバックする。指定を受け付けたら受け付けた分、指定先が正しいことを CLI 側で
    実体化するのが本オプションの意味である。目印は ``_repo_root()`` のフォールバックと同じ
    ``config/governance.yaml`` を使う(既存の「Ryza の checkout かどうか」の判定と揃える)。
    """
    if value is None:
        return None
    root = Path(value).expanduser()
    if not root.is_dir():
        raise ValueError(
            f"--repo-root='{value}' はディレクトリとして存在しない"
            "(意見書の探索ルートを指定できない)"
        )
    if not (root / "config" / "governance.yaml").is_file():
        raise ValueError(
            f"--repo-root='{value}' は Ryza リポジトリの checkout に見えない"
            "(config/governance.yaml が無い — worktree のパスを確認すること)"
        )
    return root.resolve()


def _resolve_deemed_args(
    args: argparse.Namespace, *, repo_root: Path | None = None
) -> DeemedTarget:
    """CLI 引数から発効対象(:class:`DeemedTarget`)を決める。

    ``--deemed-for-pr`` があれば ``gh`` の取得結果で欠けている引数を埋める。明示指定は
    常に優先する(自動生成の文面が状況に合わないときに手で上書きできる余地を残す)。

    ``--review`` は ``--deemed-for-pr`` と ``--kind pr`` で**必須**であり、``--notice`` で
    代替できない(後続配線審査 後-1: 旧実装は ``--notice`` があれば審査参照ゼロで通り、
    「審査前の発効をワンコマンドで作れない」という主張が成立していなかった)。

    **審査参照と審査対象 SHA は構造化列になる**(0029): ``--review`` は ``review_ref``、
    ``--deemed-for-pr`` の head SHA(または ``--reviewed-sha``)は ``reviewed_sha`` に入り、
    監査 A-18-8 が ``Approved:`` トレーラの ``reviewed=`` と突合する。

    ``--review`` が front matter 付きの意見書(新様式 — :mod:`ryza.reviews`)を指す場合、
    ``reviewed_sha`` は**審査側の記録**を採り、``--reviewed-sha`` との食い違いは
    :class:`ReviewedShaConflictError` で発効を止める(:func:`resolve_reviewed_sha`)。
    旧様式ではそれでも**証明ではない** —— 値はどちらも起票者の申告であり、同じ嘘を両方に
    書けば一致する。突合が効くのは「トレーラだけ後から書き換えた」「別 PR の SHA を写した」
    といった片側の食い違いに対してである。
    """
    if args.deemed_for_pr is None:
        missing = [
            name
            for name, value in (
                ("--proposal-ref", args.proposal_ref),
                ("--kind", args.kind),
                ("--notice", args.notice),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"{', '.join(missing)} は必須(--deemed-for-pr <PR番号> なら自動で埋まる)"
            )
        if args.kind in REVIEW_REQUIRED_KINDS and not args.review:
            raise ValueError(
                f"--kind {args.kind} のみなし承認には --review(独立役員審査の参照)が必須。"
                "保護領域 PR は審査を前置する手続であり、--notice では代替できない"
            )
        require_existing_review(
            args.review, args.kind, missing_ok=args.review_missing_ok,
            repo_root=repo_root,
        )
        choice = resolve_reviewed_sha(
            args.review, args.reviewed_sha, repo_root=repo_root
        )
        reviewed_sha = choice.sha
        notice = args.notice
        if reviewed_sha:
            notice = _with_reviewed_line(notice, reviewed_sha)
        if args.review:
            notice = _with_review_line(notice, args.review)
        return DeemedTarget(
            proposal_ref=args.proposal_ref,
            kind=args.kind,
            notice=notice,
            reviewed_sha=reviewed_sha,
            review_ref=args.review,
            reviewed_sha_source=choice.source,
            reviewed_notes=choice.notes,
        )

    if not args.review:
        raise ValueError(
            "--deemed-for-pr には --review(独立役員審査の参照)が必須。"
            "審査を経ていない変更をワンコマンドで発効させないための入口検査であり、"
            "--notice で文面を差し替えても免除されない"
        )
    # 実在検査は gh を呼ぶ前に済ませる(存在しない審査参照でネットワークを使わない)。
    # --deemed-for-pr の kind 既定は 'pr' なので、明示指定が無ければ pr として検査する。
    require_existing_review(
        args.review, args.kind or "pr", missing_ok=args.review_missing_ok,
        repo_root=repo_root,
    )
    pr = fetch_pull_request(args.deemed_for_pr, repo=args.gh_repo)
    if not pr.url:
        raise ValueError(f"PR #{args.deemed_for_pr} の URL を取得できなかった")
    # 審査記録 > 明示指定 > gh の head SHA(:func:`resolve_reviewed_sha`)。手で書けるのは、
    # 審査が head より前のコミットを対象とした場合(審査後に無関係な追従コミットを積んだ等)に
    # **実際に見た SHA** を残せるようにするため。審査記録がある場合はそちらが正であり、
    # 明示指定との食い違いは :class:`ReviewedShaConflictError` で発効を止める。
    choice = resolve_reviewed_sha(
        args.review, args.reviewed_sha, fallback=pr.head_sha, repo_root=repo_root,
    )
    reviewed_sha = choice.sha
    if args.notice:
        notice = args.notice
        if reviewed_sha:
            notice = _with_reviewed_line(notice, reviewed_sha)
        notice = _with_review_line(notice, args.review)
    else:
        notice = build_pr_notice(pr, args.review, reviewed_sha)
    return DeemedTarget(
        proposal_ref=args.proposal_ref or pr.url,
        kind=args.kind or "pr",
        notice=notice,
        reviewed_sha=reviewed_sha,
        review_ref=args.review,
        reviewed_sha_source=choice.source,
        reviewed_notes=choice.notes,
    )


#: 決定の ``note`` に残す審査参照警告の接頭辞(事後監査の検索キー)。
REVIEW_WARNING_NOTE_PREFIX = "[審査参照の警告] "

#: 決定の ``note`` に残す ``--repo-root`` 使用痕の接頭辞(Issue #132)。監査時に
#: 「どの checkout の意見書を読んだか」を追える検索キーであり、``meta.runs`` の params だけに
#: 残すと決定を直接読む監査(A-18)から届かない —— 二重に書くのが安全側。
REPO_ROOT_NOTE_PREFIX = "[repo_root] "


def _note_with_warning(note: str | None, warning: str | None) -> str | None:
    """``--note`` に審査参照の警告を追記する(警告が無ければそのまま)。

    警告を**記録側にも残す**のは、stderr が消えた後に「実在しない審査参照で発効した決定」を
    事後に特定できるようにするためである(独立役員審査 SHA-6)。追記オンリーの列なので、
    後から「実は実在した」と分かっても打ち消せない —— それでよい。警告は事実の記録であって
    判定ではなく、解釈は読む側が行う。
    """
    if not warning:
        return note
    line = f"{REVIEW_WARNING_NOTE_PREFIX}{warning}"
    return line if not note else f"{note}\n{line}"


def _note_with_repo_root(note: str | None, repo_root: Path | None) -> str | None:
    """``--repo-root`` を使ったなら決定の ``note`` に絶対パスを残す(Issue #132)。

    既存の note 追記(:func:`_note_with_warning`)と同じ書式に倣い、行頭に検索用の
    接頭辞を付ける。未使用なら元の note をそのまま返す(未指定時の挙動を不変にする)。
    """
    if repo_root is None:
        return note
    line = f"{REPO_ROOT_NOTE_PREFIX}repo_root={repo_root}"
    return line if not note else f"{note}\n{line}"


def main(argv: list[str] | None = None) -> int:
    """CLI エントリポイント(``python -m ryza.governance.decisions --deemed ...``)。

    ``notices`` は本モジュールを import するため、依存の向きを保つ目的で遅延 import する
    (writer である本モジュールは通知経路を知らない)。
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _build_parser().parse_args(argv)
    if not (args.deemed or args.deemed_for_pr):
        print(
            "--deemed または --deemed-for-pr <PR番号> を指定してください"
            "(現状みなし承認の発効が唯一のアクション)",
            file=sys.stderr,
        )
        return 2
    try:
        repo_root = _validated_repo_root(args.repo_root)
    except ValueError as exc:
        print(f"みなし承認の対象を解決できませんでした: {exc}", file=sys.stderr)
        return 1
    try:
        target = _resolve_deemed_args(args, repo_root=repo_root)
    except (PullRequestLookupError, ValueError) as exc:
        print(f"みなし承認の対象を解決できませんでした: {exc}", file=sys.stderr)
        return 1

    # 審査参照の実在検査は、``--kind pr`` では既に require_existing_review が中止として
    # 扱っている(審査 C-2(c))。ここに残るのは他 kind と --review-missing-ok の実行であり、
    # そこでは従来どおり**警告**にとどめる(遡及登録・リポジトリ外の審査を塞がないため)。
    # 警告は stderr だけに出すと**痕跡が残らず**、事後監査から「警告が出たか」を判別できない
    # (独立役員審査 SHA-6)。Run の params と決定の note に載せて DB 側にも残す。
    try:
        warning = missing_review_ref_warning(target.review_ref, repo_root=repo_root)
    except ValueError as exc:  # 脱出表記・symlink は解決時点で落ちているはずの保険
        print(f"みなし承認の対象を解決できませんでした: {exc}", file=sys.stderr)
        return 1
    if warning:
        print(f"警告: {warning}", file=sys.stderr)
        log.warning("%s", warning)
    # 審査記録からの採用・front matter の様式不備は**発効を止めない**種類の事実なので、
    # 黙らせずに出す(止める種類の食い違い・判定は既に例外で落ちている)。
    for note in target.reviewed_notes:
        print(f"注記: {note}", file=sys.stderr)
        log.info("%s", note)

    import json

    from ryza.db.conn import connect
    from ryza.governance import notices
    from ryza.provenance import start_run

    if args.dry_run:
        embed = notices.build_deemed_notice_embed(
            target.proposal_ref, target.kind, target.notice, title=args.title, role=args.role
        )
        print(json.dumps(embed, ensure_ascii=False, indent=2))
        return 0

    # Run は自前接続(autocommit)で持つ。記録側を rollback しても「試みた事実」が
    # meta.runs に残る(a18.run_and_report と同じ流儀)。
    run = start_run(
        "governance.deemed_notice",
        {
            "proposal_ref": target.proposal_ref,
            "kind": target.kind,
            "source": args.source,
            "reviewed_sha": target.reviewed_sha,
            # 由来を残すのは、A-18-8 の一致が審査記録の裏付けを持つのか起票者の申告
            # どうしの一致なのかを事後に区別するため(reviewed-sha-from-review-agent)。
            "reviewed_sha_source": target.reviewed_sha_source,
            "review_ref": target.review_ref,
            # 警告が出た実行かどうかを meta.runs に残す(stderr は消える — SHA-6)。
            "review_ref_warning": warning,
            # 同じ原則を reviewed_notes にも適用する(審査 C-5)。head SHA との相違・
            # front matter の様式警告・判定は stderr と log にしか出ておらず、
            # 「30 行下で同じ原則を適用し忘れている」状態だった。
            "reviewed_notes": list(target.reviewed_notes),
            # 実在検査を明示的に外した実行を数えられるようにする(審査 C-2(c))。
            "review_missing_ok": bool(args.review_missing_ok),
            # どの checkout の意見書を読んだかを meta.runs にも残す(Issue #132)。
            # 未指定なら None(既定経路 = _repo_root() 委譲)であり、note と併記する。
            "repo_root": str(repo_root) if repo_root is not None else None,
        },
    )
    conn = connect()
    try:
        result = notices.announce_deemed_approval(
            conn, target.proposal_ref, target.kind, target.notice, run.run_id,
            source=args.source,
            note=_note_with_repo_root(_note_with_warning(args.note, warning), repo_root),
            title=args.title, role=args.role,
            reviewed_sha=target.reviewed_sha, review_ref=target.review_ref,
        )
        conn.commit()
        run.finish("success")
    except (ValueError, PermissionError) as exc:
        conn.rollback()
        run.finish("failed")
        print(f"みなし承認を記録できませんでした: {exc}", file=sys.stderr)
        return 1
    except Exception:
        conn.rollback()
        run.finish("failed")
        raise
    finally:
        conn.close()

    print(
        f"みなし承認を記録し通知を投入しました: decision_id={result.decision.id} "
        f"notice_ref={result.notice_ref} decided_by={result.decision.decided_by} "
        f"reviewed_sha={result.decision.reviewed_sha or '(未申告)'} "
        f"由来={target.reviewed_sha_source or '(なし)'}",
        file=sys.stderr,
    )
    return 0


__all__ = [
    "DEFAULT_DEEMED_SOURCE",
    "REPO_ROOT_NOTE_PREFIX",
    "RESERVED_KINDS",
    "RESERVED_KIND_BY_MATTER",
    "REVIEWED_LINE_PREFIX",
    "REVIEW_LINE_PREFIX",
    "REVIEW_REQUIRED_KINDS",
    "SHA_SOURCE_ARGUMENT",
    "SHA_SOURCE_ARTIFACT",
    "SHA_SOURCE_PR_HEAD",
    "SYSTEM_ACTOR_PREFIX",
    "VETOABLE_DECISIONS",
    "VETO_KINDS",
    "VETO_ORIGINS",
    "DeemedApproval",
    "DeemedTarget",
    "DuplicateDecisionError",
    "NotVetoableError",
    "ProposalRefMismatchError",
    "PullRequestLookupError",
    "PullRequestRef",
    "MissingReviewArtifactError",
    "ReservedMatterError",
    "ReviewVerdictBlocksError",
    "ReviewedShaChoice",
    "ReviewedShaConflictError",
    "Veto",
    "build_pr_notice",
    "current_decision",
    "current_decision_by_id",
    "fetch_pull_request",
    "main",
    "missing_review_ref_warning",
    "normalize_reviewed_sha",
    "record_deemed_approval",
    "record_revert_completion",
    "record_veto",
    "record_veto_withdrawal",
    "require_existing_review",
    "resolve_reviewed_sha",
    "validate_proposal_ref",
]


if __name__ == "__main__":  # pragma: no cover - CLI 実行パス
    raise SystemExit(main())
