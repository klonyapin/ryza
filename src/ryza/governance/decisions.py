"""承認記録の writer(みなし承認・事後否認)— 定款 v0.4 第3条。

``src/ryza/bot/approvals.py`` が扱うのは**代表がボタンを押した明示の決定**
(approve/reject/question)である。本モジュールはその対になる2つの記録を扱う:

- :func:`record_deemed_approval` … みなし承認(``decision='deemed'``)。``#承認`` への
  PR リンク付き通知と同時に発効する(第3条2号)。押下者は存在せず、``decided_by`` は
  ``'system:<source>'``(0019 の ``decisions_deemed_system_actor_check``)
- :func:`record_veto` … 代表による事後否認(``governance.decision_vetoes`` — 0021)。
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

from dataclasses import dataclass

import psycopg

from ryza.bot.approvals import KINDS

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
    """記録された事後否認。"""

    veto_id: int
    decision_id: int
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


def record_veto(
    conn: psycopg.Connection,
    decision_id: int,
    reason: str,
    *,
    vetoed_by: str,
    revert_commit: str | None = None,
    derived_effects_ref: str | None = None,
    run_id: int | None = None,
) -> Veto:
    """代表による事後否認を ``governance.decision_vetoes`` に追記する(0021)。

    Args:
        decision_id: 否認対象 ``governance.decisions.id``
        reason: 否認理由(必須)。執行側が何を巻き戻すかの起点になる
        vetoed_by: 否認者(オーナー検証済みの Discord ユーザー ID)。
            オーナー検証は呼び出し側 (``approvals.is_owner``) の責務 — 本モジュールは
            オーナー ID の集合を知らない
        revert_commit: 取消(git revert・設定巻き戻し)のコミット SHA。否認時点で
            未確定なら省略し、確定後に同じ ``decision_id`` へもう一度呼ぶ
            (追記オンリーのため UPDATE できない。現決定 view は最新行を採る)
        derived_effects_ref: 取消不能な派生効果一覧の参照(``#運営`` への報告)
        run_id: 記録したジョブ実行(``meta.runs``)。Discord 経路など Run を持たない
            場合は省略できる — 「Run が無いから否認できない」を作らないため

    Raises:
        ValueError: reason / vetoed_by が空、または decision_id が存在しない

    決定の種別は問わない(明示承認も否認できる — 0021 の設計判断)。
    """
    _require_text(reason, "reason")
    _require_text(vetoed_by, "vetoed_by")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM governance.decisions WHERE id = %s", (decision_id,)
        )
        if cur.fetchone() is None:
            raise ValueError(f"否認対象の決定 id={decision_id} が存在しない")
        cur.execute(
            """
            INSERT INTO governance.decision_vetoes
                (decision_id, vetoed_by, reason, revert_commit, derived_effects_ref, run_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING veto_id
            """,
            (decision_id, vetoed_by, reason, revert_commit, derived_effects_ref, run_id),
        )
        veto_id = cur.fetchone()[0]
    return Veto(
        veto_id=veto_id,
        decision_id=decision_id,
        vetoed_by=vetoed_by,
        reason=reason,
        revert_commit=revert_commit,
        derived_effects_ref=derived_effects_ref,
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
                   veto_id, vetoed_by, veto_reason, revert_commit,
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
    "DeemedApproval",
    "DuplicateDecisionError",
    "ReservedMatterError",
    "Veto",
    "current_decision",
    "record_deemed_approval",
    "record_veto",
]
