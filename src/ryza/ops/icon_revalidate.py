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
すり替えがその列に埋もれる(05-governance §6-5 の形骸化)。抑止の単位は失敗の**種別**
(例外型名)であって文言ではない(追補審査 C-16)。

**素通りできる二つの穴を塞いである**(独立役員 追補審査 2026-08-04):
* C-12 — 障害中のすり替え。失敗時も指紋を温存し、復旧時に必ず比較する。変化していれば
  通常色の「復旧」ではなく警告として出す。1日 404 を返してから差し替える手が通らない
* C-13 — URL ごとの差し替え。「URL が変わったのは代表の意図」を無検証で信じず、
  ``ops.org_icon_override_log``(追記オンリー)に**前回検査以降の**指示記録があるかを
  照合する。窓を「前回検査以降」に切ることで A→B→A の往復も古い記録では通らない
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

# 指示記録の見つからない URL 変更のうち、**緊急**として扱う出所(追補審査 C-13)。
# ``ledger`` = 新しい URL が台帳(config/org.yaml)の値と一致する場合で、台帳は DB からは
# 書けず git(PR + A-18)を通る。攻撃経路ではないので報告はするが緊急にはしない。
# ``unknown`` = 台帳にも指示記録にも対応が無い URL — DB に書ける主体しか作れない状態。
_URGENT_SOURCES = ("unknown",)


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

    @property
    def url_unverified(self) -> list[IconCheckEvent]:
        return [e for e in self.events if e.event == "url_unverified"]

    @property
    def urgent(self) -> bool:
        """緊急扱いにするか(追補審査 C-13)。

        指示記録の無い URL 変更のうち、台帳(git 経路)で説明できないものだけを緊急に
        する。DB に書ける主体しか作れない状態であり、検知機構そのものを迂回する操作である。
        """
        return any(
            (e.after or {}).get("source") in _URGENT_SOURCES for e in self.url_unverified
        )

    def as_runtime(self) -> dict[str, int]:
        """``meta.runs.params.runtime`` に残す件数(沈黙を多義的にしないため)。"""
        return {
            "checked": self.checked,
            "changed": len(self.changed),
            "errors": len(self.errors),
            "cleared": len(self.cleared),
            "url_unverified": len(self.url_unverified),
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
                   etag, last_modified, last_error, last_error_kind, override_log_id
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
                "last_error_kind": r[7],
                "override_log_id": r[8],
            }
            for r in cur.fetchall()
        }


def _has_fingerprint(fingerprint: org.IconFingerprint) -> bool:
    """一度でも成功観測があるか(失敗が先に記録された行は全項目 NULL になる)。"""
    return any(v is not None for v in fingerprint.as_dict().values())


def override_log_watermark(conn: Any) -> int:
    """``ops.org_icon_override_log`` の現在の最大 id(0 行なら 0)。"""
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(id), 0) FROM ops.org_icon_override_log")
        return int(cur.fetchone()[0])


