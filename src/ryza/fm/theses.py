"""theses — FM の提案記録 ``trading.fm_theses`` と証憑の point-in-time 検証(T-017)。

役割は4つ:

1. **記録の唯一の入口** ``record_thesis``: 反証条件(invalidation)と証憑(evidence_refs)を
   欠いた提案を保存させない。スキーマ側の CHECK と二重の防御にするのは、アプリ層でしか
   検証できない **point-in-time**(証憑が as_of 以前か)をここで併せて弾くため
2. **point-in-time 検証** ``validate_evidence_refs``: 証憑1件ごとに参照先の ``as_of`` を
   引き、判断時点より新しい証憑を拒否する。未来情報の混入はバグではなく設計違反
   (不変原則4)。「存在しない証憑」と「as_of 超の証憑」は別メッセージで区別する
3. **判断履歴の注入** ``recent_theses``: FM 別・新しい順に、**ゲート判定の結果つき**で
   読み出す(orders.thesis_id → orders.status / compliance.gate_log)。block された案が
   次回プロンプトの学習材料になる(指示書6・7。governance.stances と同じ思想)
4. **検疫** ``quarantine_thesis``: 汚染が判明した提案を再注入の対象から外す
   (``trading.fm_theses_quarantine`` — 追記オンリーと両立する封じ込め。fm_theses は
   書き換えず、読出し側 3 が除外する。独立役員審査 T-017 C-3)。運用の入口は本モジュールの
   CLI(``python -m ryza.fm.theses --quarantine <id> --reason ...``)で、手順の正は
   ``docs/ops/fm-quarantine-runbook.md``

証憑参照(evidence_refs)の語彙 — いずれも ``kind`` で分岐する JSON オブジェクト:

- ``{"kind": "document", "doc_id": 12}``                       … docs.documents
- ``{"kind": "research_report", "report_id": 3}``              … docs.research_reports
- ``{"kind": "bar", "instrument_id": 1, "timeframe": "1d", "ts": "..."}`` … market.bars
- ``{"kind": "indicator", "series_code": "JP_CPI", "ts": "..."}``         … market.indicators

**direction は既定で long-only**(buy / close)。short を通すには ``allow_short=True`` を
明示する必要があり、第一陣の生成経路はこれを渡さない(テストで固定 — モジュール
``ryza.fm`` の docstring)。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

# スキーマ(0018)の direction 語彙と、第一陣が生成してよい部分集合。
DIRECTIONS = ("buy", "close", "short")
LONG_ONLY_DIRECTIONS = ("buy", "close")

# 証憑参照の kind 語彙。
EVIDENCE_KINDS = ("document", "research_report", "bar", "indicator")


class ThesisError(ValueError):
    """提案の記録を拒否する(反証条件・証憑・direction の不備)。"""


class EvidenceError(ThesisError):
    """証憑参照が不正(欠落・存在しない・as_of 超)。違反を全件列挙する。"""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        joined = "\n".join(f"- {p}" for p in problems)
        super().__init__(f"証憑参照が不正({len(problems)}件):\n{joined}")


@dataclass(frozen=True)
class ThesisRecord:
    """``trading.fm_theses`` の1行 + そこから出た注文のゲート判定(あれば)。"""

    thesis_id: int
    fm: str
    instrument_id: int
    direction: str
    thesis_md: str
    invalidation_md: str
    as_of: datetime
    order_status: str | None = None  # passed|blocked|filled|... (注文が無ければ None)
    gate_verdict: str | None = None  # pass|warn|block
    gate_reasons: list[dict[str, Any]] | None = None


# ── point-in-time 証憑検証 ────────────────────────────────────────────────────
def _ref_lookup(ref: dict[str, Any]) -> tuple[str, tuple[Any, ...], str]:
    """証憑参照 → (SQL, パラメータ, 表示名)。語彙違反は ``ThesisError``。

    SQL はいずれも **2列**を返す(行が無ければ NULL):

    1. 参照先の最小 ``as_of``(知り得た時点)。最小を採るのは改定(indicators の revision)・
       再取得(bars の as_of 複数)で最も早く知り得た時点を point-in-time の基準にするため
    2. 参照先の最大 ``ts``(**対象時点** — bar / indicator のみ。文書系は NULL)。
       ts は WHERE の等値条件でもあるが、比較を DB 側に寄せて timestamptz として返させる
       のは、文字列の ts をアプリ側でパースして tz 有無を取り違えるのを避けるため
       (独立役員審査 T-017 C-6)
    """
    kind = ref.get("kind")
    if kind == "document":
        doc_id = _require_int(ref, "doc_id")
        return (
            "SELECT min(as_of), NULL::timestamptz FROM docs.documents WHERE doc_id = %s",
            (doc_id,),
            f"document(doc_id={doc_id})",
        )
    if kind == "research_report":
        report_id = _require_int(ref, "report_id")
        return (
            "SELECT min(as_of), NULL::timestamptz FROM docs.research_reports "
            "WHERE report_id = %s",
            (report_id,),
            f"research_report(report_id={report_id})",
        )
    if kind == "bar":
        instrument_id = _require_int(ref, "instrument_id")
        timeframe = str(ref.get("timeframe") or "1d")
        ts = ref.get("ts")
        if ts is None:
            raise ThesisError(f"証憑参照 bar に ts が無い: {ref!r}")
        return (
            "SELECT min(as_of), max(ts) FROM market.bars "
            "WHERE instrument_id = %s AND timeframe = %s AND ts = %s",
            (instrument_id, timeframe, ts),
            f"bar(instrument_id={instrument_id}, ts={ts})",
        )
    if kind == "indicator":
        series_code = ref.get("series_code")
        ts = ref.get("ts")
        if not series_code or ts is None:
            raise ThesisError(f"証憑参照 indicator に series_code/ts が無い: {ref!r}")
        return (
            "SELECT min(as_of), max(ts) FROM market.indicators "
            "WHERE series_code = %s AND ts = %s",
            (str(series_code), ts),
            f"indicator({series_code} @ {ts})",
        )
    raise ThesisError(f"未知の証憑 kind {kind!r}(語彙: {list(EVIDENCE_KINDS)})")


def _require_int(ref: dict[str, Any], key: str) -> int:
    value = ref.get(key)
    if value is None:
        raise ThesisError(f"証憑参照 {ref.get('kind')!r} に {key} が無い: {ref!r}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ThesisError(f"証憑参照の {key} が整数でない: {value!r}") from exc


def validate_evidence_refs(
    conn: psycopg.Connection, refs: list[dict[str, Any]], *, as_of: datetime
) -> list[dict[str, Any]]:
    """証憑参照を検証して正規化リストを返す(全件 as_of 以前に存在すること)。

    検証は2つの時点に対して行う(独立役員審査 T-017 C-6):

    - **as_of(知り得た時点)**: 参照先が判断時点より後に取り込まれていないか
    - **ts(対象時点 — bar / indicator)**: 参照している足・指標そのものが判断時点より
      未来のものでないか。バックフィルや誤登録では「as_of は過去だが ts は未来」の行が
      作れてしまい、as_of の検査だけでは未来バーの参照を検知できない

    空リストは拒否(証憑なしの提案は作らない)。違反は ``EvidenceError`` に全件列挙する
    — 1件目で止めないのは、LLM(Ben)の出力を1回で全て直せる形で返すため。
    """
    if not refs:
        raise EvidenceError(["証憑参照が空(evidence_refs は必須 — 不変原則3)"])
    problems: list[str] = []
    normalized: list[dict[str, Any]] = []
    for raw in refs:
        if not isinstance(raw, dict):
            problems.append(f"証憑参照がオブジェクトでない: {raw!r}")
            continue
        try:
            sql, params, label = _ref_lookup(raw)
        except ThesisError as exc:
            problems.append(str(exc))
            continue
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        found = row[0] if row else None
        ref_ts = row[1] if row else None
        if found is None:
            problems.append(f"{label}: 証憑が存在しない(参照先の行なし)")
            continue
        if found > as_of:
            problems.append(
                f"{label}: 証憑の as_of {found.isoformat()} が判断時点 "
                f"{as_of.isoformat()} より新しい(未来情報の混入 — 不変原則4)"
            )
            continue
        if ref_ts is not None and ref_ts > as_of:
            problems.append(
                f"{label}: 証憑の対象時点 ts {ref_ts.isoformat()} が判断時点 "
                f"{as_of.isoformat()} より後(未来のバー・指標の参照 — 不変原則4)"
            )
            continue
        normalized.append(dict(raw))
    if problems:
        raise EvidenceError(problems)
    return normalized


# ── 記録 ──────────────────────────────────────────────────────────────────────
def record_thesis(
    conn: psycopg.Connection,
    *,
    fm: str,
    book_id: str,
    instrument_id: int,
    direction: str,
    thesis_md: str,
    evidence_refs: list[dict[str, Any]],
    invalidation_md: str,
    producer: str,
    as_of: datetime,
    run_id: int,
    rule_id: str | None = None,
    model: str | None = None,
    allow_short: bool = False,
) -> int:
    """提案を ``trading.fm_theses`` に追記して thesis_id を返す(記録の唯一の入口)。

    拒否条件(``ThesisError`` / ``EvidenceError``):

    - direction が語彙外、または long-only 期に short(``allow_short=False``)
    - thesis_md / invalidation_md が空(反証条件の欠落 = 40 §制約1 違反)
    - evidence_refs が空・参照先が存在しない・as_of 超(point-in-time 違反)
    - rule_id と model が両方 None、または両方指定(出所の曖昧化)
    """
    if direction not in DIRECTIONS:
        raise ThesisError(f"direction は {list(DIRECTIONS)} のいずれか: {direction!r}")
    if not allow_short and direction not in LONG_ONLY_DIRECTIONS:
        raise ThesisError(
            f"第一陣(Ben/Jim)は long-only のため direction={direction!r} は生成できない"
            "(ledger の空売り記帳が未対応 — execution/runner.py)"
        )
    if not (thesis_md or "").strip():
        raise ThesisError("thesis_md が空(論拠のない提案は記録しない)")
    if not (invalidation_md or "").strip():
        raise ThesisError(
            "invalidation_md が空(『この論点が崩れたら降りる』は全提案の義務 — "
            "40-fund-managers.md §制約1)"
        )
    if (rule_id is None) == (model is None):
        raise ThesisError(
            "出所は rule_id(決定論シグナル)か model(LLM)のどちらか一方を指定する"
        )
    normalized = validate_evidence_refs(conn, evidence_refs, as_of=as_of)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trading.fm_theses
                (fm, book_id, instrument_id, direction, thesis_md, evidence_refs,
                 invalidation_md, producer, rule_id, model, as_of, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING thesis_id
            """,
            (
                fm,
                book_id,
                instrument_id,
                direction,
                thesis_md.strip(),
                Jsonb(normalized),
                invalidation_md.strip(),
                producer,
                rule_id,
                model,
                as_of,
                run_id,
            ),
        )
        return cur.fetchone()[0]


