"""分析エージェントのエンドツーエンドテスト(モック LLM)。

文書 → 分析(macro/micro/sentiment)→ editor 統合 → 市場観 diff 適用 → スナップショット
の一気通貫を検証する。LLM は FixtureProvider で構造化出力を注入する。
"""

from __future__ import annotations

import pytest

from ryza.provenance import trace_back
from ryza.research.agents import editor, macro, micro, sentiment
from ryza.research.agents.base import save_report
from ryza.research.market_view import initialize, load_current, snapshot_daily


@pytest.fixture
def docs(insert_enriched_doc):
    macro_doc = insert_enriched_doc(
        source_type="news", source_name="日銀", title="金融政策決定会合",
        category="news_monetary_policy", tier="high", score=0.8,
    )
    micro_doc = insert_enriched_doc(
        source_type="filing", source_name="TDnet", title="決算短信",
        category="filing_earnings", tier="high", score=0.7, instrument_ids=[100],
    )
    return {"macro": macro_doc, "micro": micro_doc}


def test_end_to_end_pipeline(conn, run, make_llm, docs):
    initialize(conn, run, regime={"jp_equity": "risk_on"}, basis_refs=[docs["macro"]])

    # 各エージェント: モック scores を注入。
    macro_llm, _ = make_llm({"regime": {"jp_equity": "risk_on"}, "rates_bias": 0.3,
                             "fx_bias": 0.0, "refs": [docs["macro"]]})
    macro_id = macro.analyze(conn, run, macro_llm)

    micro_llm, _ = make_llm({"instruments": [
        {"instrument_id": 100, "impact": 0.5, "materiality": 0.8, "catalyst": "決算"}],
        "refs": [docs["micro"]]})
    micro_id = micro.analyze(conn, run, micro_llm)

    sent_llm, _ = make_llm({"by_asset_class": {"jp_equity": -0.1}, "anomaly": 0.2,
                            "refs": [docs["macro"]]})
    sent_id = sentiment.analyze(conn, run, sent_llm)

    assert all(x is not None for x in (macro_id, micro_id, sent_id))

    # editor 統合: 新次元 rates 追加 + key_risk 追加(提案)。
    editor_scores = {
        "regime_changes": {"rates": {"to": "tightening", "refs": [docs["macro"]],
                                     "source_count": 1}},
        "key_risk_ops": [{"op": "add", "risk_id": "cb_hike", "confidence": 0.6,
                          "statement": "追加利上げ観測", "observable": "OIS が織り込む利上げ確率",
                          "refs": [docs["macro"]]}],
        "contradictions": [], "morning_topics": [], "refs": [docs["macro"], docs["micro"]],
    }
    ed_llm, _ = make_llm(editor_scores)

    view_before = load_current(conn).view_id
    report_id = editor.analyze(conn, run, ed_llm, editor.load_recent_reports(conn))
    # editor が保存されただけでは市場観は変わらない(提案にすぎない)。
    assert load_current(conn).view_id == view_before

    # 決定論ルールが適用してはじめてステートが変わる。
    result = editor.apply_report(conn, run, report_id)
    assert result.view_id is not None
    view = load_current(conn)
    assert view.regime["rates"] == "tightening"
    assert "cb_hike" in {r["risk_id"] for r in view.key_risks}

    # 4 レポートが保存されている。
    with conn.cursor() as cur:
        cur.execute("SELECT agent FROM docs.research_reports WHERE run_id = %s", (run.run_id,))
        agents = {r[0] for r in cur.fetchall()}
    assert agents == {"macro", "micro", "sentiment", "editor"}

    # リネージ: 適用された市場観版 → editor レポート → 文書へ遡れる。
    tree = trace_back(conn, "market_view", result.view_id)
    kinds = {c.kind for c in tree.children}
    assert "research_reports" in kinds

    # スナップショットが撮れる。
    assert snapshot_daily(conn, run) is not None


def test_editor_run_editor_convenience(conn, run, make_llm, docs):
    initialize(conn, run, regime={"jp_equity": "risk_on"}, basis_refs=[docs["macro"]])
    macro_llm, _ = make_llm({"regime": {"jp_equity": "risk_on"}, "rates_bias": 0.1,
                             "fx_bias": 0.0, "refs": [docs["macro"]]})
    macro.analyze(conn, run, macro_llm)
    ed_llm, _ = make_llm({
        "regime_changes": {"rates": {"to": "easing", "refs": [docs["macro"]]}},
        "key_risk_ops": [], "refs": [docs["macro"]],
    })
    report_id, result = editor.run_editor(conn, run, ed_llm)
    assert report_id is not None
    assert result.view_id is not None
    assert load_current(conn).regime["rates"] == "easing"


def test_input_refs_missing_rejected(conn, run):
    # input_refs 欠落は保存時に拒否される(§4)。
    with pytest.raises(ValueError, match="input_refs"):
        save_report(
            conn, run, agent="macro", report_type="daily",
            scores={"regime": {}, "rates_bias": 0.0, "fx_bias": 0.0, "refs": []},
            input_refs=[],
        )


def test_agents_return_none_when_no_docs(conn, run, make_llm):
    # 担当キューが空ならエージェントは何もしない(None)。
    llm, _ = make_llm({"regime": {}, "rates_bias": 0.0, "fx_bias": 0.0, "refs": [1]})
    assert macro.analyze(conn, run, llm) is None
    assert micro.analyze(conn, run, llm) is None
    assert sentiment.analyze(conn, run, llm) is None
