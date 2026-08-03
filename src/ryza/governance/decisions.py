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

``--review``(独立役員審査の参照)は ``--deemed-for-pr`` と ``--kind pr`` で必須である。
値は通知本文に残るだけでなく ``governance.decisions.review_ref`` に構造化して記録し、
``--deemed-for-pr`` では PR の head SHA を ``reviewed_sha`` として自動で埋める(0029)。
これにより監査 A-18-8 が「トレーラの ``reviewed=<sha>``」と「承認記録の ``reviewed_sha``」を
突合できる —— **別経路で書かれた2つの申告**なので、片方だけを書き換えた偽装は不一致で出る。

**残る限界**: どちらの値も発効を起票した側が書く。審査エージェント自身の署名ではないため、
起票者が両方に同じ嘘を書けば一致する。``--review`` の実在検査もリポジトリ内パス形式に
限られ、**不在でも拒否はしない**(過去の審査を遡って登録する経路を塞がないため — 警告のみ)。

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

# 審査対象 SHA の様式(0029 の decisions_reviewed_sha_check と一致させる)。短縮 SHA を
# 許すと A-18-8 の突合が「一致とも不一致とも言えない」状態を作るため完全 SHA のみ。
# 監査側(audit/a18._FULL_SHA_RE)と同じ様式だが、audit は被監査モジュールを import しない
# 方針なので定数は共有せず各々が持つ。
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)

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

    **拒否ではなく警告にする理由**: 審査意見書がリポジトリ外(Discord スレッド・Issue)に
    ある運用と、過去に完了した審査を後から遡って登録する運用(``docs/reviews`` に無い・
    別ブランチにしか無いファイルを指す)を塞いでしまうため。実在検査の目的は「``--review 嘘``
    をタイプミスや出まかせのまま通さない」ことであって、発効そのものの可否判定ではない。

    URL(``http://`` / ``https://``)や ``discord://`` 等のスキーム付き参照は対象外
    —— ネットワーク越しの実在確認は CLI の責務にしない(gh 以外の到達手段を増やさない)。

    **リポジトリルートが決められない実行では検査そのものを行わない**(独立役員審査 SHA-6):
    ``__file__`` からの相対位置はソースチェックアウト前提であり、パッケージとして設置された
    実行では site-packages を指して**全参照が誤警告**になる。git 作業ツリーの外なら検査を
    諦めて ``None`` を返す —— 誤警告は「警告が出ていても実在する」学習を生み、警告そのものを
    無意味にするので、検査できないときは黙るほうが安全である。
    """
    if not review_ref:
        return None
    ref = review_ref.strip()
    if "://" in ref or ref.startswith("#"):
        return None
    root = repo_root or _repo_root()
    if root is None or (root / ref).exists():
        return None
    return (
        f"--review の参照 '{ref}' がリポジトリ内に見つからない"
        "(パス形式に見えるが実在しない — 発効は妨げないが、審査意見書の所在を確認すること)"
    )


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
    revert_commit: str | None = None,
    derived_effects_ref: str | None = None,
    run_id: int | None = None,
) -> Veto:
    """``governance.decision_vetoes`` へ1行追記する(否認系 writer の共通実装)。

    検証の順序は「安いものから、かつ破壊的でない順」:
    文字列必須 → オーナー検証 → 対象決定の実在 → ``proposal_ref`` 照合 → INSERT。
    """
    if kind not in VETO_KINDS:
        raise ValueError(f"未知の否認行種別: {kind}(既知: {', '.join(VETO_KINDS)})")
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
                 revert_commit, derived_effects_ref, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING veto_id
            """,
            (
                decision_id, kind, vetoed_by, reason,
                revert_commit, derived_effects_ref, run_id,
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
    )


def record_veto(
    conn: psycopg.Connection,
    decision_id: int,
    reason: str,
    *,
    vetoed_by: str,
    owner_ids: Iterable[str],
    expected_proposal_ref: str,
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
        revert_commit: 取消コミット SHA。否認時点で未確定なら省略し、確定後に
            :func:`record_revert_completion` で追記する
        derived_effects_ref: 取消不能な派生効果一覧の参照(``#運営`` への報告)
        run_id: 記録したジョブ実行。否認は代表の作為でありジョブ生成物ではないため任意

    Raises:
        ValueError: 必須文字列が空、または decision_id が存在しない
        NotOwnerError: 非オーナーの否認操作
        ProposalRefMismatchError: expected_proposal_ref の不一致
        NotVetoableError: 対象が approve / deemed 以外(却下・質問は否認できない)
    """
    return _append_veto_row(
        conn, decision_id, "veto", reason,
        vetoed_by=vetoed_by, owner_ids=owner_ids,
        expected_proposal_ref=expected_proposal_ref,
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
    revert_commit: str | None = None,
    derived_effects_ref: str | None = None,
    run_id: int | None = None,
) -> Veto:
    """否認に伴う取消の完了報告(``kind='revert_complete'``)を追記する。

    定款第3条は否認された変更の「遅滞ない取消」と、取消不能な派生効果の
    ``#運営`` への報告を義務付ける。追記オンリーのため否認行を UPDATE できず、
    確定した ``revert_commit`` / 派生効果一覧は本関数で追記する。現決定 view は
    これらを**列単位**で解決するので、片方だけの追記がもう片方を消さない。
    """
    return _append_veto_row(
        conn, decision_id, "revert_complete", reason,
        vetoed_by=vetoed_by, owner_ids=owner_ids,
        expected_proposal_ref=expected_proposal_ref,
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
        expected_proposal_ref=expected_proposal_ref,
        run_id=run_id,
    )


# 現決定 view から読む列(:func:`current_decision` / :func:`current_decision_by_id` 共通)。
_CURRENT_DECISION_COLUMNS = """
    decision_id, proposal_ref, kind, recorded_decision,
    effective_decision, is_vetoed, decided_by, decided_at,
    veto_id, veto_kind, vetoed_by, veto_reason, revert_commit,
    derived_effects_ref, vetoed_at, reviewed_sha, review_ref
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
            "PR の head SHA が自動で入るため通常は不要。監査 A-18-8 が Approved トレーラの"
            "reviewed=<sha40> と突合する"
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


def _resolve_deemed_args(args: argparse.Namespace) -> DeemedTarget:
    """CLI 引数から発効対象(:class:`DeemedTarget`)を決める。

    ``--deemed-for-pr`` があれば ``gh`` の取得結果で欠けている引数を埋める。明示指定は
    常に優先する(自動生成の文面が状況に合わないときに手で上書きできる余地を残す)。

    ``--review`` は ``--deemed-for-pr`` と ``--kind pr`` で**必須**であり、``--notice`` で
    代替できない(後続配線審査 後-1: 旧実装は ``--notice`` があれば審査参照ゼロで通り、
    「審査前の発効をワンコマンドで作れない」という主張が成立していなかった)。

    **審査参照と審査対象 SHA は構造化列になる**(0029): ``--review`` は ``review_ref``、
    ``--deemed-for-pr`` の head SHA(または ``--reviewed-sha``)は ``reviewed_sha`` に入り、
    監査 A-18-8 が ``Approved:`` トレーラの ``reviewed=`` と突合する。**それでも証明では
    ない** —— どちらも起票者の申告であり、審査エージェント自身の署名は無い。同じ嘘を両方に
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
        reviewed_sha = normalize_reviewed_sha(args.reviewed_sha)
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
        )

    if not args.review:
        raise ValueError(
            "--deemed-for-pr には --review(独立役員審査の参照)が必須。"
            "審査を経ていない変更をワンコマンドで発効させないための入口検査であり、"
            "--notice で文面を差し替えても免除されない"
        )
    pr = fetch_pull_request(args.deemed_for_pr, repo=args.gh_repo)
    if not pr.url:
        raise ValueError(f"PR #{args.deemed_for_pr} の URL を取得できなかった")
    # 明示指定 > gh の head SHA。手で書けるのは、審査が head より前のコミットを対象とした
    # 場合(審査後に無関係な追従コミットを積んだ等)に**実際に見た SHA** を残せるようにするため。
    reviewed_sha = normalize_reviewed_sha(args.reviewed_sha) or normalize_reviewed_sha(pr.head_sha)
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
    )


#: 決定の ``note`` に残す審査参照警告の接頭辞(事後監査の検索キー)。
REVIEW_WARNING_NOTE_PREFIX = "[審査参照の警告] "


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
        target = _resolve_deemed_args(args)
    except (PullRequestLookupError, ValueError) as exc:
        print(f"みなし承認の対象を解決できませんでした: {exc}", file=sys.stderr)
        return 1

    # 審査参照の実在検査は**警告**であって発効の可否ではない(遡及登録・リポジトリ外の
    # 審査を塞がないため)。黙って通すと `--review 嘘` がタイプミスのまま記録に残る。
    # 警告は stderr だけに出すと**痕跡が残らず**、事後監査から「警告が出たか」を判別できない
    # (独立役員審査 SHA-6)。Run の params と決定の note に載せて DB 側にも残す。
    warning = missing_review_ref_warning(target.review_ref)
    if warning:
        print(f"警告: {warning}", file=sys.stderr)
        log.warning("%s", warning)

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
            "review_ref": target.review_ref,
            # 警告が出た実行かどうかを meta.runs に残す(stderr は消える — SHA-6)。
            "review_ref_warning": warning,
        },
    )
    conn = connect()
    try:
        result = notices.announce_deemed_approval(
            conn, target.proposal_ref, target.kind, target.notice, run.run_id,
            source=args.source, note=_note_with_warning(args.note, warning),
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
        f"reviewed_sha={result.decision.reviewed_sha or '(未申告)'}",
        file=sys.stderr,
    )
    return 0


__all__ = [
    "DEFAULT_DEEMED_SOURCE",
    "RESERVED_KINDS",
    "RESERVED_KIND_BY_MATTER",
    "REVIEWED_LINE_PREFIX",
    "REVIEW_LINE_PREFIX",
    "REVIEW_REQUIRED_KINDS",
    "SYSTEM_ACTOR_PREFIX",
    "VETOABLE_DECISIONS",
    "VETO_KINDS",
    "DeemedApproval",
    "DeemedTarget",
    "DuplicateDecisionError",
    "NotVetoableError",
    "ProposalRefMismatchError",
    "PullRequestLookupError",
    "PullRequestRef",
    "ReservedMatterError",
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
]


if __name__ == "__main__":  # pragma: no cover - CLI 実行パス
    raise SystemExit(main())