# ── 検疫(プロンプト汚染の封じ込め — 独立役員審査 T-017 C-3)────────────────────
# 再注入の対象から外す thesis を指す追記オンリー表(migrations/0023)。fm_theses 自体は
# 書き換えない(判断の履歴は不変)。除外は**読出し側**の責務であり、注入経路
# (recent_theses / open_theses_by_instrument)の SQL に共通で入る述語がこれである。
_NOT_QUARANTINED = """
              AND NOT EXISTS (
                  SELECT 1 FROM trading.fm_theses_quarantine q
                  WHERE q.thesis_id = t.thesis_id
              )
"""


def quarantine_thesis(
    conn: psycopg.Connection,
    thesis_id: int,
    *,
    reason: str,
    quarantined_by: str,
    run_id: int | None = None,
) -> int:
    """提案を検疫する(以後、着任プロンプトへ再注入しない)。

    汚染が判明した提案を封じ込める唯一の入口。当面の登録は**人手**(この関数の直接呼出、
    または同等の SQL)で行う — 自動検出は誤検知で判断履歴を静かに欠落させるため、
    判断を経路に残す(0023 の判断3)。

    既に検疫済みの thesis を再度渡した場合は既存の quarantine_id を返す(冪等)。
    解除の API は用意しない — 誤検疫の救済は同じ内容を新しい thesis として記録する
    (0023 の判断2 — 検疫を消せる経路を作らない)。
    """
    if not (reason or "").strip():
        raise ThesisError("検疫の理由(reason)は必須(何が汚染したかを残す)")
    if not (quarantined_by or "").strip():
        raise ThesisError("検疫の実施主体(quarantined_by)は必須")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM trading.fm_theses WHERE thesis_id = %s", (thesis_id,)
        )
        if cur.fetchone() is None:
            raise ThesisError(f"検疫対象の thesis_id={thesis_id} が存在しない")
        cur.execute(
            """
            INSERT INTO trading.fm_theses_quarantine
                (thesis_id, reason, quarantined_by, run_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (thesis_id) DO NOTHING
            RETURNING quarantine_id
            """,
            (thesis_id, reason.strip(), quarantined_by.strip(), run_id),
        )
        row = cur.fetchone()
        if row is not None:
            return row[0]
        cur.execute(
            "SELECT quarantine_id FROM trading.fm_theses_quarantine WHERE thesis_id = %s",
            (thesis_id,),
        )
        return cur.fetchone()[0]


