"""ops/deploy-dashboard.sh が生成するロール権限ゲートのリグレッションテスト。

独立役員 再審査(docs/reviews/dashboard-deploy-independent-review.md「次回 PR 対応」第1項)
への対応。是正前、``dashboard_write_grants`` などの検証クエリは**デプロイログに出力される
だけ**で、値が想定外でもスクリプトは進んだ。是正後は SQL 末尾の ``DO $ryza_gate$`` ブロックが
``RAISE EXCEPTION`` を上げ、``psql -v ON_ERROR_STOP=1`` 経由でデプロイを中断する。

ここでは意見でなく実測で決着させる(議論規約4)。deploy-dashboard.sh に埋め込まれた
SQL 生成器を**実際に実行**して DO ブロックを取り出し、実 PostgreSQL 上に一時ロールを
作って各項目が想定どおり例外を上げることを確かめる。一時ロールとその GRANT は
トランザクション内で作り、rollback で消す(PostgreSQL では CREATE ROLE / GRANT も
トランザクショナル)。実 GCP には触れない — psql も gcloud も使わず、DO ブロックだけを
psycopg で実行する。
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import errors

from ryza.db.conn import connect

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "ops" / "deploy-dashboard.sh"

# 役員室ロールが書けてよい ops の表(0020)。ゲート 4.5/4.6 の期待値。
BOARDROOM_OPS_TABLES = ("ops.org_icon_overrides", "ops.org_icon_override_log")


def generate_role_sql(dash_role: str, br_role: str) -> str:
    """deploy-dashboard.sh 内の SQL 生成器(python3 ヒアドキュメント)をそのまま実行する。

    生成器を切り出さずスクリプト本文から抽出するのは、本体とテストの二重管理を避け、
    本体を書き換えたらテストが必ず追随するようにするため。
    """
    text = DEPLOY.read_text(encoding="utf-8")
    m = re.search(r"python3 - <<'PY'\n(.*?)\nPY\n", text, re.S)
    assert m, "deploy-dashboard.sh から SQL 生成器を抽出できない(ヒアドキュメントの形が変わった?)"
    env = dict(
        os.environ,
        RYZA_DASH_PW="dummy-dash-password",
        RYZA_BR_PW="dummy-br-password",
        RYZA_DASH_ROLE=dash_role,
        RYZA_BR_ROLE=br_role,
        RYZA_DB="ryza",
        RYZA_OWNER="ryza",
    )
    proc = subprocess.run(
        ["python3", "-c", m.group(1)], capture_output=True, text=True, env=env, check=True
    )
    return base64.b64decode(proc.stdout).decode("utf-8")


def extract_gate_block(sql: str) -> str:
    m = re.search(r"DO \$ryza_gate\$.*?\$ryza_gate\$;", sql, re.S)
    assert m, "生成 SQL にロール権限ゲート(DO $ryza_gate$ ブロック)が無い"
    return m.group(0)


def extract_gate_statements(sql: str) -> list[str]:
    """ゲートの実行に必要な文(権限ビュー + DO ブロック)を順に返す。"""
    view = re.search(r"CREATE OR REPLACE TEMP VIEW ryza_gate_grants AS.*?;", sql, re.S)
    assert view, "生成 SQL に権限ビュー ryza_gate_grants が無い(C-3 の列レベル併用)"
    return [view.group(0), extract_gate_block(sql)]


# ── 生成 SQL の静的検査(DB 不要)──────────────────────────────────────────────


def test_generated_sql_contains_gate_with_all_assertions() -> None:
    sql = generate_role_sql("ryza_dashboard", "ryza_boardroom")
    gate = extract_gate_block(sql)
    # 8つの例外(4.1〜4.7 の各項目。4.5 は「対象表の実在」と「権限表数」の2本)
    assert gate.count("RAISE EXCEPTION") == 8
    for marker in (
        "ロールが揃っていない",
        "ロール属性が想定外",
        "非 SELECT 権限が",
        "ops.discord_webhooks に権限を持つ",
        "役員室の書込先",  # C-4: 0020 未適用 DB を正常と誤認しない
        "ops 権限表数が想定外",
        "想定外テーブルに権限を持つ",
        "追記オンリー違反",
    ):
        assert marker in gate, marker


def test_gate_reads_the_combined_privilege_view() -> None:
    """各検査が role_table_grants 直参照でなく列レベル併用のビューを見ていること(C-3)。"""
    sql = generate_role_sql("ryza_dashboard", "ryza_boardroom")
    gate = extract_gate_block(sql)
    assert "information_schema.role_table_grants" not in gate
    assert gate.count("FROM ryza_gate_grants") == 5
    view = extract_gate_statements(sql)[0]
    assert "information_schema.role_table_grants" in view
    assert "information_schema.column_privileges" in view


def test_generated_sql_substitutes_role_names_inside_the_gate() -> None:
    """プレースホルダの置換漏れがあるとゲートは別ロールを検査してしまう。"""
    gate = extract_gate_block(generate_role_sql("dash_x", "br_x"))
    assert "__DASH_ROLE__" not in gate
    assert "__BR_ROLE__" not in gate
    assert "'dash_x'" in gate
    assert "'br_x'" in gate


def test_generated_sql_keeps_evidence_queries() -> None:
    """ゲート化しても、デプロイログに残す証跡(\\echo + SELECT)は落とさない。"""
    sql = generate_role_sql("ryza_dashboard", "ryza_boardroom")
    for name in (
        "dashboard_write_grants",
        "dashboard_secret_grants",
        "boardroom_ops_tables",
        "boardroom_unexpected_ops_grants",
        "boardroom_log_mutation_grants",
    ):
        assert name in sql, name


# ── 実 PostgreSQL に対するゲートの実測 ─────────────────────────────────────────


@pytest.fixture
def gate_env(migrated_db):
    """一時ロール2つ+接続。トランザクション内で作り、テスト終了時に rollback で消す。"""
    suffix = uuid.uuid4().hex[:10]
    dash = f"tst_dash_{suffix}"
    br = f"tst_br_{suffix}"
    conn = connect()
    try:
        try:
            conn.execute(f'CREATE ROLE "{dash}" LOGIN NOINHERIT')
        except errors.InsufficientPrivilege:
            pytest.skip("CREATE ROLE 権限が無い(スーパーユーザーでない接続)")
        yield conn, dash, br
    finally:
        conn.rollback()
        conn.close()


def _create_boardroom(conn: psycopg.Connection, br: str, *, attrs: str = "LOGIN NOINHERIT") -> None:
    conn.execute(f'CREATE ROLE "{br}" {attrs}')


def _grant_expected(conn: psycopg.Connection, dash: str, br: str) -> None:
    """本番の GRANT 構成を再現する(読取専用+役員室の ops 2表)。"""
    conn.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA ops TO "{dash}"')
    # 秘密テーブルは一括 GRANT から明示的に外す(deploy-dashboard.sh の REVOKE と同じ)
    conn.execute(f'REVOKE ALL ON ops.discord_webhooks FROM "{dash}"')
    conn.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ops.org_icon_overrides TO "{br}"')
    conn.execute(f'GRANT INSERT ON ops.org_icon_override_log TO "{br}"')


def _run_gate(conn: psycopg.Connection, dash: str, br: str) -> None:
    for stmt in extract_gate_statements(generate_role_sql(dash, br)):
        conn.execute(stmt)


def test_gate_passes_on_the_intended_configuration(gate_env) -> None:
    conn, dash, br = gate_env
    _create_boardroom(conn, br)
    _grant_expected(conn, dash, br)
    _run_gate(conn, dash, br)  # 例外が上がらなければ OK


def test_gate_aborts_when_a_role_is_missing(gate_env) -> None:
    conn, dash, br = gate_env  # br は作らない
    with pytest.raises(errors.RaiseException, match="ロールが揃っていない"):
        _run_gate(conn, dash, br)


def test_gate_aborts_when_readonly_role_inherits_owner(gate_env) -> None:
    """旧版の `CREATE ROLE ... IN ROLE ryza`(全権限継承・重大-2)が復活したら落とす。"""
    conn, dash, br = gate_env
    _create_boardroom(conn, br)
    _grant_expected(conn, dash, br)
    conn.execute(f'GRANT ryza TO "{dash}"')
    with pytest.raises(errors.RaiseException, match="ロール属性が想定外"):
        _run_gate(conn, dash, br)


def test_gate_aborts_when_role_is_inherit(gate_env) -> None:
    conn, dash, br = gate_env
    _create_boardroom(conn, br)
    _grant_expected(conn, dash, br)
    conn.execute(f'ALTER ROLE "{dash}" INHERIT')
    with pytest.raises(errors.RaiseException, match="ロール属性が想定外"):
        _run_gate(conn, dash, br)


def test_gate_aborts_when_role_cannot_login(gate_env) -> None:
    conn, dash, br = gate_env
    _create_boardroom(conn, br, attrs="NOLOGIN NOINHERIT")
    _grant_expected(conn, dash, br)
    with pytest.raises(errors.RaiseException, match="ロール属性が想定外"):
        _run_gate(conn, dash, br)


def test_gate_aborts_on_write_grant_to_readonly_role(gate_env) -> None:
    """``dashboard_write_grants`` がログ出力だけだった箇所(次回 PR 対応 第1項)。"""
    conn, dash, br = gate_env
    _create_boardroom(conn, br)
    _grant_expected(conn, dash, br)
    conn.execute(f'GRANT INSERT ON ops.org_icon_overrides TO "{dash}"')
    with pytest.raises(errors.RaiseException, match="非 SELECT 権限が"):
        _run_gate(conn, dash, br)


def test_gate_aborts_when_readonly_role_can_read_webhook_secrets(gate_env) -> None:
    """ops.discord_webhooks.webhook_url は秘密(0017)。SELECT でも許さない。"""
    conn, dash, br = gate_env
    _create_boardroom(conn, br)
    _grant_expected(conn, dash, br)
    conn.execute(f'GRANT SELECT ON ops.discord_webhooks TO "{dash}"')
    with pytest.raises(errors.RaiseException, match="ops.discord_webhooks に権限を持つ"):
        _run_gate(conn, dash, br)


def test_gate_aborts_when_boardroom_grants_were_skipped(gate_env) -> None:
    """to_regclass ガード付き GRANT が黙ってスキップされた状態を検出する(0020 C-5)。"""
    conn, dash, br = gate_env
    _create_boardroom(conn, br)
    conn.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA ops TO "{dash}"')
    conn.execute(f'REVOKE ALL ON ops.discord_webhooks FROM "{dash}"')
    with pytest.raises(errors.RaiseException, match="ops 権限表数が想定外"):
        _run_gate(conn, dash, br)


def test_gate_aborts_when_boardroom_reaches_unexpected_ops_table(gate_env) -> None:
    """Kill Switch(ops.trading_state)への経路が出来ていないこと。"""
    conn, dash, br = gate_env
    _create_boardroom(conn, br)
    conn.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA ops TO "{dash}"')
    conn.execute(f'REVOKE ALL ON ops.discord_webhooks FROM "{dash}"')
    # 表数は 2 のまま(4.5 を通過させ、4.6 の想定外テーブル検査を狙い撃ちする)
    conn.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ops.org_icon_overrides TO "{br}"')
    conn.execute(f'GRANT SELECT ON ops.trading_state TO "{br}"')
    with pytest.raises(errors.RaiseException, match="想定外テーブルに権限を持つ"):
        _run_gate(conn, dash, br)


def test_gate_aborts_when_override_log_is_mutable(gate_env) -> None:
    """履歴表は追記オンリー。UPDATE を持てば履歴を書き換えられる。"""
    conn, dash, br = gate_env
    _create_boardroom(conn, br)
    _grant_expected(conn, dash, br)
    conn.execute(f'GRANT UPDATE ON ops.org_icon_override_log TO "{br}"')
    with pytest.raises(errors.RaiseException, match="追記オンリー違反"):
        _run_gate(conn, dash, br)


def test_gate_aborts_when_target_tables_are_missing(gate_env) -> None:
    """0020 未適用の DB を『権限 0 で一致』と誤認しない(C-4)。

    是正前は期待値を実在表数から算出していたため、実在 0・GRANT 0 が一致と判定され、
    役員室が何も書けないダッシュボードを正常として本番化しえた。

    「表が無い DB」は実表を DROP せず、ゲートが参照する表名を実在しない名前へ
    差し替えて再現する。DROP TABLE は ACCESS EXCLUSIVE ロックを取り、同じテスト DB を
    使う他テストと干渉するため(tests/conftest.py の隔離方針)。
    """
    conn, dash, br = gate_env
    _create_boardroom(conn, br)
    _grant_expected(conn, dash, br)
    absent = f"ops.absent_{uuid.uuid4().hex[:8]}"
    stmts = [
        s.replace("ops.org_icon_overrides", f"{absent}_a").replace(
            "ops.org_icon_override_log", f"{absent}_b"
        )
        for s in extract_gate_statements(generate_role_sql(dash, br))
    ]
    with pytest.raises(errors.RaiseException, match="役員室の書込先"):
        for stmt in stmts:
            conn.execute(stmt)


# ── 列レベル GRANT の検出(C-3)─────────────────────────────────────────────────


def test_column_level_grant_is_invisible_to_role_table_grants(gate_env) -> None:
    """なぜ column_privileges の併用が要るのかを実測で固定する。

    列レベル GRANT は role_table_grants に1行も現れない。是正前のゲートはこの view
    だけを見ていたため、列指定の GRANT は検査にもログにも映らず素通りしていた。
    """
    conn, _dash, br = gate_env
    _create_boardroom(conn, br)
    conn.execute(f'GRANT UPDATE (state) ON ops.trading_state TO "{br}"')
    table_rows = conn.execute(
        "SELECT count(*) FROM information_schema.role_table_grants WHERE grantee = %s",
        (br,),
    ).fetchone()[0]
    column_rows = conn.execute(
        "SELECT count(*) FROM information_schema.column_privileges WHERE grantee = %s",
        (br,),
    ).fetchone()[0]
    assert table_rows == 0
    assert column_rows > 0


def test_gate_aborts_on_column_level_grant_to_kill_switch(gate_env) -> None:
    """ops.trading_state への列 UPDATE(Kill Switch 状態の偽装経路)を検出する。"""
    conn, dash, br = gate_env
    _create_boardroom(conn, br)
    conn.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA ops TO "{dash}"')
    conn.execute(f'REVOKE ALL ON ops.discord_webhooks FROM "{dash}"')
    # 権限表数は 2 のまま(4.5 を通過させ、4.6 の想定外テーブル検査を狙い撃ちする)
    conn.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ops.org_icon_overrides TO "{br}"')
    conn.execute(f'GRANT UPDATE (state) ON ops.trading_state TO "{br}"')
    with pytest.raises(errors.RaiseException, match="想定外テーブルに権限を持つ"):
        _run_gate(conn, dash, br)


def test_gate_aborts_on_column_level_write_to_readonly_role(gate_env) -> None:
    """読取専用ロールへの列 INSERT を検出する。"""
    conn, dash, br = gate_env
    _create_boardroom(conn, br)
    _grant_expected(conn, dash, br)
    conn.execute(f'GRANT INSERT (icon_url) ON ops.org_icon_overrides TO "{dash}"')
    with pytest.raises(errors.RaiseException, match="非 SELECT 権限が"):
        _run_gate(conn, dash, br)


def test_gate_aborts_on_column_level_update_of_override_log(gate_env) -> None:
    """履歴表への列 UPDATE を検出する(追記オンリーの実効化)。"""
    conn, dash, br = gate_env
    _create_boardroom(conn, br)
    _grant_expected(conn, dash, br)
    conn.execute(f'GRANT UPDATE (icon_url) ON ops.org_icon_override_log TO "{br}"')
    with pytest.raises(errors.RaiseException, match="追記オンリー違反"):
        _run_gate(conn, dash, br)
