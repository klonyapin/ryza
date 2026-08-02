"""lineage — 成果物 → 入力の辺の記録と遡及クエリ。

設計書 §6(``meta.lineage_edges``)準拠。各ジョブは参照した入力を ``lineage_edges`` に
登録し、「この仕訳の元データは何か」「このニュースはどの成果物に使われたか」を SQL で
遡れるようにする。

辺の向き: ``from``(成果物) → ``to``(入力)。例: ``(research_reports, 123) → (documents, 456)``。
``kind`` は概ねテーブル名、``id`` は行 ID。DB 上は text なので int / str いずれも受け付ける
(内部で str 化する)。

DB 書き込みは渡された ``conn`` のトランザクションに参加し、本モジュールは commit しない。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg

from ryza.provenance.runs import Run

# (kind, id) のペア。id は int / str どちらでもよい。
IdRef = tuple[str, "int | str"]


@dataclass
class LineageNode:
    """リネージ木のノード。

    ``trace_back`` では ``children`` が入力(この成果物が依存したもの)、
    ``trace_forward`` では ``children`` が成果物(この入力を使ったもの)。
    ``truncated`` は max_depth 到達で打ち切ったことを示す。
    """

    kind: str
    id: str
    children: list[LineageNode] = field(default_factory=list)
    truncated: bool = False


def _run_id(run: Run | int) -> int:
    return run.run_id if isinstance(run, Run) else run


def record(
    conn: psycopg.Connection,
    run: Run | int,
    outputs: list[IdRef],
    inputs: list[IdRef],
) -> int:
    """``outputs`` × ``inputs`` の全ペアを ``meta.lineage_edges`` に一括登録する。

    各成果物が各入力に依存した、という辺を張る。既存の辺(同一 PK)は無視する
    (``ON CONFLICT DO NOTHING``)。登録した(重複を除く)辺数を返す。
    """
    rid = _run_id(run)
    rows = [
        (out_kind, str(out_id), in_kind, str(in_id), rid)
        for (out_kind, out_id) in outputs
        for (in_kind, in_id) in inputs
    ]
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO meta.lineage_edges (from_kind, from_id, to_kind, to_id, run_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (from_kind, from_id, to_kind, to_id) DO NOTHING
            """,
            rows,
        )
    return len(rows)


def _trace(
    conn: psycopg.Connection,
    kind: str,
    id: str,
    *,
    forward: bool,
    max_depth: int,
    _depth: int,
    _seen: set[tuple[str, str]],
) -> LineageNode:
    """1 ノードとその子を再帰的に構築する。``forward`` で辺の追う向きを切り替える。"""
    node = LineageNode(kind=kind, id=id)
    key = (kind, id)
    if _depth >= max_depth:
        node.truncated = True
        return node
    if key in _seen:
        # 循環防止: 既訪問ノードはそれ以上展開しない。
        return node
    _seen = _seen | {key}

    with conn.cursor() as cur:
        if forward:
            # 逆方向: この入力(to)を使った成果物(from)を探す。
            cur.execute(
                "SELECT from_kind, from_id FROM meta.lineage_edges "
                "WHERE to_kind = %s AND to_id = %s ORDER BY from_kind, from_id",
                (kind, id),
            )
        else:
            # 順方向(遡及): この成果物(from)が依存した入力(to)を探す。
            cur.execute(
                "SELECT to_kind, to_id FROM meta.lineage_edges "
                "WHERE from_kind = %s AND from_id = %s ORDER BY to_kind, to_id",
                (kind, id),
            )
        neighbors = cur.fetchall()

    for nkind, nid in neighbors:
        node.children.append(
            _trace(
                conn,
                nkind,
                nid,
                forward=forward,
                max_depth=max_depth,
                _depth=_depth + 1,
                _seen=_seen,
            )
        )
    return node


def trace_back(
    conn: psycopg.Connection,
    kind: str,
    id: int | str,
    max_depth: int = 10,
) -> LineageNode:
    """成果物から入力へ再帰的に遡り、木を返す(「この仕訳の元データは何か」)。

    ``max_depth`` を超える枝は ``truncated=True`` で打ち切る。循環は自動で止める。
    """
    return _trace(
        conn, kind, str(id), forward=False, max_depth=max_depth, _depth=0, _seen=set()
    )


def trace_forward(
    conn: psycopg.Connection,
    kind: str,
    id: int | str,
    max_depth: int = 10,
) -> LineageNode:
    """入力から成果物へ逆方向にたどり、木を返す(「このニュースはどの成果物に使われたか」)。"""
    return _trace(
        conn, kind, str(id), forward=True, max_depth=max_depth, _depth=0, _seen=set()
    )