def load_thesis(conn: psycopg.Connection, thesis_id: int) -> ThesisRecord | None:
    """1件の提案を**検疫の有無にかかわらず**読む(検疫 CLI の確認表示・監査用)。

    再注入経路(``recent_theses`` / ``open_theses_by_instrument``)と違って検疫済みも
    返すのは、本関数の用途が「これから検疫する対象を人が読む」ことだからである。
    プロンプトに戻す経路ではない。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.thesis_id, t.fm, t.instrument_id, t.direction, t.thesis_md,
                   t.invalidation_md, t.as_of, o.status, g.verdict, g.reasons
            FROM trading.fm_theses t
            LEFT JOIN trading.orders o ON o.thesis_id = t.thesis_id
            LEFT JOIN compliance.gate_log g ON g.id = o.gate_log_id
            WHERE t.thesis_id = %s
            ORDER BY o.id DESC NULLS LAST
            LIMIT 1
            """,
            (thesis_id,),
        )
        r = cur.fetchone()
    if r is None:
        return None
    return ThesisRecord(
        thesis_id=r[0], fm=r[1], instrument_id=r[2], direction=r[3],
        thesis_md=r[4], invalidation_md=r[5], as_of=r[6],
        order_status=r[7], gate_verdict=r[8], gate_reasons=r[9],
    )