def url_change_authorization(
    conn: Any, member_id: str, icon_url: str, watermark: int | None
) -> tuple[bool, str]:
    """URL 変更を指示した記録を探し ``(検証できたか, 出所)`` を返す(追補審査 C-13)。

    「URL が変わったのは代表がそう指示したから」を**無検証で信じない**。
    ``ops.org_icon_overrides`` に書ける主体(侵害されたジョブ・誤ったツール)は、URL を
    差し替えるだけで指紋比較を素通りできる。照合先は 0020 の追記オンリー台帳
    ``ops.org_icon_override_log`` で、こちらは改竄が禁じられている。

    **前回検査より後に積まれた記録だけを認める**のが要点である。「過去のどこかに同じ URL の
    set があればよい」とすると、A→B→A の往復(いったん別 URL を経由して元へ戻す)が古い
    記録で正当化されてしまう。順序は時刻ではなく id で見る — ``now()`` はトランザクション
    開始時刻で固定されるため、同一トランザクション内の前後関係を表せない。

    台帳(``config/org.yaml``)の値へ戻る変更は ``reset`` の記録でも認める。台帳そのものの
    書き換えは git(PR + A-18)の経路で DB からは触れないため、記録が無い場合の出所
    ``ledger`` は警告はするが緊急にはしない(``_URGENT_SOURCES``)。
    """
    if watermark is None:
        return True, "initial"
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM ops.org_icon_override_log
            WHERE member_id = %s AND action = 'set' AND icon_url = %s AND id > %s
            LIMIT 1
            """,
            (member_id, icon_url, watermark),
        )
        if cur.fetchone() is not None:
            return True, "override_log"
        cur.execute(
            """
            SELECT 1 FROM ops.org_icon_override_log
            WHERE member_id = %s AND action = 'reset' AND id > %s
            LIMIT 1
            """,
            (member_id, watermark),
        )
        reset_seen = cur.fetchone() is not None
    ledger = org.members().get(member_id)
    is_ledger_value = ledger is not None and ledger.icon_url == icon_url
    if reset_seen and is_ledger_value:
        return True, "override_log(reset)"
    if is_ledger_value:
        return False, "ledger"
    return False, "unknown"


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
                 first_seen_at, last_checked_at, last_error, last_error_kind, override_log_id)
            VALUES (%s, %s, %s, %s, %s, %s, now(), now(), NULL, NULL,
                    (SELECT coalesce(max(id), 0) FROM ops.org_icon_override_log))
            ON CONFLICT (member_id) DO UPDATE SET
                icon_url = EXCLUDED.icon_url,
                content_type = EXCLUDED.content_type,
                content_length = EXCLUDED.content_length,
                etag = EXCLUDED.etag,
                last_modified = EXCLUDED.last_modified,
                first_seen_at = CASE
                    WHEN %s THEN now() ELSE ops.org_icon_checks.first_seen_at END,
                last_checked_at = now(),
                last_error = NULL,
                last_error_kind = NULL,
                override_log_id = EXCLUDED.override_log_id
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


def _record_error(
    conn: Any, member_id: str, icon_url: str, detail: str, kind: str
) -> None:
    """検査失敗を現在値へ反映する。**指紋は消さない** — 復旧時の比較基準に要る。

    温存した指紋は飾りではない: 復旧時に必ず比較し、障害中に中身が差し替わっていれば
    「復旧」ではなく「変化」として報告する(追補審査 C-12)。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.org_icon_checks
                (member_id, icon_url, last_checked_at, last_error, last_error_kind,
                 override_log_id)
            VALUES (%s, %s, now(), %s, %s,
                    (SELECT coalesce(max(id), 0) FROM ops.org_icon_override_log))
            ON CONFLICT (member_id) DO UPDATE SET
                icon_url = EXCLUDED.icon_url,
                last_checked_at = now(),
                last_error = EXCLUDED.last_error,
                last_error_kind = EXCLUDED.last_error_kind,
                override_log_id = EXCLUDED.override_log_id
            """,
            (member_id, icon_url, detail, kind),
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

    **URL 自体の変更は「変化」ではなく再基準化として扱う**が、無検証では受け入れない。
    それが代表の意図的な差し替えなら ``ops.org_icon_override_log``(追記オンリー)に記録が
    あるはずで、記録が見つからない URL 変更は ``url_unverified`` として報告する
    (追補審査 C-13)。検知したい「中身のすり替え」は、URL を差し替えるだけで指紋比較を
    素通りできてしまうため、URL の同一性そのものを統制対象に含める必要がある。
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
            kind = type(exc).__name__
            detail = f"{kind}: {exc}"
            # 同じ**種別**の失敗が続く間は毎日イベントを積まない(追補審査 C-16)。
            # 文言の完全一致で抑止すると、理由にリクエスト ID や時刻を混ぜる配信元では
            # 毎日イベント化して読まれなくなる(形骸化)。文言は last_error に残る。
            if base is None or base["last_error_kind"] != kind:
                event = IconCheckEvent(
                    member_id=member_id, event="error", icon_url=icon_url, detail=detail
                )
                _record_event(conn, event)
                result.events.append(event)
            _record_error(conn, member_id, icon_url, detail, kind)
            continue

        url_moved = base is not None and base["icon_url"] != icon_url
        if url_moved:
            verified, source = url_change_authorization(
                conn, member_id, icon_url, base["override_log_id"]
            )
            if not verified:
                event = IconCheckEvent(
                    member_id=member_id,
                    event="url_unverified",
                    icon_url=icon_url,
                    before={"icon_url": base["icon_url"]},
                    after={"icon_url": icon_url, "source": source},
                    detail=(
                        f"URL が変わったが、それを指示した記録が見つからない(出所: {source})。"
                        f"変更前: {base['icon_url']}"
                    ),
                )
                _record_event(conn, event)
                result.events.append(event)

        # **障害中のすり替えを「復旧」に化けさせない**(追補審査 C-12)。失敗時も指紋を
        # 温存しているのだから、復旧時には必ずそれと比較する。比較を省くと、1日わざと
        # 404 を返してから差し替えるだけで通常色の cleared 1通に化ける。
        changed = (
            base is not None
            and not url_moved
            and _has_fingerprint(base["fingerprint"])
            and base["fingerprint"] != fingerprint
        )
        if base is not None and base["last_error"]:
            detail = (
                "復旧したが**内容が変わっている**(障害中に差し替えられた疑い)"
                if changed
                else f"復旧(直前の失敗: {base['last_error']})"
            )
            event = IconCheckEvent(
                member_id=member_id,
                event="cleared",
                icon_url=icon_url,
                after=fingerprint.as_dict(),
                detail=detail,
            )
            _record_event(conn, event)
            result.events.append(event)
        if changed:
            event = IconCheckEvent(
                member_id=member_id,
                event="changed",
                icon_url=icon_url,
                before=base["fingerprint"].as_dict(),
                after=fingerprint.as_dict(),
                detail="障害からの復旧時に検知" if base["last_error"] else None,
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
    for e in result.url_unverified:
        fields.append(
            {
                "name": f"⚠ 指示記録の無い URL 変更: {e.member_id}",
                "value": f"{e.detail or ''}\n変更後: {e.icon_url}",
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
        "color": COLOR_FLASH if (result.changed or result.url_unverified) else COLOR_NORMAL,
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
        enqueue(
            conn, OPS_CHANNEL, build_report_embed(result), run_id, urgent=result.urgent
        )
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
