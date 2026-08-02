"""会計エンジン × 証憑ストア統合(T-005)の検証。

- RYZA_EVIDENCE_DIR 設定時: 証憑がストア経由(file:// URI)で保存され、verify が通り、
  仕訳の evidence_id から get で原文が取れる。
- 未設定時: 従来どおり payload_ref に JSON をインライン格納(フォールバック)。
- どちらの経路でも移動平均法のポジション再生(replay)が破綻しない。
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from ryza.ledger import posting, statements
from ryza.provenance.evidence import EvidenceStore, LocalStorage

D = Decimal
DAY = date(2026, 8, 3)


def _evidence_id_of(conn, entry_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT evidence_id FROM ledger.journal_entries WHERE entry_id = %s", (entry_id,)
        )
        return cur.fetchone()[0]


def _payload_ref_of(conn, evidence_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload_ref FROM ledger.evidence WHERE evidence_id = %s", (evidence_id,)
        )
        return cur.fetchone()[0]


# ── ストア経由(RYZA_EVIDENCE_DIR 設定時) ───────────────────────────────────
def test_fill_evidence_stored_via_provenance_store(conn, run_id, tmp_path, monkeypatch):
    evdir = tmp_path / "evidence_root"
    monkeypatch.setenv("RYZA_EVIDENCE_DIR", str(evdir))

    entry_id = posting.post_fill(
        conn, book_id="DEMO_FUND", instrument_id=2001, side="buy",
        qty=10, price=300, entry_date=DAY, run_id=run_id,
    )
    eid = _evidence_id_of(conn, entry_id)
    payload_ref = _payload_ref_of(conn, eid)

    # 証憑ストア経由 → payload_ref は file:// URI(インライン JSON ではない)。
    assert payload_ref.startswith("file://")

    # 独立に構築したストアで verify が通り、原文が取れる(改竄検知の部品)。
    store = EvidenceStore(LocalStorage(evdir))
    assert store.verify(conn, eid) is True
    got = json.loads(store.get(conn, eid).decode("utf-8"))
    assert got["instrument_id"] == 2001
    assert got["side"] == "buy"
    assert got["qty"] == "10"


def test_replay_works_with_store_backed_evidence(conn, run_id, tmp_path, monkeypatch):
    """ストア経由の証憑でも移動平均法の実現/未実現損益が手計算と一致する。"""
    monkeypatch.setenv("RYZA_EVIDENCE_DIR", str(tmp_path / "ev"))
    iid = 2002
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="buy",
                      qty=100, price=500, entry_date=DAY, run_id=run_id)
    posting.post_mark_to_market(conn, book_id="DEMO_FUND", instrument_id=iid,
                                price=600, entry_date=DAY, run_id=run_id)
    # 未実現 = 100*(600-500) = 10000(収益は貸方=負の borrow)
    tb = statements.trial_balance(conn, "DEMO_FUND", DAY)
    unreal = tb[tb["account_id"] == "unrealized_pnl"].balance.iloc[0]
    assert unreal == D(-10000)

    # 一部売却 40 @ 620 → 実現 = 40*(620-500) = 4800
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=iid, side="sell",
                      qty=40, price=620, entry_date=DAY, run_id=run_id)
    tb2 = statements.trial_balance(conn, "DEMO_FUND", DAY)
    realized = tb2[tb2["account_id"] == "realized_pnl"].balance.iloc[0]
    assert realized == D(-4800)


# ── フォールバック(RYZA_EVIDENCE_DIR 未設定時) ────────────────────────────
def test_inline_fallback_without_env(conn, run_id, monkeypatch):
    monkeypatch.delenv("RYZA_EVIDENCE_DIR", raising=False)

    entry_id = posting.post_fill(
        conn, book_id="DEMO_FUND", instrument_id=2003, side="buy",
        qty=5, price=200, entry_date=DAY, run_id=run_id,
    )
    eid = _evidence_id_of(conn, entry_id)
    payload_ref = _payload_ref_of(conn, eid)

    # フォールバック → payload_ref はインライン JSON(URI ではない)。
    assert not payload_ref.startswith(("file://", "gs://"))
    inline = json.loads(payload_ref)
    assert inline["instrument_id"] == 2003
    # 続けて売却でき、replay がインライン証憑を読めている。
    posting.post_fill(conn, book_id="DEMO_FUND", instrument_id=2003, side="sell",
                      qty=5, price=250, entry_date=DAY, run_id=run_id)


def test_ops_cost_evidence_via_store(conn, run_id, tmp_path, monkeypatch):
    """運営費用の証憑もストア経由になる。"""
    monkeypatch.setenv("RYZA_EVIDENCE_DIR", str(tmp_path / "ev"))
    entry_id = posting.post_ops_cost(
        conn, category="gcp", amount=1200, entry_date=DAY,
        dept_tag="ops", run_id=run_id,
    )
    eid = _evidence_id_of(conn, entry_id)
    store = EvidenceStore(LocalStorage(tmp_path / "ev"))
    assert store.verify(conn, eid) is True
    payload = json.loads(store.get(conn, eid).decode("utf-8"))
    assert payload["category"] == "gcp"
    assert payload["amount"] == "1200"


def test_dedup_same_evidence_reused_across_env(conn, run_id, tmp_path, monkeypatch):
    """ストア経由では同一内容の証憑が重複排除で同じ evidence_id を返す。"""
    monkeypatch.setenv("RYZA_EVIDENCE_DIR", str(tmp_path / "ev"))
    from ryza.ledger import _util

    e1 = _util.create_evidence(conn, kind="decision", payload={"k": 1}, source="t")
    e2 = _util.create_evidence(conn, kind="decision", payload={"k": 1}, source="t")
    assert e1 == e2