def is_quarantined(conn: psycopg.Connection, thesis_id: int) -> bool:
    """当該提案が検疫済みか(監査・運用確認用)。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM trading.fm_theses_quarantine WHERE thesis_id = %s",
            (thesis_id,),
        )
        return cur.fetchone() is not None


def quarantined_open_instruments(
    conn: psycopg.Connection, fm: str, instrument_ids: list[int]
) -> set[int]:
    """建玉根拠が**検疫されたために読み出せない**銘柄(独立役員審査 C-11)。

    ``open_theses_by_instrument`` が返さない銘柄には2種類ある — 「そもそも thesis が
    無い」と「あるが検疫済み」。後者は**降りる条件を失った保有**であり、放置すると
    invalidation の無い持ち切りになる(40 §制約1 違反)。呼び出し側が両者を区別して
    扱えるよう、後者だけを返す。
    """
    if not instrument_ids:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT t.instrument_id
            FROM trading.fm_theses t
            JOIN trading.fm_theses_quarantine q ON q.thesis_id = t.thesis_id
            WHERE t.fm = %s AND t.instrument_id = ANY(%s) AND t.direction = 'buy'
            """,
            (fm, list(instrument_ids)),
        )
        return {r[0] for r in cur.fetchall()}


def quarantine_stats(conn: psycopg.Connection, *, as_of: datetime) -> dict[str, int]:
    """検疫の発生状況(当日増分・累計・全提案数)— 日次サマリと監査の入力。

    解除できない封じ込め(0023 判断2)である以上、**silent な mass-quarantine を
    検知できること**が採用条件である(独立役員審査 C-10 の裁定)。当日は JST の暦日で
    数える(日次サイクルの区切りと揃える)。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                count(*) FILTER (
                    WHERE (created_at AT TIME ZONE 'Asia/Tokyo')::date
                          = (%s AT TIME ZONE 'Asia/Tokyo')::date
                ),
                count(*)
            FROM trading.fm_theses_quarantine
            """,
            (as_of,),
        )
        today, total = cur.fetchone()
        cur.execute("SELECT count(*) FROM trading.fm_theses")
        theses_total = cur.fetchone()[0]
    return {"today": today, "total": total, "theses_total": theses_total}


