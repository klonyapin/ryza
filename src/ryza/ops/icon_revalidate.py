"""アイコン URL の定期再検証(0033 — 独立役員審査 0020 C-7 の検知側是正)。

キャラクターのアイコンは**外部 URL のホットリンク**である(``config/org.yaml``、および
0020 の実行時上書き)。0020 は保存時に1度だけ URL を検証するが、それは保存時点の
スナップショットでしかなく、URL 先の画像が後から差し替えられても気づけない(C-7)。

**なぜ再ホストではなく再検証なのか**: 再配布の法的整理の結論が「再ホストしない」だから
である。Discord の embed アイコンは Discord 側がサーバから取得するため、自前ストレージへ
複製する構成は必ず公開 URL を伴い、第三者が著作権を持つ画像の送信可能化になる。全文の
根拠と代替案の比較は ``docs/research/icon-hosting-legal.md``。防止が採れない以上、
C-7 は**検知**で扱う(``ops/reminders.yaml`` icon-rehost-storage が明示的に許容した代替)。

**この検知の限界を先に書く**: 指紋は HEAD 応答ヘッダであり、全て配信元の自己申告である。
UA や IP で応答を出し分ける配信元(クローキング)には無力で、捕まえられるのは誠実な
配信元による差し替えだけである。独立役員審査 0020 C-6 が「単発 HEAD で誠実な誤りしか
防げない」と述べた限界は、周期的にしても消えない。受容した残存リスクである。

**通知は遷移でだけ出す**: 毎日走らせても、変化・失敗・復旧の**瞬間**にしか #運営 へ
投稿しない。到達不能な URL が毎日同じ警告を出し続けると読まれなくなり、本物の
すり替えがその列に埋もれる(05-governance §6-5 の形骸化)。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from psycopg.types.json import Jsonb

from ryza import org
from ryza.bot import COLOR_FLASH, COLOR_NORMAL, DISCLAIMER
from ryza.bot.outbox import enqueue

log = logging.getLogger("ryza.ops.icon_revalidate")

# 通知先(#運営)。速報チャンネルは使わない — 運用の異常であり相場の速報ではない。
OPS_CHANNEL = "ops"

# 報告の発信者。監査部門(ターニャ)— A-18 の週次警告と同じ発信者に揃え、
# 「統制からの報告」を読み手が1つのキャラクターとして認識できるようにする。
REPORT_ROLE = "audit"

# 指紋を取る関数(URL → IconFingerprint。失敗は org.IconUrlError)。テストで差し替える。
Prober = Callable[[str], org.IconFingerprint]


@dataclass(frozen=True)
class IconCheckEvent:
    """検知した遷移1件(``ops.org_icon_check_events`` の1行に対応)。"""

    member_id: str
    event: str  # changed | error | cleared
    icon_url: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    detail: str | None = None


@dataclass
class RevalidationResult:
    """1回の再検証の結果。"""

    checked: int = 0
    events: list[IconCheckEvent] = field(default_factory=list)

    @property
    def changed(self) -> list[IconCheckEvent]:
        return [e for e in self.events if e.event == "changed"]

    @property
    def errors(self) -> list[IconCheckEvent]:
        return [e for e in self.events if e.event == "error"]

    @property
    def cleared(self) -> list[IconCheckEvent]:
        return [e for e in self.events if e.event == "cleared"]

    def as_runtime(self) -> dict[str, int]:
        """``meta.runs.params.runtime`` に残す件数(沈黙を多義的にしないため)。"""
        return {
            "checked": self.checked,
            "changed": len(self.changed),
            "errors": len(self.errors),
            "cleared": len(self.cleared),
        }


# ────────────────────────────────────────────────────────────────────────────
# DB 入出力
# ────────────────────────────────────────────────────────────────────────────
def load_baselines(conn: Any) -> dict[str, dict[str, Any]]:
    """``ops.org_icon_checks`` の現在値(member_id → 指紋+URL+直近エラー)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT member_id, icon_url, content_type, content_length,
                   etag, last_modified, last_error
            FROM ops.org_icon_checks
            """
        )
        return {
            r[0]: {
                "icon_url": r[1],
                "fingerprint": org.IconFingerprint(
                    content_type=r[2], content_length=r[3], etag=r[4], last_modified=r[5]
                ),
                "last_error": r[6],
            }
            for r in cur.fetchall()
        }


def _record_ok(
    conn: Any,
    member_id: str,
    icon_url: str,
    fingerprint: org.IconFingerprint,
    *,
    rebaseline: bool,
) -> None:
    """検査成功を現在値へ反映する(``rebaseline`` なら ``first_seen_at`` も更新)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.org_icon_checks
                (member_id, icon_url, content_type, content_length, etag, last_modified,
                 first_seen_at, last_checked_at, last_error)
            VALUES (%s, %s, %s, %s, %s, %s, now(), now(), NULL)
            ON CONFLICT (member_id) DO UPDATE SET
                icon_url = EXCLUDED.icon_url,
                content_type = EXCLUDED.content_type,
                content_length = EXCLUDED.content_length,
                etag = EXCLUDED.etag,
                last_modified = EXCLUDED.last_modified,
                first_seen_at = CASE
                    WHEN %s THEN now() ELSE ops.org_icon_checks.first_seen_at END,
                last_checked_at = now(),
                last_error = NULL
            """,
            (
                member_id,
                icon_url,
                fingerprint.content_type,
                fingerprint.content_length,
                fingerprint.etag,
                fingerprint.last_modified,
                rebaseline,
            ),
        )


def _record_error(conn: Any, member_id: str, icon_url: str, detail: str) -> None:
    """検査失敗を現在値へ反映する。**指紋は消さない** — 復旧時の比較基準に要る。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.org_icon_checks (member_id, icon_url, last_checked_at, last_error)
            VALUES (%s, %s, now(), %s)
            ON CONFLICT (member_id) DO UPDATE SET
                icon_url = EXCLUDED.icon_url,
                last_checked_at = now(),
                last_error = EXCLUDED.last_error
            """,
            (member_id, icon_url, detail),
        )


def _record_event(conn: Any, event: IconCheckEvent) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.org_icon_check_events
                (member_id, event, icon_url, before_json, after_json, detail)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                event.member_id,
                event.event,
                event.icon_url,
                Jsonb(event.before) if event.before is not None else None,
                Jsonb(event.after) if event.after is not None else None,
                event.detail,
            ),
        )


# ────────────────────────────────────────────────────────────────────────────
# 再検証本体
# ────────────────────────────────────────────────────────────────────────────
def revalidate(
    conn: Any,
    *,
    prober: Prober | None = None,
    urls: dict[str, str] | None = None,
) -> RevalidationResult:
    """全メンバーの実効アイコン URL を検査し、遷移を記録して結果を返す(commit しない)。

    ``urls`` を渡さなければ ``org.effective_members(conn)``(台帳に 0020 の上書きを
    重ねた実効値)から取る。テストは ``urls`` と ``prober`` を差し替えて実ネットワークを
    叩かない。

    **URL 自体が変わっていたら変化として扱わない**。それは代表の意図的な差し替え
    (``ops.org_icon_override_log`` に既に残っている)か台帳の更新であり、すり替えでは
    ない。新しい URL を新しい基準として取り直す(re-baseline)。検知したいのは
    「同じ URL の中身が変わった」ことだけである。
    """
    probe = prober if prober is not None else org.probe_icon_url
    if urls is None:
        urls = {m.id: m.icon_url for m in org.effective_members(conn).values()}
    baselines = load_baselines(conn)
    result = RevalidationResult()

    for member_id in sorted(urls):
        icon_url = urls[member_id]
        base = baselines.get(member_id)
        result.checked += 1
        try:
            fingerprint = probe(icon_url)
        except Exception as exc:  # noqa: BLE001 - 到達不能・非画像は所見であって障害ではない
            detail = f"{type(exc).__name__}: {exc}"
            # 同じ失敗が続く間は毎日イベントを積まない(形骸化の防止)。
            if base is None or base["last_error"] != detail:
                event = IconCheckEvent(
                    member_id=member_id, event="error", icon_url=icon_url, detail=detail
                )
                _record_event(conn, event)
                result.events.append(event)
            _record_error(conn, member_id, icon_url, detail)
            continue

        url_moved = base is not None and base["icon_url"] != icon_url
        if base is not None and base["last_error"]:
            event = IconCheckEvent(
                member_id=member_id,
                event="cleared",
                icon_url=icon_url,
                after=fingerprint.as_dict(),
                detail=f"復旧(直前の失敗: {base['last_error']})",
            )
            _record_event(conn, event)
            result.events.append(event)
        changed = (
            base is not None
            and not url_moved
            and not base["last_error"]
            and base["fingerprint"] != fingerprint
        )
        if changed:
            event = IconCheckEvent(
                member_id=member_id,
                event="changed",
                icon_url=icon_url,
                before=base["fingerprint"].as_dict(),
                after=fingerprint.as_dict(),
            )
            _record_event(conn, event)
            result.events.append(event)
        _record_ok(conn, member_id, icon_url, fingerprint, rebaseline=changed or url_moved)

    return result


# ────────────────────────────────────────────────────────────────────────────
# 報告(#運営)
# ────────────────────────────────────────────────────────────────────────────
def _fingerprint_line(fp: dict[str, Any] | None) -> str:
    if not fp:
        return "(なし)"
    size = fp.get("content_length")
    return (
        f"{fp.get('content_type') or '?'} / "
        f"{f'{size:,}B' if isinstance(size, int) else '?'} / "
        f"etag={fp.get('etag') or '-'}"
    )


def build_report_embed(result: RevalidationResult) -> dict[str, Any]:
    """遷移の報告 embed。**呼び出し側が「遷移があるときだけ」使う**。"""
    fields: list[dict[str, Any]] = []
    for e in result.changed:
        fields.append(
            {
                "name": f"⚠ 差し替えの疑い: {e.member_id}",
                "value": (
                    f"{e.icon_url}\n"
                    f"変更前: {_fingerprint_line(e.before)}\n"
                    f"変更後: {_fingerprint_line(e.after)}"
                ),
                "inline": False,
            }
        )
    for e in result.errors:
        fields.append(
            {
                "name": f"✖ 検査失敗: {e.member_id}",
                "value": f"{e.icon_url}\n{e.detail or ''}",
                "inline": False,
            }
        )
    for e in result.cleared:
        fields.append(
            {
                "name": f"✅ 復旧: {e.member_id}",
                "value": f"{e.icon_url}\n{e.detail or ''}",
                "inline": False,
            }
        )
    return {
        "title": "アイコン再検証",
        "description": (
            f"実効アイコン URL {result.checked} 件を検査し、{len(result.events)} 件の遷移を"
            "検知した。指紋は配信元が名乗る HEAD ヘッダで、応答を出し分ける配信元には"
            "無力である(限界は docs/research/icon-hosting-legal.md)。"
        ),
        "color": COLOR_FLASH if result.changed else COLOR_NORMAL,
        "fields": fields,
        "author": org.author_for_role(REPORT_ROLE),
        "footer": {"text": DISCLAIMER},
    }


def run_revalidation(
    conn: Any,
    run_id: int,
    *,
    prober: Prober | None = None,
    urls: dict[str, str] | None = None,
) -> RevalidationResult:
    """再検証を行い、遷移があれば #運営 へ1通 enqueue する(commit は呼び出し側)。

    遷移が無ければ**投稿しない**。それでも実行の事実は ``meta.runs`` に残り、日報の
    「本日のジョブ実行」に数えられるため、「静かなのは走っていないから」と「静かなのは
    変化が無いから」は区別できる。
    """
    result = revalidate(conn, prober=prober, urls=urls)
    if result.events:
        enqueue(conn, OPS_CHANNEL, build_report_embed(result), run_id)
    return result


def main() -> None:
    """手動実行の入口(``python -m ryza.ops.icon_revalidate``)。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from ryza.db.conn import connect
    from ryza.provenance import start_run

    with connect() as conn:
        r = start_run("ops.icon_revalidate", conn=conn)
        result = run_revalidation(conn, r.run_id)
        r.record_runtime(result.as_runtime())
        r.finish("success")
        conn.commit()
    log.info("アイコン再検証: %s", result.as_runtime())


if __name__ == "__main__":
    main()
