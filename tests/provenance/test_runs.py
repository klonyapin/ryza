"""run ライフサイクルの受け入れ基準テスト。

- コンテキストマネージャが正常系 success / 例外系 failed を記録
- code_version が env ``RYZA_CODE_VERSION`` → git describe の順で取得される
- add_cost がモデル階層別にコストを集計する

すべて共有 ``conn``(rollback 隔離)を渡して実行する。
"""

from __future__ import annotations

import psycopg
import pytest

from ryza.provenance.runs import (
    CODE_VERSION_ENV,
    RUN_STATUSES,
    _git_code_version,
    run,
    start_run,
)


def _fetch_run(conn, run_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT job_name, code_version, status, started_at, finished_at, cost "
            "FROM meta.runs WHERE run_id = %s",
            (run_id,),
        )
        return cur.fetchone()


def test_start_run_creates_running_row(conn):
    r = start_run("ingest.jquants.daily", {"symbols": ["7203.T"]}, conn=conn)
    job_name, code_version, status, started_at, finished_at, cost = _fetch_run(conn, r.run_id)
    assert job_name == "ingest.jquants.daily"
    assert status == "running"
    assert started_at is not None
    assert finished_at is None
    # code_version は git describe 由来(このリポジトリは git なので 'unknown' にはならない)。
    assert code_version and code_version != "unknown"


# ── code_version の解決順(独立役員 再審査 条件2)──────────────────────────────
# コンテナには .git が無いため git describe は必ず失敗する。デプロイが注入する
# env を最優先で読まないと meta.runs.code_version が 'unknown' になり、
# リネージ(不変原則3)が成立しない。


def test_code_version_prefers_injected_env(monkeypatch):
    monkeypatch.setenv(CODE_VERSION_ENV, "0123456789abcdef0123456789abcdef01234567")
    assert _git_code_version() == "0123456789abcdef0123456789abcdef01234567"


def test_code_version_ignores_blank_env(monkeypatch):
    monkeypatch.setenv(CODE_VERSION_ENV, "   ")
    assert _git_code_version() != "   "  # git describe へフォールバックする


