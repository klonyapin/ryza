"""反証拠反転テストハーネスのテスト(§7・監査 A-13)。

反転率カーブの計測と診断は純関数(DB 非依存)。合成文書の生成・挿入は DB を使う。
"""

from __future__ import annotations

from ryza.research.counterevidence import (
    insert_synthetic,
    measure_reversal_curve,
    synthesize,
)


# ── 合成反証拠の生成(純関数)─────────────────────────────────────────────────
def test_synthesize_graduated_counter_fraction():
    docs0 = synthesize(0.0, n_docs=10)
    docs5 = synthesize(0.5, n_docs=10)
    docs10 = synthesize(1.0, n_docs=10)
    assert sum(d.is_counter for d in docs0) == 0
    assert sum(d.is_counter for d in docs5) == 5
    assert sum(d.is_counter for d in docs10) == 10


# ── 反転率カーブ(純関数)───────────────────────────────────────────────────────
def test_reversal_curve_shape_and_rows():
    # 健全なアナリスト: level>=0.7 で反転する試行関数。
    def trial(level, seed):
        return level >= 0.7

    curve = measure_reversal_curve(trial, levels=[0.0, 0.2, 0.5, 0.8, 1.0], seeds=[0, 1, 2])
    assert curve.rate_at(0.2) == 0.0
    assert curve.rate_at(0.8) == 1.0
    rows = curve.as_rows()
    assert len(rows) == 5
    assert rows[0]["n_trials"] == 3


def test_diagnose_healthy():
    curve = measure_reversal_curve(lambda lvl, s: lvl >= 0.7,
                                   levels=[0.2, 0.8], seeds=[0])
    assert curve.diagnose() == "healthy"


def test_diagnose_stubborn():
    # 反証拠 80% でも反転しない → 固執。
    curve = measure_reversal_curve(lambda lvl, s: False, levels=[0.2, 0.8], seeds=[0])
    assert curve.diagnose() == "stubborn"


def test_diagnose_oversensitive():
    # 反証拠 20% で既に反転 → 過敏。
    curve = measure_reversal_curve(lambda lvl, s: True, levels=[0.2, 0.8], seeds=[0])
    assert curve.diagnose() == "oversensitive"


# ── 合成文書の DB 挿入 ──────────────────────────────────────────────────────────
def test_insert_synthetic_marks_documents(conn, run):
    docs = synthesize(0.6, n_docs=5)
    ids = insert_synthetic(conn, run, docs)
    assert len(ids) == 5
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM docs.documents "
            "WHERE doc_id = ANY(%s) AND (meta->>'synthetic')::boolean = true",
            (ids,),
        )
        assert cur.fetchone()[0] == 5