# ── 読出し(次回プロンプトへの注入)────────────────────────────────────────────
def recent_theses(
    conn: psycopg.Connection, fm: str, *, limit: int = 20
) -> list[ThesisRecord]:
    """当該 FM の直近提案を新しい順に、ゲート判定の結果つきで返す。

    ゲート判定は ``trading.fm_theses`` には書き戻さない(追記オンリー)ため、
    orders.thesis_id → orders.status / compliance.gate_log を辿って合成する。

    **検疫済み(``trading.fm_theses_quarantine``)の提案は返さない** — 本関数は着任
    プロンプトへの再注入経路であり、汚染した提案をここで落とす(審査 T-017 C-3)。
    検疫は件数を減らすだけで繰り上げは行わない(limit は検疫前ではなく後に効く SQL に
    しているため、除外分は次に古い提案で埋まる)。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.thesis_id, t.fm, t.instrument_id, t.direction, t.thesis_md,
                   t.invalidation_md, t.as_of, o.status, g.verdict, g.reasons
            FROM trading.fm_theses t
            LEFT JOIN trading.orders o ON o.thesis_id = t.thesis_id
            LEFT JOIN compliance.gate_log g ON g.id = o.gate_log_id
            WHERE t.fm = %s
            """
            + _NOT_QUARANTINED
            + """
            ORDER BY t.thesis_id DESC
            LIMIT %s
            """,
            (fm, limit),
        )
        rows = cur.fetchall()
    return [
        ThesisRecord(
            thesis_id=r[0], fm=r[1], instrument_id=r[2], direction=r[3],
            thesis_md=r[4], invalidation_md=r[5], as_of=r[6],
            order_status=r[7], gate_verdict=r[8], gate_reasons=r[9],
        )
        for r in rows
    ]


