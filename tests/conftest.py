"""tests 全体の共有フィクスチャ。

**残留データ隔離**(Issue #23): テストは compose.yaml のライブ PostgreSQL を
日次取込ジョブ等と共有するため、commit 済みの実データ(market.indicators /
market.bars / docs.documents など)が残っていると、テーブル全体を数える
count assert が残留分を拾って壊れる。rollback 隔離はテスト自身の書込にしか
効かない。

対策として ``clear_residual`` フィクスチャを提供する: テストが開いた
トランザクションの**内側で**対象テーブルを DELETE し、テスト終了時の
rollback で削除ごと巻き戻す。commit しないため共有 DB の実データは無傷の
まま、テストからは空のテーブルに見える。

TRUNCATE ではなく DELETE を使う理由: TRUNCATE は ACCESS EXCLUSIVE ロックを
取り、並行して動く取込ジョブをブロックする。DELETE は ROW EXCLUSIVE で済む
(削除行の行ロックは rollback まで保持されるが、テストは短命なので許容)。
"""

from __future__ import annotations

import pytest

# 取込・前処理テストが件数 assert の対象にするテーブル。FK の子 → 親の順。
RESIDUAL_TABLES = (
    "meta.lineage_edges",
    "docs.embeddings",           # docs.documents への FK 子
    "docs.documents_enriched",
    "docs.preprocess_pending",
    "docs.triage_queue",
    "docs.documents",
    "market.indicators",
    "market.bars",
    "market.calendar_events",
    "press.outbox",
)

# ledger.evidence は ledger.journal_entries / ledger.reconciliations から
# FK 参照され得るため、未参照行のみ削除する(取込由来の証憑は未参照)。
_CLEAR_EVIDENCE_SQL = """
    DELETE FROM ledger.evidence e
    WHERE NOT EXISTS (SELECT 1 FROM ledger.journal_entries j
                      WHERE j.evidence_id = e.evidence_id)
      AND NOT EXISTS (SELECT 1 FROM ledger.reconciliations r
                      WHERE r.evidence_id = e.evidence_id)
"""


@pytest.fixture
def clear_residual():
    """接続を受け取り、そのトランザクション内で残留データを不可視にする関数。

    呼び出し側の ``conn`` フィクスチャが接続直後に呼ぶ。**commit してはならない**
    (rollback で削除が巻き戻ることが共有 DB を守る前提)。
    """

    def _clear(conn) -> None:
        with conn.cursor() as cur:
            for table in RESIDUAL_TABLES:
                cur.execute(f"DELETE FROM {table}")  # noqa: S608 - 固定リスト
            cur.execute(_CLEAR_EVIDENCE_SQL)

    return _clear
