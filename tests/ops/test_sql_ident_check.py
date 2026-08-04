"""SQL 識別子検証(ops/lib/sql_ident_check.sh)のリグレッションテスト。

A-12 F-8(pass4-1・裁定 §3 中)への対応。deploy-dashboard.sh がロール名 env を
検証なしで SQL 識別子に埋め込んでいた点を、入口の関数(assert_sql_ident)で塞いだ。
「設定がそう書いてある」ではなく「想定外の入力で実際に中断すること」を機械的に
確かめる(tests/ops/test_deploy_guards.py と同じ思想)。

テスト方式は tests/ops/test_deploy_guards.py に倣い pytest + bash 関数の直接実行。
bats は CI に無く、既存の uv + pytest + ruff で完結させるほうが安く回る(同ファイル
docstring 参照)。関数を PATH の副作用なしに source して直接叩ける。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "ops" / "lib" / "sql_ident_check.sh"
DEPLOY = REPO_ROOT / "ops" / "deploy-dashboard.sh"


def _run_assert(name: str, value: str) -> subprocess.CompletedProcess[str]:
    """assert_sql_ident を単発で走らせる。値はシェルに引数として渡す(quote 込みで検査)。"""
    # bash の $1 経由で渡す(親シェルの展開を経ずに値をそのまま届ける — `"` や `;` を
    # 含む値もそのままテストできる)。
    script = f'. "{LIB}"; assert_sql_ident "$1" "$2"'
    return subprocess.run(
        ["bash", "-c", script, "bash", name, value],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        check=False,
    )


# ── 合格 ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "ryza",
        "ryza_dashboard",
        "ryza_boardroom",
        "_underscore_head",
        "abc123",
        "a",  # 1 文字
        "a" * 63,  # 上限ちょうど
    ],
)
def test_valid_identifiers_pass(value: str) -> None:
    r = _run_assert("RYZA_TEST_ROLE", value)
    assert r.returncode == 0, r.stderr
    # stdout は空(受け取った値を垂れ流さない — 制御文字混入時の端末破壊を避ける)
    assert r.stdout == ""


# ── 不合格 ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "reason_marker"),
    [
        ("", "空"),
        ("Ryza", "形式が不正"),  # 大文字
        ("RYZA", "形式が不正"),
        ("ryza-dashboard", "形式が不正"),  # ハイフン
        ("ryza dashboard", "形式が不正"),  # 空白
        ("ryza dashboard extra", "形式が不正"),  # 空白複数
        ("ryza\tdashboard", "形式が不正"),  # タブ
        ("ryza\ndashboard", "形式が不正"),  # 改行
        ('ryza"drop', "形式が不正"),  # ダブルクォート(SQL 識別子の閉じ)
        ("ryza;drop", "形式が不正"),  # セミコロン
        ("ryza'x", "形式が不正"),  # シングルクォート
        ("1abc", "形式が不正"),  # 先頭数字
        ("ryza.dashboard", "形式が不正"),  # ドット
        ("ryza,br", "形式が不正"),  # カンマ
        ("ryza--", "形式が不正"),  # コメント記号
        ("ryza/*x*/", "形式が不正"),  # コメント
        ("ryzä", "形式が不正"),  # 非 ASCII
        ("役員室", "形式が不正"),  # 非 ASCII(日本語)
        ("a" * 64, "長すぎる"),  # 上限 +1 バイト
        ("a" * 128, "長すぎる"),
    ],
)
def test_invalid_identifiers_are_rejected(value: str, reason_marker: str) -> None:
    r = _run_assert("RYZA_TEST_ROLE", value)
    assert r.returncode != 0
    assert reason_marker in r.stderr
    # 診断メッセージに env 名が含まれること(「どの env が」を追える)
    assert "RYZA_TEST_ROLE" in r.stderr


def test_error_diagnostic_does_not_emit_raw_value_to_stdout() -> None:
    """制御文字混入時に端末が壊れないよう、値は stdout に出さない(stderr の printf %q で開示)。"""
    r = _run_assert("RYZA_TEST_ROLE", "bad\x1b[31mvalue")
    assert r.returncode != 0
    # stdout に生の値が漏れていない
    assert r.stdout == ""


def test_missing_argument_is_treated_as_caller_bug() -> None:
    """引数不足(値そのものが渡されない)は空文字とは区別せず、呼び出し側のバグとして落とす。"""
    script = f'. "{LIB}"; assert_sql_ident RYZA_TEST_ROLE'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert r.returncode != 0
    assert "呼び出し側のバグ" in r.stderr or "空" in r.stderr


def test_function_survives_set_e() -> None:
    """呼び出し側は set -euo pipefail 下で走る。関数の非ゼロ戻り値で親が途中終了せず、
    `|| exit 1` を経由すること。deploy-dashboard.sh がまさにこの形で呼ぶ。"""
    script = (
        f'set -euo pipefail; . "{LIB}"; '
        'if assert_sql_ident RYZA_TEST_ROLE "bad;name"; then echo CONTINUED; else echo ABORTED; fi'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert r.returncode == 0
    assert "ABORTED" in r.stdout


# ── 本体スクリプトとの配線(切り出しによるドリフト防止)─────────────────────────


def test_shell_syntax_is_valid() -> None:
    r = subprocess.run(["bash", "-n", str(LIB)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr


def test_deploy_script_sources_the_library() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    assert '. "${ROOT}/ops/lib/sql_ident_check.sh"' in text


@pytest.mark.parametrize(
    "env_name",
    ["RYZA_DASH_ROLE", "RYZA_BR_ROLE", "RYZA_OWNER"],
)
def test_deploy_script_validates_role_envs_with_abort(env_name: str) -> None:
    """ロール名 env の全箇所で assert_sql_ident が呼ばれ、`|| exit 1` が付いていること。
    付け忘れ = 統制の無効化(deploy-guards の流儀と同じ)。"""
    text = DEPLOY.read_text(encoding="utf-8")
    calls = [
        ln
        for ln in text.splitlines()
        if re.search(rf"assert_sql_ident\s+{env_name}\b", ln)
        and not ln.lstrip().startswith("#")
    ]
    assert calls, f"assert_sql_ident {env_name} の呼び出しが deploy-dashboard.sh に無い"
    for line in calls:
        assert "|| exit 1" in line, f"`|| exit 1` の無い検証呼び出し: {line!r}"


def test_deploy_script_validates_before_sql_generation() -> None:
    """検証は SQL 生成(ROLE_SQL_B64=…)より前で行われていること。後で検証しても手遅れ。"""
    text = DEPLOY.read_text(encoding="utf-8")
    lines = text.splitlines()
    validation_idx = next(
        (i for i, ln in enumerate(lines) if "assert_sql_ident RYZA_DASH_ROLE" in ln), None
    )
    sql_gen_idx = next(
        (i for i, ln in enumerate(lines) if ln.startswith('ROLE_SQL_B64="')), None
    )
    assert validation_idx is not None, "assert_sql_ident RYZA_DASH_ROLE の行が見つからない"
    assert sql_gen_idx is not None, "ROLE_SQL_B64 生成の行が見つからない"
    assert validation_idx < sql_gen_idx, (
        f"SQL 識別子検証(L{validation_idx + 1})が SQL 生成(L{sql_gen_idx + 1})より後にある"
    )
