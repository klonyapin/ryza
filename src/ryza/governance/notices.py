"""みなし承認の通知配線 — 記録と通知を不可分にする(定款 v0.4 第3条)。

``governance/decisions.py`` は承認・否認を **DB に書く** writer であり、
``bot/outbox.py`` は Discord へ **通知を出す** enqueue である。本モジュールはその2つを
1つのトランザクションに束ね、「通知されたが記録が無い」「記録はあるが通知されていない」
のどちらも起こらない状態を作る。

**なぜ束ねる必要があるか**: 定款 v0.4 第3条は、みなし承認が「``#承認`` への通知と同時に
発効する」と定め、``config/governance.yaml`` の ``deemed_approval.unnotified_change:
violation`` は通知なき発効を A-18 の無承認変更として扱う。したがって
「発効した(= decisions に deemed 行がある)が通知が出ていない」は定款違反そのものであり、
2つの書込を別トランザクションに置くことは、違反状態を作れる経路を残すことに等しい。
逆向き(通知だけ出て記録が無い)も、監査部門の ``deemed_ratio`` が実態より小さく出る
——形骸化アラートが鈍る——ため許容できない。

**原子性の実装**: 呼び出し側のトランザクションに参加し、内部は ``conn.transaction()``
(= SAVEPOINT)で包む。片方が失敗すれば SAVEPOINT まで巻き戻って**両方が消える**が、
呼び出し側のトランザクションは生き残る(呼び出し側が独自に書いた行を巻き添えにしない)。
本モジュールは commit しない —— コミット位置は呼び出し側(CLI・Bot)が決める。

**通知参照(``notice_ref``)の形式**: ``outbox:<press.outbox.id>``。Discord のメッセージ ID は
配送後にしか確定せず、記録と同一トランザクションでは取得できない。outbox の行 ID は
投入時点で確定し、配送後は ``press.outbox.sent_message_id`` から実メッセージ ID へ
解決できる(:func:`notice_message_id`)ため、不可分性を壊さずに通知を一意に指せる
唯一の参照になる。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import pq

from ryza import org
from ryza.bot import COLOR_APPROVAL, COLOR_FLASH, DISCLAIMER
from ryza.bot.approvals import KINDS, NotOwnerError, is_owner
from ryza.bot.outbox import enqueue
from ryza.governance import decisions as decisions_mod

log = logging.getLogger("ryza.governance.notices")

# 通知先の論理チャンネル(ryza.bot.CHANNELS)。
APPROVAL_CHANNEL = "approval"  # #承認: みなし承認の発効通知(定款第3条の発効要件)
OPS_CHANNEL = "ops"            # #運営: 否認に伴う取消義務・派生効果報告(第3条2号)

# 通知参照の接頭辞。``governance.decisions.channel_msg_id`` に入る。
NOTICE_REF_PREFIX = "outbox:"

# みなし承認 embed のフッターマーカー。配送時に否認ボタンを付ける判定に使う
# (``bot/approvals.py`` の ``proposal:`` と衝突しない別マーカー —— みなし承認は
#  既に発効済みであり、承認/却下ボタンを出すと二度目の決定を促してしまう)。
DEEMED_MARKER = "deemed:"

# bot/approvals.build_approval_embed のマーカー(あちらは保護領域なので参照のみ)。
# proposal_ref にこれが混ざると deemed 通知が承認 embed と誤認される(軽微-9)。
_APPROVAL_MARKER = "proposal:"

# 通知の発信者(config/org.yaml の役職キー)。既定は設計リード —— 保護領域 PR の
# みなし承認通知はこれまで設計リードが手動送信しており、その手順の機械化が本モジュールの
# 出自である(ops/reminders.yaml: governance-deemed-notice-wiring)。
DEFAULT_NOTICE_ROLE = "dev_lead"


class AtomicityError(RuntimeError):
    """記録と通知の原子性を保証できない接続で呼ばれた(autocommit 接続)。

    autocommit では各文が即時コミットされ、SAVEPOINT による巻き戻しが効かない。
    片方だけ永続化された状態(= 定款第3条違反、または deemed_ratio の過少計上)を
    作れてしまうため、書込を始める前に拒否する。
    """


class UnknownProposalError(ValueError):
    """否認・撤回の対象 ``proposal_ref`` に承認記録が無い。"""


class AlreadyVetoedError(ValueError):
    """既に否認されている決定をもう一度否認しようとした。

    追記オンリーの表(0021)は二重否認を物理的には許すが、ボタンの二度押しで
    ``#運営`` への取消義務リマインドが二重投稿され、否認件数(``deemed_ratio`` の分母
    ではなく分子側の統計)も二重計上される。現決定 view が既に ``vetoed`` を返す間は
    追記の意味が無いので弾く。
    """


class NotVetoedError(ValueError):
    """否認されていない決定の否認を撤回しようとした。"""


@dataclass(frozen=True)
class DeemedNotice:
    """みなし承認の記録+通知の結果。"""

    decision: decisions_mod.DeemedApproval
    outbox_id: int
    notice_ref: str


@dataclass(frozen=True)
class VetoNotice:
    """否認(または否認の撤回)の記録+通知の結果。"""

    veto: decisions_mod.Veto
    outbox_id: int
    proposal_ref: str


# ────────────────────────────────────────────────────────────────────────────
# トランザクション前提
# ────────────────────────────────────────────────────────────────────────────
def _require_shared_transaction(conn: psycopg.Connection) -> None:
    """呼び出し側のトランザクションに参加できる状態にする。

    psycopg3 の ``conn.transaction()`` は**最も外側なら exit 時に COMMIT する**。
    本モジュールは commit しない約束(コミット位置は呼び出し側が決める)なので、
    ブロックに入る前にトランザクションを開いておき、必ず SAVEPOINT として振る舞わせる。
    autocommit 接続は SAVEPOINT が成立しないため、書込前に拒否する。
    """
    if conn.autocommit:
        raise AtomicityError(
            "autocommit 接続では記録と通知の原子性を保証できない"
            "(片方だけ永続化された状態 = 定款第3条違反を作れてしまう)。"
            "autocommit=False の接続を渡し、呼び出し側で commit すること"
        )
    if conn.info.transaction_status == pq.TransactionStatus.IDLE:
        # 何も実行していない接続では transaction() が最外側になり COMMIT してしまう。
        # 無害な文でトランザクションを開き、以降の transaction() を SAVEPOINT にする。
        conn.execute("SELECT 1")


# ────────────────────────────────────────────────────────────────────────────
# embed の組立と復元
# ────────────────────────────────────────────────────────────────────────────
def build_deemed_notice_embed(
    proposal_ref: str,
    kind: str,
    notice: str,
    *,
    title: str | None = None,
    role: str = DEFAULT_NOTICE_ROLE,
) -> dict[str, Any]:
    """``#承認`` へ出すみなし承認の発効通知 embed を組み立てる。

    ``bot/approvals.build_approval_embed``(これから決めてもらう提案)と区別するため、
    フッターのマーカーを ``deemed:`` にする。配送側は承認/却下ボタンではなく
    **否認ボタン**を付ける —— みなし承認は通知の時点で既に発効しており、代表に残された
    操作は「いつでもできる否認」(第3条2号)だけだからである。
    """
    if kind not in KINDS:
        raise ValueError(f"未知の提案種別: {kind}")
    if not notice.strip():
        raise ValueError("notice(通知の要旨)は必須")
    if not proposal_ref.strip():
        raise ValueError("proposal_ref は必須(空文字不可)")
    # フッターは「最後のマーカー以降が参照」という規約で復元する。参照自体がマーカー文字列を
    # 含むと復元が壊れ、``proposal:`` を含む場合は承認 embed と誤認されて**承認/却下ボタンが
    # 付く**(軽微-9)。発効済みの提案に承認ボタンを出すのは誤操作の温床なので入口で弾く。
    for marker in (DEEMED_MARKER, _APPROVAL_MARKER):
        if marker in proposal_ref:
            raise ValueError(
                f"proposal_ref にマーカー文字列 '{marker}' を含められない"
                "(フッターからの参照復元が壊れる)"
            )
    return {
        "title": title or f"みなし承認が発効しました({kind})",
        "description": (
            f"{notice}\n\n"
            "本変更は本通知と同時に発効しています(定款第3条: みなし承認)。"
            "代表はいつでも否認でき、否認された変更は遅滞なく取り消されます。"
            "否認する場合は下の「否認」ボタンを押してください。"
        ),
        "color": COLOR_APPROVAL,
        "author": org.author_for_role(role),
        "fields": [
            {"name": "種別", "value": kind, "inline": True},
            {"name": "提案参照", "value": proposal_ref, "inline": True},
            {"name": "発効", "value": "通知と同時(みなし承認)", "inline": True},
        ],
        "footer": {"text": f"{DISCLAIMER} / {DEEMED_MARKER}{proposal_ref}"},
    }


def parse_deemed_notice(embed: dict[str, Any]) -> str | None:
    """みなし承認通知の embed から ``proposal_ref`` を復元する(通知でなければ None)。

    配送側(``bot/main.py``)が否認ボタンを付けるかの判定に使う。``parse_proposal`` と
    同じくフッターに埋めた参照を読む —— outbox の行は配送時に embed しか持たないため、
    ボタンが押されたときに「どの決定を否認するのか」を復元できるのはここだけになる。
    """
    footer_text = (embed.get("footer") or {}).get("text", "")
    idx = footer_text.rfind(DEEMED_MARKER)
    if idx < 0:
        return None
    ref = footer_text[idx + len(DEEMED_MARKER):].strip()
    return ref or None


@dataclass(frozen=True)
class DeemedViewTarget:
    """配送時の否認ボタン付与判定。``ref`` が None ならボタンを付けない。"""

    ref: str | None
    warning: str | None = None


def resolve_deemed_view(conn: psycopg.Connection, embed: dict[str, Any]) -> DeemedViewTarget:
    """embed が**実在する** deemed 決定の通知なら、その ``proposal_ref`` を返す。

    **なぜ DB を引くのか**: ``press.outbox`` へ enqueue できる主体なら誰でも ``deemed:``
    フッター付きの embed を ``#承認`` に出せ、任意の文字列を指す否認ボタンを代表に見せられる
    (独立役員審査 重要-4)。ボタンの押下先は ``proposal_ref`` だけで決まるので、偽の通知に
    本物の決定 ID を書けば、代表は「見覚えのない提案の否認」ではなく**別の提案の否認**を
    押させられる。フッターの自己申告を信じず、対応する ``deemed`` 決定の実在を配送時に確かめる。

    既に否認済みの決定にもボタンを付けない(押しても ``AlreadyVetoedError`` になるだけで、
    代表には「否認できるのにできない」ように見える)。撤回は ``/unveto`` にある。

    照合できない場合は ``ref=None`` と警告文を返す — **fail-closed**。ボタンが出ない不利益は
    ``/veto`` で埋められるが、偽の通知にボタンを付ける不利益は埋められない。
    """
    ref = parse_deemed_notice(embed)
    if ref is None:
        return DeemedViewTarget(None)
    try:
        row = decisions_mod.current_decision(conn, ref)
    except Exception as exc:  # noqa: BLE001 - 照合できないならボタンを付けない(fail-closed)
        return DeemedViewTarget(None, f"deemed 決定の照合に失敗したため否認ボタンを付けない: {exc}")
    if row is None:
        return DeemedViewTarget(
            None, f"deemed 決定が存在しない参照の通知(偽装の疑い): proposal_ref={ref}"
        )
    if row["recorded_decision"] != "deemed":
        return DeemedViewTarget(
            None,
            f"みなし承認でない決定を指す deemed 通知: proposal_ref={ref} "
            f"decision={row['recorded_decision']}",
        )
    if row["is_vetoed"]:
        return DeemedViewTarget(None, f"既に否認済みのため否認ボタンを付けない: proposal_ref={ref}")
    return DeemedViewTarget(ref)


def build_veto_notice_embed(
    proposal_ref: str,
    veto: decisions_mod.Veto,
    *,
    kind: str,
    role: str = DEFAULT_NOTICE_ROLE,
) -> dict[str, Any]:
    """``#運営`` へ出す否認の受領+取消義務リマインド embed(定款第3条2号)。

    否認は「止めた」だけでは完結しない。第3条は (1) 遅滞ない取消(git revert・設定の
    巻き戻し)と (2) 取消不能な派生効果の ``#運営`` への報告を義務付ける。義務の内容を
    通知本文に書き、報告先の追記 API(``record_revert_completion``)まで示すことで、
    否認から取消完了までが証跡として閉じる。
    """
    return {
        "title": "⛔ 否認: 取消義務が発生しました",
        "description": (
            "代表が承認決定を否認しました(定款第3条)。執行側は遅滞なく変更を取り消し"
            "(git revert・設定の巻き戻し)、取消不能な派生効果があれば一覧を本チャンネルへ"
            "報告してください。取消完了・派生効果の参照は "
            "`ryza.governance.decisions.record_revert_completion` で追記します。"
        ),
        "color": COLOR_FLASH,
        "author": org.author_for_role(role),
        "fields": [
            {"name": "提案参照", "value": proposal_ref, "inline": True},
            {"name": "種別", "value": kind, "inline": True},
            {"name": "否認者", "value": veto.vetoed_by, "inline": True},
            # 出所(0030)を本文に出す。オーナー検証は呼び出し側供給の 2 引数比較でしかなく、
            # DB も Discord も「本当に代表が押したか」を独立に知り得ない。代表本人が読む
            # チャンネルに経路を書けば、身に覚えのない経路からの否認をその場で気付ける。
            {"name": "出所", "value": veto.origin, "inline": True},
            {"name": "理由", "value": veto.reason[:1024], "inline": False},
        ],
        "footer": {"text": DISCLAIMER},
    }


def build_veto_withdrawal_embed(
    proposal_ref: str,
    veto: decisions_mod.Veto,
    *,
    kind: str,
    role: str = DEFAULT_NOTICE_ROLE,
) -> dict[str, Any]:
    """``#運営`` へ出す否認撤回の通知 embed。

    撤回は「取消義務が消えた」という執行側への指示変更であり、否認と同じ場所に同じ強度で
    出さないと、既に始まった取消作業が止まらない。
    """
    return {
        "title": "否認を撤回しました(取消義務は消滅)",
        "description": (
            "代表が否認そのものを撤回しました(定款第3条・0021 の ``withdrawal``)。"
            "当該決定は否認前の効力に戻ります。取消作業を開始していた場合は中止してください。"
        ),
        "color": COLOR_APPROVAL,
        "author": org.author_for_role(role),
        "fields": [
            {"name": "提案参照", "value": proposal_ref, "inline": True},
            {"name": "種別", "value": kind, "inline": True},
            {"name": "撤回者", "value": veto.vetoed_by, "inline": True},
            {"name": "出所", "value": veto.origin, "inline": True},
            {"name": "理由", "value": veto.reason[:1024], "inline": False},
        ],
        "footer": {"text": DISCLAIMER},
    }


# ────────────────────────────────────────────────────────────────────────────
# みなし承認: 記録 + 通知(同一トランザクション)
# ────────────────────────────────────────────────────────────────────────────
def announce_deemed_approval(
    conn: psycopg.Connection,
    proposal_ref: str,
    kind: str,
    notice: str,
    run_id: int,
    *,
    source: str = decisions_mod.DEFAULT_DEEMED_SOURCE,
    note: str | None = None,
    title: str | None = None,
    role: str = DEFAULT_NOTICE_ROLE,
    channel: str = APPROVAL_CHANNEL,
    reviewed_sha: str | None = None,
    review_ref: str | None = None,
) -> DeemedNotice:
    """``#承認`` への通知投入と ``deemed`` 行の記録を**同一トランザクション**で行う。

    Args:
        proposal_ref: 提案の一意参照(PR URL 等)。1提案=1決定の UNIQUE キー
        kind: 提案種別(``approvals.KINDS``)。3専決事項の kind は拒否される
        notice: 通知本文の要旨(何がなぜ発効したか)
        run_id: 投入元の Run(``press.outbox.run_id`` は NOT NULL)
        source: 発効源。``decided_by`` は ``'system:<source>'`` になる
        note / title / role / channel: 補足・見出し・発信者役職・通知先チャンネル
        reviewed_sha / review_ref: 審査対象コミットと独立役員審査の参照(0029)。
            承認記録の構造化列に入り、監査 A-18-8 が ``Approved:`` トレーラの
            ``reviewed=<sha40>`` と突合する。通知本文への表示は呼び出し側の責務
            (:func:`ryza.governance.decisions.build_pr_notice`)

    Raises:
        AtomicityError: autocommit 接続(原子性を保証できない)
        ValueError: 未知の kind、空の notice / proposal_ref
        ReservedMatterError: 3専決事項の kind(定款第3条)
        DuplicateDecisionError: 同 proposal_ref の決定が既にある

    通知を先に投入するのは、``notice_ref``(= ``outbox:<id>``)を承認記録に埋めるため。
    記録側が失敗すれば SAVEPOINT ごと巻き戻り、通知も残らない。
    """
    _require_shared_transaction(conn)
    # embed の組立と SHA 様式の検証は書込の前に済ませる(未知 kind・空 notice・様式不備の
    # reviewed_sha で outbox 行を作らない)。
    embed = build_deemed_notice_embed(proposal_ref, kind, notice, title=title, role=role)
    reviewed_sha = decisions_mod.normalize_reviewed_sha(reviewed_sha)
    with conn.transaction():
        outbox_id = enqueue(conn, channel, embed, run_id)
        notice_ref = f"{NOTICE_REF_PREFIX}{outbox_id}"
        decision = decisions_mod.record_deemed_approval(
            conn, proposal_ref, kind, notice_ref, source=source, note=note,
            reviewed_sha=reviewed_sha, review_ref=review_ref,
        )
    return DeemedNotice(decision=decision, outbox_id=outbox_id, notice_ref=notice_ref)


def notice_message_id(conn: psycopg.Connection, notice_ref: str) -> str | None:
    """``outbox:<id>`` 形式の通知参照から、配送済み Discord メッセージ ID を解決する。

    未配送(``sent_at IS NULL``)や別形式の参照では None。監査・ダッシュボードが
    「その通知は実際に届いたのか」を確認するための読み口であり、記録と通知の
    不可分性(=同一トランザクション)を壊さずに実メッセージへ辿る唯一の経路になる。
    """
    if not notice_ref.startswith(NOTICE_REF_PREFIX):
        return None
    raw = notice_ref[len(NOTICE_REF_PREFIX):]
    if not raw.isdigit():
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT sent_message_id FROM press.outbox WHERE id = %s", (int(raw),))
        row = cur.fetchone()
    return row[0] if row else None


# ────────────────────────────────────────────────────────────────────────────
# 否認・撤回: 記録 + 通知(同一トランザクション)
# ────────────────────────────────────────────────────────────────────────────
def record_denied_attempt(action: str, proposal_ref: str, actor: str) -> int | None:
    """非オーナーの否認操作の試行を、呼び出し側から独立した接続で ``#運営`` へ記録する。

    **なぜ別接続なのか**: 拒否は例外送出で終わり、呼び出し側はトランザクションを rollback
    する。同じ接続に警告を書けば拒否の痕跡ごと消える(独立役員審査 中-6)。オーナー検証は
    呼び出し側が渡す 2 引数の比較でしかなく、コード経路からの偽装は防げないため、**事後に
    痕跡が残ること**が実質的な防御になる。Run も自前で起こす(呼び出し側の Run は未コミットで
    あり得るので ``press.outbox.run_id`` の FK が満たせない)。

    記録に失敗しても拒否そのものは妨げない(ログのみ)。戻り値は投入した outbox id。
    """
    embed = {
        "title": "⚠️ 権限のない否認操作を拒否しました",
        "description": (
            "オーナー以外のユーザー(またはコード経路)が承認決定の否認を試みました。"
            "否認は代表の専権(定款第3条)であり、操作は記録されていません。"
        ),
        "color": COLOR_FLASH,
        "fields": [
            {"name": "操作", "value": action, "inline": True},
            {"name": "提案参照", "value": proposal_ref, "inline": True},
            {"name": "試行者", "value": actor, "inline": True},
        ],
        "footer": {"text": DISCLAIMER},
    }
    try:
        from ryza.db.conn import connect
        from ryza.provenance import start_run

        run = start_run(
            "governance.veto_denied", {"action": action, "proposal_ref": proposal_ref}
        )
        try:
            with connect(autocommit=True) as c:
                outbox_id = enqueue(c, OPS_CHANNEL, embed, run.run_id, urgent=True)
            run.finish("success")
        except Exception:
            run.finish("failed")
            raise
    except Exception:  # noqa: BLE001 - 記録の失敗で拒否を妨げない
        log.exception("非オーナーの否認試行を記録できなかった: %s %s", action, proposal_ref)
        return None
    return outbox_id


def _require_owner(action: str, proposal_ref: str, actor: str, owner_ids: Iterable[str]) -> None:
    """オーナー検証を **DB 読取より前** に行う(既存 killswitch/approvals と同じ順序)。

    権限の無い呼び出しに現決定を読ませない。読取は情報の露出であり、拒否するなら
    その前に拒否する(独立役員審査 中-6)。
    """
    if is_owner(actor, owner_ids):
        return
    record_denied_attempt(action, proposal_ref, actor)
    raise NotOwnerError(f"非オーナーの否認操作を拒否: user={actor}")


def _decision_row(conn: psycopg.Connection, proposal_ref: str) -> dict[str, Any]:
    row = decisions_mod.current_decision(conn, proposal_ref)
    if row is None:
        raise UnknownProposalError(
            f"proposal_ref='{proposal_ref}' の承認記録が無い(否認できるのは記録済みの決定のみ)"
        )
    return row


def apply_veto(
    conn: psycopg.Connection,
    proposal_ref: str,
    reason: str,
    *,
    vetoed_by: str,
    owner_ids: Iterable[str],
    run_id: int,
    origin: str,
    role: str = DEFAULT_NOTICE_ROLE,
    channel: str = OPS_CHANNEL,
) -> VetoNotice:
    """代表の否認を記録し、``#運営`` へ取消義務のリマインドを**同時に**投入する。

    ``proposal_ref`` から現決定を引いて ``decision_id`` を解決するため、押下側
    (Discord のボタン)は embed のフッターに埋めた参照だけを渡せばよい。
    ``expected_proposal_ref`` 照合は解決した ID に対してもう一度掛ける
    (0021 の対象取り違え防止をボタン経路でも通す)。

    ``origin`` は**必須**(:data:`ryza.governance.decisions.VETO_ORIGINS`)。ここで既定値を
    置くと、新しい呼び出し側が渡し忘れても黙って既定の経路として記録され、0030 が入れた
    「経路の一次識別」が働かなくなる。呼び出し側は自分がどの経路かを必ず宣言する。

    Raises:
        AtomicityError / UnknownProposalError / AlreadyVetoedError
        NotOwnerError: 非オーナーの否認操作(否認は代表の専権 —— 定款第3条)
        NotVetoableError: 対象が approve / deemed 以外
        ValueError: origin が語彙外
    """
    _require_owner("veto", proposal_ref, vetoed_by, owner_ids)
    _require_shared_transaction(conn)
    row = _decision_row(conn, proposal_ref)
    if row["is_vetoed"]:
        raise AlreadyVetoedError(
            f"proposal_ref='{proposal_ref}' は既に否認済み(veto_id={row['veto_id']})。"
            "取消の完了報告は record_revert_completion で追記する"
        )
    embed_kind = str(row["kind"])
    with conn.transaction():
        veto = decisions_mod.record_veto(
            conn, int(row["decision_id"]), reason,
            vetoed_by=vetoed_by, owner_ids=owner_ids,
            expected_proposal_ref=proposal_ref,
            # 出所(どの経路で否認が記録されたか)を事後に辿れるようにする。run_id だけでは
            # 足りない —— ボタン経路と /veto は同じ job_name で Run を開くため、meta.runs を
            # 辿っても両者は区別できない(0030)。run_id が答えるのは「どの実行の中で
            # 書かれたか」、origin が答えるのは「どの経路から書かれたか」である。
            origin=origin,
            run_id=run_id,
        )
        outbox_id = enqueue(
            conn, channel,
            build_veto_notice_embed(proposal_ref, veto, kind=embed_kind, role=role),
            run_id, urgent=True,  # 取消義務は時限つき(遅滞なく)—— 速報と同じ優先度で配送する
        )
    return VetoNotice(veto=veto, outbox_id=outbox_id, proposal_ref=proposal_ref)


def withdraw_veto(
    conn: psycopg.Connection,
    proposal_ref: str,
    reason: str,
    *,
    vetoed_by: str,
    owner_ids: Iterable[str],
    run_id: int,
    origin: str,
    role: str = DEFAULT_NOTICE_ROLE,
    channel: str = OPS_CHANNEL,
) -> VetoNotice:
    """否認そのものの撤回を記録し、``#運営`` へ通知する(誤った否認からの復旧)。

    ボタン1つで否認できる経路を作った以上、誤操作からの復旧経路も同じ強度で用意する
    (0021 独立役員審査 C-3: 撤回の表現が無いと ``UNIQUE(proposal_ref)`` により
    復旧手段が存在しない)。
    """
    _require_owner("veto_withdrawal", proposal_ref, vetoed_by, owner_ids)
    _require_shared_transaction(conn)
    row = _decision_row(conn, proposal_ref)
    if not row["is_vetoed"]:
        raise NotVetoedError(
            f"proposal_ref='{proposal_ref}' は否認されていない(撤回する否認が無い)"
        )
    embed_kind = str(row["kind"])
    with conn.transaction():
        veto = decisions_mod.record_veto_withdrawal(
            conn, int(row["decision_id"]), reason,
            vetoed_by=vetoed_by, owner_ids=owner_ids,
            expected_proposal_ref=proposal_ref,
            # 撤回にも出所を刻む。撤回は「取消義務を消す」操作であり、否認と同じだけ
            # 経路を問える必要がある(むしろ、身に覚えのない撤回のほうが危険である)。
            origin=origin,
            run_id=run_id,  # どの実行の中で書かれたか(重要-5 後段)
        )
        outbox_id = enqueue(
            conn, channel,
            build_veto_withdrawal_embed(proposal_ref, veto, kind=embed_kind, role=role),
            run_id, urgent=True,
        )
    return VetoNotice(veto=veto, outbox_id=outbox_id, proposal_ref=proposal_ref)


__all__ = [
    "APPROVAL_CHANNEL",
    "DEEMED_MARKER",
    "DEFAULT_NOTICE_ROLE",
    "NOTICE_REF_PREFIX",
    "OPS_CHANNEL",
    "AlreadyVetoedError",
    "AtomicityError",
    "DeemedNotice",
    "DeemedViewTarget",
    "NotVetoedError",
    "UnknownProposalError",
    "VetoNotice",
    "announce_deemed_approval",
    "apply_veto",
    "build_deemed_notice_embed",
    "build_veto_notice_embed",
    "build_veto_withdrawal_embed",
    "notice_message_id",
    "parse_deemed_notice",
    "record_denied_attempt",
    "resolve_deemed_view",
    "withdraw_veto",
]
