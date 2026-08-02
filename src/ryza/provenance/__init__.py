"""provenance — 全部門が使う横断基盤。

- ``evidence``: 証憑ストア(不変保存 + sha256 改竄検知 + 重複排除)。
- ``runs``: ジョブ実行(run)のライフサイクルと code_version / コストの記録。
- ``lineage``: 成果物 → 入力の辺(``meta.lineage_edges``)の記録と遡及クエリ。

これらは会計エンジン(``ryza.ledger``)・データ基盤・リサーチ各部門から共通に呼ばれる。
DB への書き込みは基本的に呼び出し側の psycopg 接続(トランザクション)を受け取り、
本モジュールは commit しない(呼び出し側が制御する)。詳細は各モジュールの docstring を参照。
"""

from __future__ import annotations

from ryza.provenance.evidence import (
    EvidenceStorage,
    EvidenceStore,
    GcsStorage,
    LocalStorage,
)
from ryza.provenance.lineage import LineageNode, record, trace_back, trace_forward
from ryza.provenance.runs import Run, run, start_run

__all__ = [
    "EvidenceStorage",
    "EvidenceStore",
    "GcsStorage",
    "LocalStorage",
    "LineageNode",
    "Run",
    "record",
    "run",
    "start_run",
    "trace_back",
    "trace_forward",
]
