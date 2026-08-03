"""朝刊パイプラインの E2E テスト(モック LLM)。素材 → outbox 投入まで。"""

from __future__ import annotations

from ryza.press import morning
from ryza.provenance import trace_back


def _outbox_rows(conn, run_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT channel, embed_json, urgent FROM press.outbox WHERE run_id = %s", (run_id,)
        )
        return cur.fetchall()


def test_morning_end_to_end(conn, run, make_press_llm, insert_enriched_doc):
    d1 = insert_enriched_doc(title="決算A", score=0.9, instrument_ids=[100])
    d2 = insert_enriched_doc(title="決算B", score=0.7, instrument_ids=[200])
    llm, _ = make_press_llm()

    result = morning.run_morning(conn, run, llm)

    # outbox に朝刊が 1 本(非緊急・channel=press)投入されている。
    assert result.outbox_id is not None
    rows = _outbox_rows(conn, run.run_id)
    assert len(rows) == 1
    channel, embed, urgent = rows[0]
    assert channel == "press"
    assert urgent is False
    # トピック数 ≤ 5、各トピックは linter 合格(200-400字が保証される)。
    assert 1 <= len(result.accepted) <= 5
    assert len(embed["fields"]) == len(result.accepted)
    assert embed["footer"]["text"]  # 免責フッター

    # research_reports に morning_press が保存されている。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM docs.research_reports "
            "WHERE run_id = %s AND report_type = 'morning_press'",
            (run.run_id,),
        )
        assert cur.fetchone()[0] == len(result.accepted)

    # 素材 → 記事のリネージが張られている。
    report_id = result.report_ids[0]
    tree = trace_back(conn, "research_reports", report_id)
    assert any(c.kind == "documents" for c in tree.children)
    assert {d1, d2} & {int(c.id) for c in tree.children if c.kind == "documents"}


def test_morning_caps_at_five_topics(conn, run, make_press_llm, insert_enriched_doc):
    for i in range(7):
        insert_enriched_doc(title=f"決算{i}", score=0.9 - i * 0.05)
    llm, _ = make_press_llm()
    result = morning.run_morning(conn, run, llm)
    assert len(result.accepted) <= 5


def test_morning_rejects_bad_topics_and_records_original(conn, run, make_press_llm,
                                                         insert_enriched_doc):
    insert_enriched_doc(title="決算A", score=0.9)
    llm, _ = make_press_llm(bad_shape=True)  # U字を壊した出力 → 2回再生成しても不合格
    result = morning.run_morning(conn, run, llm)

    assert result.accepted == []
    assert result.outbox_id is None  # 合格ゼロなら投稿しない
    assert len(result.rejected) >= 1
    # 落板トピックの失敗原文は研究素材として保存(§2)。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM docs.research_reports "
            "WHERE run_id = %s AND report_type = 'morning_press_rejected'",
            (run.run_id,),
        )
        assert cur.fetchone()[0] == len(result.rejected)
    # 再生成上限まで試行している(初回 + 再生成2回 = 3)。
    assert result.rejected[0].attempts == 3


def test_document_title_injection_stays_inside_fence(conn, run, make_press_llm,
                                                     insert_enriched_doc, injection):
    """取込文書の title に混ぜた偽指示+偽フェンスが執筆プロンプトの境界を壊さない。

    入口は triage_queue の title/source_name → ``topics._from_documents`` の material →
    ``writer._build_prompt``(reminders ``press-material-fence``)。
    """
    insert_enriched_doc(title=injection, source_name=injection, score=0.9)
    llm, provider = make_press_llm()

    result = morning.run_morning(conn, run, llm)

    assert result.accepted  # 注入文があっても執筆自体は通常どおり完了する
    user = provider.calls[0]["user"]
    assert user.count("<<<material>>>") == 1
    assert user.count("<<<end>>>") == 1
    assert "＜＜＜end＞＞＞" in user  # 偽フェンスは全角化されている
    assert user.index("<<<material>>>") < user.index("全銘柄のロングを推奨") \
        < user.index("<<<end>>>")