def open_theses_by_instrument(
    conn: psycopg.Connection, fm: str, instrument_ids: list[int]
) -> dict[int, ThesisRecord]:
    """保有銘柄ごとの「最後に建てた根拠」(direction='buy' の最新 thesis)。

    Ben の保有見直し(invalidation 成立チェック)の入力。約定に至らなかった提案も
    含み得るが、保有中の銘柄に限って引くため実務上は建玉の根拠になる。

    ここも**プロンプトへの注入経路**であるため検疫済みの提案は返さない(審査 T-017
    C-3。審査は recent_theses のみを挙げたが、建玉根拠も同じ再注入経路である)。
    根拠が検疫されると呼び出し側の入力は None になり、Ben は「根拠不明の保有」として
    見直す — 汚染テキストを渡し続けるより安全な縮退である。
    """
    if not instrument_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (t.instrument_id)
                   t.thesis_id, t.fm, t.instrument_id, t.direction, t.thesis_md,
                   t.invalidation_md, t.as_of
            FROM trading.fm_theses t
            WHERE t.fm = %s AND t.instrument_id = ANY(%s) AND t.direction = 'buy'
            """
            + _NOT_QUARANTINED
            + """
            ORDER BY t.instrument_id, t.thesis_id DESC
            """,
            (fm, list(instrument_ids)),
        )
        rows = cur.fetchall()
    return {
        r[2]: ThesisRecord(
            thesis_id=r[0], fm=r[1], instrument_id=r[2], direction=r[3],
            thesis_md=r[4], invalidation_md=r[5], as_of=r[6],
        )
        for r in rows
    }


# ── 検疫 CLI(運用の入口 — reminder fm-quarantine-runbook)─────────────────────
# 手動 SQL より安全な入口を作るのが目的である。素の INSERT には
#   (1) 対象を読まずに thesis_id を打ち間違える(誤検疫は解除できない)
#   (2) reason / quarantined_by を空文字で通す
#   (3) 検疫後に「根拠を失った保有」を確認し忘れる
# の3つの事故があり、いずれもアプリ層でしか止められない。手順の正は
# docs/ops/fm-quarantine-runbook.md。
def _render_thesis(record: ThesisRecord, *, quarantined: bool) -> str:
    """検疫前に人が読む表示(**本文全文**)。要約しない — 誤爆防止が目的のため。"""
    gate = record.gate_verdict or "(注文なし)"
    lines = [
        f"thesis_id : {record.thesis_id}",
        f"FM        : {record.fm}   direction: {record.direction}",
        f"instrument: {record.instrument_id}",
        f"as_of     : {record.as_of.isoformat()}",
        f"注文/ゲート: {record.order_status or '(注文なし)'} / {gate}",
        f"検疫済み  : {'はい(この呼び出しは冪等)' if quarantined else 'いいえ'}",
        "--- thesis_md ---",
        record.thesis_md,
        "--- invalidation_md ---",
        record.invalidation_md,
    ]
    return "\n".join(lines)


def _open_instruments(conn: psycopg.Connection, fm: str) -> list[int]:
    """当該 FM の保有銘柄(全帳簿・qty<>0)。検疫の影響確認に使う。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT instrument_id FROM trading.positions "
            "WHERE fm = %s AND qty <> 0",
            (fm,),
        )
        return sorted(int(r[0]) for r in cur.fetchall())


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI 実行パス
    """CLI: 提案の検疫(表示 → 確認 → 登録 → 影響と件数の再表示)。

    ``uv run python -m ryza.fm.theses --quarantine <thesis_id> --reason "..." --by "..."``

    ``--show`` は表示のみ(検疫しない)。``--yes`` は確認プロンプトを省く — 対話端末の
    無い環境向けだが、**runbook は二者確認を求めているので通常運用では使わない**。
    """
    parser = argparse.ArgumentParser(
        description="FM 提案の検疫(手順: docs/ops/fm-quarantine-runbook.md)"
    )
    parser.add_argument("--quarantine", type=int, metavar="THESIS_ID", help="検疫する提案")
    parser.add_argument("--show", type=int, metavar="THESIS_ID", help="内容の表示のみ")
    parser.add_argument("--stats", action="store_true", help="検疫件数の表示のみ")
    parser.add_argument("--reason", help="検疫の理由(何が汚染したか — 必須)")
    parser.add_argument("--by", dest="quarantined_by", help="実施主体(二者の氏名/役割)")
    parser.add_argument("--yes", action="store_true", help="確認プロンプトを省く")
    args = parser.parse_args(argv)

    from ryza.db.conn import connect
    from ryza.provenance import start_run

    if sum(x is not None for x in (args.quarantine, args.show)) + int(args.stats) != 1:
        parser.error("--quarantine / --show / --stats のいずれか1つを指定してください")

    now = datetime.now(UTC)
    if args.stats:
        conn = connect()
        try:
            print(quarantine_stats(conn, as_of=now), file=sys.stderr)
        finally:
            conn.close()
        return 0

    if args.show is not None:
        conn = connect()
        try:
            record = load_thesis(conn, args.show)
            if record is None:
                print(f"thesis_id={args.show} は存在しません", file=sys.stderr)
                return 1
            print(_render_thesis(record, quarantined=is_quarantined(conn, args.show)))
        finally:
            conn.close()
        return 0

    thesis_id = args.quarantine
    if not (args.reason or "").strip():
        parser.error("--reason は必須です(何が汚染したかを残す)")
    if not (args.quarantined_by or "").strip():
        parser.error("--by は必須です(runbook は設計リード・監査の二者確認を求める)")

    conn = connect()
    try:
        record = load_thesis(conn, thesis_id)
        if record is None:
            print(f"thesis_id={thesis_id} は存在しません", file=sys.stderr)
            conn.close()
            return 1
        already = is_quarantined(conn, thesis_id)
        print(_render_thesis(record, quarantined=already))
        if not args.yes:
            # 誤爆防止: y/N ではなく thesis_id の再入力を求める(解除できない操作のため)。
            answer = input("\n上記を検疫します。thesis_id を再入力してください: ").strip()
            if answer != str(thesis_id):
                print("入力が一致しないため中止しました", file=sys.stderr)
                conn.close()
                return 1
    except Exception:
        conn.close()
        raise

    run = start_run("fm.quarantine", {"thesis_id": thesis_id})
    try:
        with conn.transaction():
            quarantine_id = quarantine_thesis(
                conn, thesis_id, reason=args.reason,
                quarantined_by=args.quarantined_by, run_id=run.run_id,
            )
            open_ids = _open_instruments(conn, record.fm)
            orphaned = quarantined_open_instruments(conn, record.fm, open_ids)
            stats = quarantine_stats(conn, as_of=now)
        run.finish("success")
    except Exception:
        run.finish("failed")
        raise
    finally:
        conn.close()

    print(f"検疫しました: quarantine_id={quarantine_id}", file=sys.stderr)
    print(f"検疫件数: {stats}", file=sys.stderr)
    if orphaned:
        print(
            "⚠ 建玉の根拠を失った保有があります(降りる条件が読めない状態 — "
            f"runbook の『影響確認』へ): instrument_id={sorted(orphaned)}",
            file=sys.stderr,
        )
    return 0


__all__ = [
    "DIRECTIONS",
    "EVIDENCE_KINDS",
    "LONG_ONLY_DIRECTIONS",
    "EvidenceError",
    "ThesisError",
    "ThesisRecord",
    "is_quarantined",
    "load_thesis",
    "main",
    "open_theses_by_instrument",
    "quarantine_stats",
    "quarantine_thesis",
    "quarantined_open_instruments",
    "record_thesis",
    "recent_theses",
    "validate_evidence_refs",
]


if __name__ == "__main__":  # pragma: no cover - CLI 実行パス
    raise SystemExit(main())
