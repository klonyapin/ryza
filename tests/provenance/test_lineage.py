"""リネージ記録・遡及の受け入れ基準テスト。

- 3段のリネージ(documents → research_reports → journal_entries)を登録し、
  trace_back が全段を返す
- trace_forward が逆方向をたどる
- record が outputs × inputs の全ペアを張る / max_depth / 循環に耐える

すべて共有 ``conn``(rollback 隔離)を渡して実行する。lineage_edges.run_id は
meta.runs を参照するため、まず start_run で run を作る。
"""

from __future__ import annotations

from ryza.provenance.lineage import record, trace_back, trace_forward
from ryza.provenance.runs import start_run


def _child_ids(node):
    return {(c.kind, c.id) for c in node.children}


def test_three_level_trace_back(conn):
    r = start_run("test.lineage", conn=conn)
    # 3段: journal_entries(1000) → research_reports(500) → documents(10, 11)
    record(
        conn, r,
        outputs=[("research_reports", 500)],
        inputs=[("documents", 10), ("documents", 11)],
    )
    record(conn, r, outputs=[("journal_entries", 1000)], inputs=[("research_reports", 500)])

    tree = trace_back(conn, "journal_entries", 1000)
    assert (tree.kind, tree.id) == ("journal_entries", "1000")
    # 第2段: research_reports 500
    assert _child_ids(tree) == {("research_reports", "500")}
    report = tree.children[0]
    # 第3段: documents 10, 11
    assert _child_ids(report) == {("documents", "10"), ("documents", "11")}
    # documents は末端(それ以上の入力なし)。
    assert all(not d.children for d in report.children)


def test_trace_forward_reverse(conn):
    r = start_run("test.lineage.fwd", conn=conn)
    record(conn, r, outputs=[("research_reports", 500)], inputs=[("documents", 10)])
    record(conn, r, outputs=[("research_reports", 501)], inputs=[("documents", 10)])
    record(conn, r, outputs=[("journal_entries", 1000)], inputs=[("research_reports", 500)])

    # documents 10 → 使った成果物 research_reports 500, 501
    tree = trace_forward(conn, "documents", 10)
    assert _child_ids(tree) == {("research_reports", "500"), ("research_reports", "501")}
    # research_reports 500 → journal_entries 1000 までたどる
    r500 = next(c for c in tree.children if c.id == "500")
    assert _child_ids(r500) == {("journal_entries", "1000")}


def test_record_returns_edge_count_and_dedups(conn):
    r = start_run("test.lineage.count", conn=conn)
    # 2 outputs × 3 inputs = 6 辺
    n = record(
        conn,
        r,
        outputs=[("orders", 1), ("orders", 2)],
        inputs=[("signals", 10), ("signals", 11), ("signals", 12)],
    )
    assert n == 6
    # 再登録は ON CONFLICT DO NOTHING で DB 上は増えない。
    record(conn, r, outputs=[("orders", 1)], inputs=[("signals", 10)])
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM meta.lineage_edges WHERE from_kind = 'orders' AND run_id = %s",
            (r.run_id,),
        )
        assert cur.fetchone()[0] == 6


def test_record_empty_is_noop(conn):
    r = start_run("test.lineage.empty", conn=conn)
    assert record(conn, r, outputs=[], inputs=[("documents", 1)]) == 0
    assert record(conn, r, outputs=[("reports", 1)], inputs=[]) == 0


def test_trace_back_accepts_run_id_int(conn):
    # record は Run でも run_id(int)でも受け付ける。
    r = start_run("test.lineage.int", conn=conn)
    record(conn, r.run_id, outputs=[("a", 1)], inputs=[("b", 2)])
    tree = trace_back(conn, "a", 1)
    assert _child_ids(tree) == {("b", "2")}


def test_max_depth_truncates(conn):
    r = start_run("test.lineage.depth", conn=conn)
    # 鎖: n0 → n1 → n2 → n3
    for i in range(3):
        record(conn, r, outputs=[("n", i)], inputs=[("n", i + 1)])
    tree = trace_back(conn, "n", 0, max_depth=2)
    # depth 0: n0, depth1: n1, depth2: n2(ここで打ち切り)
    n1 = tree.children[0]
    n2 = n1.children[0]
    assert n2.truncated is True
    assert n2.children == []


def test_cycle_is_handled(conn):
    r = start_run("test.lineage.cycle", conn=conn)
    # 循環: x → y → x
    record(conn, r, outputs=[("x", 1)], inputs=[("y", 1)])
    record(conn, r, outputs=[("y", 1)], inputs=[("x", 1)])
    tree = trace_back(conn, "x", 1)  # 無限再帰にならず返る
    y = tree.children[0]
    assert (y.kind, y.id) == ("y", "1")
    # y の子に x が再び現れるが、既訪問なので展開されない。
    x_again = y.children[0]
    assert (x_again.kind, x_again.id) == ("x", "1")
    assert x_again.children == []
