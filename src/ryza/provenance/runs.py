"""runs — ジョブ実行(run)のライフサイクル管理。

設計書 §6(``meta.runs``)準拠。**規約(CLAUDE.md / 設計原則3)**: DB に書き込む全ジョブは
Run 経由で ``run_id`` を取得し、生成する全行に刻む。これがリネージの鍵になる。

``code_version`` は env ``RYZA_CODE_VERSION``(デプロイ時に注入されるコミット SHA)を
最優先で、無ければ ``git describe`` で自動取得する(実行時のコードを再現できるように)。

## 接続とトランザクション

- ``start_run(job_name, params)`` / コンテキストマネージャ ``run(...)`` に ``conn`` を
  渡さない場合、Run は自前の autocommit 接続を開いて所有する(``finish`` で閉じる)。
  実ジョブはこちら。running 行も最終ステータスも即時永続化される。
- ``conn`` を渡した場合は共有接続を使い、Run は commit も close もしない(呼び出し側が
  トランザクションを制御する)。テストや、既存トランザクションに参加したいときに使う。

典型:

    with run("ingest.jquants.daily", {"symbols": [...]}) as r:
        ...  # r.run_id を書き込む全行に刻む
        r.add_cost("mid", tokens=1200, cost_estimate=0.03)
    # 正常終了で status=success、例外送出で status=failed を記録して再送出
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ryza.db.conn import connect

# git describe をリポジトリルートで実行するための基準(src/ryza/provenance/runs.py から 3 つ上)。
_REPO_ROOT = Path(__file__).resolve().parents[3]


#: デプロイ時に注入される code_version(コミット SHA)。コンテナには .git が無く
#: git describe が使えないため、これが最優先の情報源になる(独立役員 再審査 条件2)。
CODE_VERSION_ENV = "RYZA_CODE_VERSION"


def _git_code_version() -> str:
    """code_version を取得する。env ``RYZA_CODE_VERSION`` → ``git describe`` → 'unknown'。

    コンテナ実行(Cloud Run / Cloud Run Jobs)では .git が同梱されないため、
    ``git describe`` は必ず失敗して 'unknown' になる。デプロイスクリプトが
    ``RYZA_CODE_VERSION`` にコミット SHA を注入するので、それを最優先で読む
    (不変原則3: 生成物に code_version を記録する)。
    """
    injected = os.environ.get(CODE_VERSION_ENV, "").strip()
    if injected:
        return injected
    try:
        proc = subprocess.run(
            ["git", "describe", "--always", "--dirty", "--tags"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


class Run:
    """1つのジョブ実行。``run_id`` を保持し、コスト加算と終了記録を提供する。

    通常は ``start_run`` / ``run`` から生成する。
    """

    def __init__(
        self,
        conn: psycopg.Connection,
        run_id: int,
        job_name: str,
        *,
        owns_conn: bool = False,
    ) -> None:
        self._conn = conn
        self._owns_conn = owns_conn
        self._finished = False
        self.run_id = run_id
        self.job_name = job_name

    def add_cost(self, model_tier: str, tokens: int, cost_estimate: float) -> None:
        """モデル階層別のコストを ``meta.runs.cost`` に加算する(経営管理部が集計)。

        構造: ``{"by_tier": {tier: {tokens, cost_estimate, calls}},
        "total_tokens": ..., "total_cost_estimate": ...}``。
        """
        with self._conn.cursor() as cur:
            cur.execute("SELECT cost FROM meta.runs WHERE run_id = %s", (self.run_id,))
            row = cur.fetchone()
            current: dict[str, Any] = row[0] if row and row[0] else {}
            by_tier: dict[str, Any] = current.get("by_tier", {})
            tier = by_tier.get(model_tier, {"tokens": 0, "cost_estimate": 0.0, "calls": 0})
            tier["tokens"] += tokens
            tier["cost_estimate"] += cost_estimate
            tier["calls"] += 1
            by_tier[model_tier] = tier
            merged = {
                "by_tier": by_tier,
                "total_tokens": sum(t["tokens"] for t in by_tier.values()),
                "total_cost_estimate": sum(t["cost_estimate"] for t in by_tier.values()),
            }
            cur.execute(
                "UPDATE meta.runs SET cost = %s WHERE run_id = %s",
                (Jsonb(merged), self.run_id),
            )
        self._commit_if_owned()

    def record_runtime(self, patch: dict[str, Any]) -> None:
        """実行中に判明した情報を ``params['runtime']`` へ追記する。

        開始時点では決まらない値(例: 役員室会議で進行役が選んだ発言者)を後から記録
        するための口。**入力証跡(start_run が書いた params 本体)は書き換えない** —
        実行時の観測値は ``runtime`` 名前空間に隔離する(独立役員審査 2026-08-03 C-7)。
        マージは SQL 側の ``||`` で行い、read-modify-write の競合を作らない。
        """
        if not patch:
            return
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE meta.runs
                SET params = coalesce(params, '{}'::jsonb) || jsonb_build_object(
                        'runtime',
                        coalesce(params -> 'runtime', '{}'::jsonb) || %s::jsonb
                    )
                WHERE run_id = %s
                """,
                (Jsonb(patch), self.run_id),
            )
        self._commit_if_owned()

    def finish(self, status: str = "success") -> None:
        """実行を終了として記録する(``finished_at`` と ``status`` を更新)。

        status: ``success`` | ``failed``(``running`` からの遷移)。二重呼び出しは無視。
        自前接続を所有する場合は最後に閉じる。
        """
        if self._finished:
            return
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE meta.runs SET status = %s, finished_at = now() WHERE run_id = %s",
                (status, self.run_id),
            )
        self._commit_if_owned()
        self._finished = True
        if self._owns_conn:
            self._conn.close()

    def _commit_if_owned(self) -> None:
        # 所有接続は autocommit なので明示 commit は不要。共有接続は呼び出し側が制御。
        pass


def start_run(
    job_name: str,
    params: dict[str, Any] | None = None,
    *,
    conn: psycopg.Connection | None = None,
) -> Run:
    """``meta.runs`` に ``running`` 行を作成し ``Run`` を返す。

    ``conn`` 省略時は autocommit 接続を新規に開いて Run が所有する。``conn`` 指定時は
    その共有接続を使い(commit しない)、呼び出し側のトランザクションに参加する。
    """
    code_version = _git_code_version()
    owns_conn = conn is None
    if conn is None:
        conn = connect(autocommit=True)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO meta.runs (job_name, code_version, started_at, status, params)
            VALUES (%s, %s, now(), 'running', %s)
            RETURNING run_id
            """,
            (job_name, code_version, Jsonb(params) if params is not None else None),
        )
        run_id = cur.fetchone()[0]
    return Run(conn, run_id, job_name, owns_conn=owns_conn)


@contextmanager
def run(
    job_name: str,
    params: dict[str, Any] | None = None,
    *,
    conn: psycopg.Connection | None = None,
) -> Iterator[Run]:
    """ジョブ実行のコンテキストマネージャ。

    正常終了で ``status=success``、例外送出で ``status=failed`` を記録して再送出する。
    """
    r = start_run(job_name, params, conn=conn)
    try:
        yield r
    except Exception:
        r.finish("failed")
        raise
    else:
        r.finish("success")
