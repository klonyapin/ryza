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
(独立役員審査 0019 C-3)。承認・否認は監査 A-13-1 の ``Approved:`` トレーラ突合の
参照先そのものなので、書き込みの形式(decided_by の表記・3専決の除外・冪等性)を
散らさず1箇所に閉じる。

**二重の検証**: 3専決事項(定款第3条)への ``'deemed'`` はスキーマの CHECK が拒否するが、
本モジュールでも INSERT 前に弾く。理由は2つ — (1) CheckViolation はどの制約に触れたか
呼び出し側に伝わりにくく、運用時に「なぜ通知が失敗したか」の切り分けが遅れる、
(2) CheckViolation はトランザクションを中断させるため、通知と同一トランザクションで
記録する設計(第3条: 通知を欠いた発効は A-13 の無承認変更)では通知側の書込も巻き添えに
なる。**スキーマ側の CHECK が一次統制であり、本モジュールの検証はその代替ではない**
(アプリ検証だけに寄せると、別経路の INSERT で穴が開く)。

呼び出し側でトランザクションを制御する(本モジュールは commit しない)。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import psycopg

from ryza.bot.approvals import KINDS, NotOwnerError, is_owner

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
) -> DeemedApproval:
    """みなし承認を ``governance.decisions`` に ``decision='deemed'`` で記録する。

    Args:
        proposal_ref: 提案の一意参照(PR URL 等)。UNIQUE で二重記録を防ぐ
        kind: 提案種別(``approvals.KINDS``)。3専決の kind は拒否する
        notice_ref: ``#承認`` へ投稿した通知の参照(メッセージ ID / URL)。
            **必須** — 定款第3条は通知を発効要件とし、通知を欠いた発効は
            A-13 の無承認変更にあたる。``channel_msg_id`` 列に記録する
        source: 発効源。``decided_by`` は ``'system:<source>'`` になる
        note: 補足(任意)

    Raises:
        ValueError: 未知の kind、または proposal_ref / notice_ref / source が空
        ReservedMatterError: 3専決事項の kind(定款第3条)
        DuplicateDecisionError: 同 proposal_ref の決定が既にある

    本関数は **通知の送信そのものは行わない**。呼び出し側が通知の投入(press.outbox)と
    本記録を同一トランザクションに置くことで、「通知されたが記録が無い」「記録は
    あるが通知されていない」のどちらも起こらないようにする(定款第3条3号)。
    """
    _require_text(proposal_ref, "proposal_ref")
    _require_text(notice_ref, "notice_ref")
    _require_text(source, "source")
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
                    (proposal_ref, kind, decision, decided_by, note, channel_msg_id)
                VALUES (%s, %s, 'deemed', %s, %s, %s)
                RETURNING id
                """,
                (proposal_ref, kind, decided_by, note, notice_ref),
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
    )


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


def current_decision(
    conn: psycopg.Connection, proposal_ref: str
) -> dict[str, object] | None:
    """現決定(``governance.current_decisions`` view — 0021)を1件読む。

    承認記録を読むコードは ``governance.decisions`` を直接読まず本関数を使う。
    否認された決定の ``effective_decision`` は ``'vetoed'`` になるため、
    「承認済み」として扱う分岐に否認済みの決定が紛れ込まない。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT decision_id, proposal_ref, kind, recorded_decision,
                   effective_decision, is_vetoed, decided_by, decided_at,
                   veto_id, veto_kind, vetoed_by, veto_reason, revert_commit,
                   derived_effects_ref, vetoed_at
            FROM governance.current_decisions
            WHERE proposal_ref = %s
            """,
            (proposal_ref,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [d.name for d in cur.description]
    return dict(zip(columns, row, strict=True))


__all__ = [
    "DEFAULT_DEEMED_SOURCE",
    "RESERVED_KINDS",
    "RESERVED_KIND_BY_MATTER",
    "SYSTEM_ACTOR_PREFIX",
    "VETOABLE_DECISIONS",
    "VETO_KINDS",
    "DeemedApproval",
    "DuplicateDecisionError",
    "NotVetoableError",
    "ProposalRefMismatchError",
    "ReservedMatterError",
    "Veto",
    "current_decision",
    "record_deemed_approval",
    "record_revert_completion",
    "record_veto",
    "record_veto_withdrawal",
]