def test_start_run_records_injected_code_version(conn, monkeypatch):
    monkeypatch.setenv(CODE_VERSION_ENV, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
    r = start_run("dashboard.boardroom.chat", conn=conn)
    _, code_version, *_ = _fetch_run(conn, r.run_id)
    assert code_version == "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def test_context_manager_success(conn):
    with run("research.macro", conn=conn) as r:
        run_id = r.run_id
    _, _, status, _, finished_at, _ = _fetch_run(conn, run_id)
    assert status == "success"
    assert finished_at is not None


def test_context_manager_failed_on_exception(conn):
    with pytest.raises(ValueError, match="boom"):
        with run("research.micro", conn=conn) as r:
            run_id = r.run_id
            raise ValueError("boom")
    _, _, status, _, finished_at, _ = _fetch_run(conn, run_id)
    assert status == "failed"
    assert finished_at is not None


def test_add_cost_accumulates_by_tier(conn):
    with run("press.morning", conn=conn) as r:
        r.add_cost("mid", tokens=1000, cost_estimate=0.03)
        r.add_cost("mid", tokens=500, cost_estimate=0.015)
        r.add_cost("fable", tokens=2000, cost_estimate=0.5)
        run_id = r.run_id
    _, _, _, _, _, cost = _fetch_run(conn, run_id)
    assert cost["by_tier"]["mid"] == {"tokens": 1500, "cost_estimate": 0.045, "calls": 2}
    assert cost["by_tier"]["fable"] == {"tokens": 2000, "cost_estimate": 0.5, "calls": 1}
    assert cost["total_tokens"] == 3500
    assert cost["total_cost_estimate"] == pytest.approx(0.545)


def test_params_stored_as_jsonb(conn):
    r = start_run("ingest.edinet", {"date": "2026-08-02", "types": ["filing"]}, conn=conn)
    with conn.cursor() as cur:
        cur.execute("SELECT params FROM meta.runs WHERE run_id = %s", (r.run_id,))
        params = cur.fetchone()[0]
    assert params == {"date": "2026-08-02", "types": ["filing"]}


def test_record_runtime_keeps_input_params_immutable(conn):
    """実行時の観測値は params['runtime'] に隔離され、入力証跡は書き換わらない。"""
    r = start_run("dashboard.boardroom.meeting", {"speaker_tier": "fable"}, conn=conn)
    r.record_runtime({"rounds": 2, "roles": [["cio"], ["audit"]]})
    r.record_runtime({"guard_fired": True})  # 追記はマージされる
    r.record_runtime({})  # 空パッチは何もしない
    with conn.cursor() as cur:
        cur.execute("SELECT params FROM meta.runs WHERE run_id = %s", (r.run_id,))
        params = cur.fetchone()[0]
    assert params["speaker_tier"] == "fable"  # 入力は不変
    assert params["runtime"] == {
        "rounds": 2, "roles": [["cio"], ["audit"]], "guard_fired": True,
    }


def test_record_runtime_on_run_without_initial_params(conn):
    r = start_run("dashboard.boardroom.meeting", conn=conn)
    r.record_runtime({"roles": [["cio"]]})
    with conn.cursor() as cur:
        cur.execute("SELECT params FROM meta.runs WHERE run_id = %s", (r.run_id,))
        assert cur.fetchone()[0] == {"runtime": {"roles": [["cio"]]}}


# ── meta.runs.status の語彙凍結(0031 / 独立役員審査 0027 中-4)────────────────
# 0027 の部分索引 runs_running_idx は述語 `status = 'running'` を持つ。CHECK が無い間は
# 語彙を増やしても索引が黙って使われなくなるだけで、エラーも警告も出なかった。


def test_status_check_freezes_vocabulary(conn):
    """語彙外の status は DB が拒否する(0031 の runs_status_check)。"""
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                """
                INSERT INTO meta.runs (job_name, code_version, started_at, status)
                VALUES ('test.status', 'test', now(), 'retrying')
                """
            )
    conn.rollback()


@pytest.mark.parametrize("status", RUN_STATUSES)
def test_known_statuses_pass_the_check(conn, status):
    """既存の語彙 running / success / failed は通る(既存行を落とさない)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO meta.runs (job_name, code_version, started_at, status)
            VALUES ('test.status', 'test', now(), %s) RETURNING run_id
            """,
            (status,),
        )
        assert cur.fetchone()[0] > 0
    conn.rollback()


def test_finish_rejects_unknown_status_before_touching_db(conn):
    """writer 側で先に弾く — CheckViolation は呼び出し側トランザクションを巻き添えにする。

    共有接続で ``finish('partial')`` のような値を渡すと、DB 側の CHECK 違反は
    そのジョブが同じトランザクションで書いた成果物ごと巻き戻す。writer で落として
    壊すのを呼び出しだけに留める(一次統制は 0031 の CHECK のまま)。
    """
    r = start_run("test.status.writer", conn=conn)
    with pytest.raises(ValueError, match="未知の run status"):
        r.finish("partial")
    # トランザクションは生きている = 続けて書ける。
    assert start_run("test.status.writer2", conn=conn).run_id > 0
    conn.rollback()


def test_run_status_constant_matches_db_check(conn):
    """定数と DB の CHECK が同じ語彙を指す(片方だけ増やす事故を検出する)。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'runs_status_check'"
        )
        row = cur.fetchone()
    assert row is not None, "0031 の runs_status_check が存在しない"
    definition = row[0]
    for status in RUN_STATUSES:
        assert f"'{status}'" in definition, f"CHECK に {status} が無い"
    # 索引の述語も同じ語彙に乗っている(0027 索引4 が沈黙劣化しないための紐付け)。
    with conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = 'meta' AND indexname = 'runs_running_idx'"
        )
        indexdef = cur.fetchone()[0]
    assert "status = 'running'" in indexdef
    conn.rollback()
