"""デプロイ統制(ops/lib/deploy-guards.sh・ops/lib/pg_hba_check.sh)のリグレッションテスト。

独立役員 再審査(docs/reviews/dashboard-deploy-independent-review.md「次回 PR 対応」第3項)
への対応。統制コードは**壊れても静かに通る**ため、「設定がそう書いてある」ではなく
「想定外の入力で実際に中断する」ことを機械的に確かめる。

テスト方式に pytest + bash 関数の直接実行を選んだ理由:
  - bats は CI(.github/workflows/ci.yml)に無く、導入すると別ランタイムの追加になる。
    既存の CI は uv + pytest + ruff だけで完結しており、そこに乗せるのが最も安く回る。
  - deploy-dashboard.sh 全体を `--check-only` で走らせる案は、統制以外の副作用
    (gcloud の API 有効化・VM への SSH・Cloud Build)を全てスタブする必要があり、
    スタブの網羅性がテストの妥当性を左右してしまう。統制部分だけを関数として
    切り出し、gcloud だけを PATH 上のスタブに差し替えるほうが、検証対象と
    検証範囲が一致する。
  - 切り出しによる「本体が関数を呼ばなくなる」ドリフトは、末尾の配線テスト
    (test_deploy_script_wires_*)で本体スクリプトの実テキストを検査して塞ぐ。

実 GCP には一切触れない(gcloud は PATH 上のスタブ)。git は一時ディレクトリに作る
ローカルの bare リポジトリ + clone で完結し、ネットワークを使わない。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARDS = REPO_ROOT / "ops" / "lib" / "deploy-guards.sh"
PG_HBA_LIB = REPO_ROOT / "ops" / "lib" / "pg_hba_check.sh"
DEPLOY = REPO_ROOT / "ops" / "deploy-dashboard.sh"

# deploy-dashboard.sh が pg_hba に追記する1行(SUBNET_CIDR は実行時に決まる)。
DESIRED_HBA = "host    ryza    ryza_dashboard,ryza_boardroom    10.138.0.0/20    scram-sha-256"

# gcloud のスタブ。実 GCP を呼ばず、呼び出しを記録して既定のポリシーを返す。
#   STUB_STATE            … 状態ディレクトリ(呼び出しログ・ポリシーファイル)
#   STUB_SVC_FAIL=<n>     … n 回目の run services get-iam-policy を失敗させる
#   STUB_REMOVE_FAIL=1    … remove-iam-policy-binding を失敗させる
#   STUB_PROJ_FAIL=1      … projects get-iam-policy を失敗させる
GCLOUD_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "${STUB_STATE}/calls.log"
if [ "$1 $2 $3" = "run services get-iam-policy" ]; then
  n=$(( $(cat "${STUB_STATE}/svc_calls" 2>/dev/null || echo 0) + 1 ))
  printf '%s' "${n}" > "${STUB_STATE}/svc_calls"
  if [ "${STUB_SVC_FAIL:-}" = "${n}" ]; then
    echo "stub: get-iam-policy failed" >&2
    exit 1
  fi
  f="${STUB_STATE}/svc_policy_${n}"
  [ -f "${f}" ] || f="${STUB_STATE}/svc_policy_default"
  if [ -f "${f}" ]; then cat "${f}"; fi
  exit 0
fi
if [ "$1 $2 $3" = "run services remove-iam-policy-binding" ]; then
  if [ "${STUB_REMOVE_FAIL:-}" = "1" ]; then
    echo "stub: remove-iam-policy-binding failed" >&2
    exit 1
  fi
  exit 0
fi
if [ "$1 $2" = "projects get-iam-policy" ]; then
  if [ "${STUB_PROJ_FAIL:-}" = "1" ]; then
    echo "stub: projects get-iam-policy failed" >&2
    exit 1
  fi
  if [ -f "${STUB_STATE}/proj_policy" ]; then cat "${STUB_STATE}/proj_policy"; fi
  exit 0
fi
echo "stub: unexpected gcloud call: $*" >&2
exit 1
"""


def run_bash(
    script: str,
    *,
    env_extra: dict[str, str] | None = None,
    bin_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """bash -c でスニペットを実行する。bin_dir を PATH の先頭に足せる(gcloud スタブ用)。"""
    env = dict(os.environ)
    if bin_dir is not None:
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, check=False
    )


# ── git ゲート ──────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    """ローカル bare リポジトリ(origin)+ clone。clean かつ HEAD == origin/main の状態。"""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
    )
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "test")
    _git(work, "config", "commit.gpgsign", "false")
    (work / "README.md").write_text("initial\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "-u", "origin", "main")
    return work, str(origin)


def guard_git(root: str | Path, expected_origin: str) -> subprocess.CompletedProcess[str]:
    return run_bash(f'. "{GUARDS}"; guard_git_state "{root}" "{expected_origin}"')


def test_git_gate_passes_on_clean_head_matching_origin(repo: tuple[Path, str]) -> None:
    work, origin = repo
    head = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    r = guard_git(work, origin)
    assert r.returncode == 0, r.stderr
    # stdout はコミット SHA のみ(呼び出し側が code_version として受け取る)
    assert r.stdout.strip() == head


def test_git_gate_accepts_expected_origin_without_dot_git(repo: tuple[Path, str]) -> None:
    """`.git` の有無で弾かない(再審査 条件4 の注記)。"""
    work, origin = repo
    r = guard_git(work, origin.removesuffix(".git"))
    assert r.returncode == 0, r.stderr


def test_git_gate_aborts_on_dirty_worktree(repo: tuple[Path, str]) -> None:
    work, origin = repo
    (work / "README.md").write_text("tampered\n", encoding="utf-8")
    r = guard_git(work, origin)
    assert r.returncode == 1
    assert "未コミット" in r.stderr
    assert r.stdout.strip() == ""


def test_git_gate_aborts_on_untracked_file(repo: tuple[Path, str]) -> None:
    """未追跡ファイルもデプロイ対象に混入しうる(Cloud Build のコンテキストに入る)。"""
    work, origin = repo
    (work / "backdoor.py").write_text("print('x')\n", encoding="utf-8")
    r = guard_git(work, origin)
    assert r.returncode == 1
    assert "backdoor.py" in r.stderr


def test_git_gate_aborts_when_head_ahead_of_origin_main(repo: tuple[Path, str]) -> None:
    """未 push のコミット(= PR も承認記録も経ていないコード)で中断する。"""
    work, origin = repo
    (work / "README.md").write_text("local only\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "local only")
    r = guard_git(work, origin)
    assert r.returncode == 1
    assert "origin/main" in r.stderr


def test_git_gate_aborts_when_head_behind_origin_main(repo: tuple[Path, str]) -> None:
    """origin/main が進んでいる場合も一致しないので中断する。"""
    work, origin = repo
    other = work.parent / "other"
    subprocess.run(["git", "clone", str(origin), str(other)], check=True, capture_output=True)
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "test")
    _git(other, "config", "commit.gpgsign", "false")
    (other / "README.md").write_text("moved on\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "second")
    _git(other, "push", "origin", "main")
    r = guard_git(work, origin)
    assert r.returncode == 1
    assert "origin/main" in r.stderr


def test_git_gate_aborts_on_origin_url_mismatch(repo: tuple[Path, str]) -> None:
    """origin を差し替えれば HEAD==origin/main は容易に満たせる(再審査 条件4)。"""
    work, _origin = repo
    r = guard_git(work, "https://github.com/attacker/ryza")
    assert r.returncode == 1
    assert "origin が想定と違う" in r.stderr


def test_git_gate_aborts_when_origin_missing(repo: tuple[Path, str]) -> None:
    work, origin = repo
    _git(work, "remote", "remove", "origin")
    r = guard_git(work, origin)
    assert r.returncode == 1
    assert "origin が想定と違う" in r.stderr


def test_git_gate_aborts_outside_git_repository(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    r = guard_git(plain, "https://github.com/klonyapin/ryza")
    assert r.returncode == 1
    assert "git リポジトリではない" in r.stderr


# ── 公開バインディング検査 ──────────────────────────────────────────────────────


class GcloudStub:
    def __init__(self, tmp_path: Path) -> None:
        self.bin_dir = tmp_path / "stub-bin"
        self.bin_dir.mkdir()
        self.state = tmp_path / "stub-state"
        self.state.mkdir()
        exe = self.bin_dir / "gcloud"
        exe.write_text(GCLOUD_STUB, encoding="utf-8")
        exe.chmod(0o755)

    def set_service_policy(self, text: str, call: int | None = None) -> None:
        name = "svc_policy_default" if call is None else f"svc_policy_{call}"
        (self.state / name).write_text(text, encoding="utf-8")

    def set_project_policy(self, text: str) -> None:
        (self.state / "proj_policy").write_text(text, encoding="utf-8")

    @property
    def calls(self) -> list[str]:
        log = self.state / "calls.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def run(self, snippet: str, **env_extra: str) -> subprocess.CompletedProcess[str]:
        return run_bash(
            f'. "{GUARDS}"; {snippet}',
            bin_dir=self.bin_dir,
            env_extra={"STUB_STATE": str(self.state), **env_extra},
        )


@pytest.fixture
def gcloud(tmp_path: Path) -> GcloudStub:
    return GcloudStub(tmp_path)


SERVICE_GUARD = 'guard_service_public_bindings ryza-dashboard ryza-main us-west1'
PROJECT_GUARD = 'guard_project_public_bindings ryza-main'


def test_service_guard_passes_when_no_public_binding(gcloud: GcloudStub) -> None:
    gcloud.set_service_policy("roles/run.invoker\tuser:rep@example.com\n")
    r = gcloud.run(SERVICE_GUARD)
    assert r.returncode == 0, r.stderr
    assert "公開バインディングは無し" in r.stdout
    assert not any("remove-iam-policy-binding" in c for c in gcloud.calls)


def test_service_guard_aborts_when_policy_fetch_fails(gcloud: GcloudStub) -> None:
    """取得失敗を『公開なし』と混同しない(再審査 条件1)。"""
    r = gcloud.run(SERVICE_GUARD, STUB_SVC_FAIL="1")
    assert r.returncode == 1
    assert "確認できないため中断" in r.stderr


def test_service_guard_removes_public_binding_and_verifies(gcloud: GcloudStub) -> None:
    gcloud.set_service_policy("roles/run.invoker\tallUsers\n", call=1)
    gcloud.set_service_policy("roles/run.invoker\tuser:rep@example.com\n", call=2)
    r = gcloud.run(SERVICE_GUARD)
    assert r.returncode == 0, r.stderr
    removes = [c for c in gcloud.calls if "remove-iam-policy-binding" in c]
    assert len(removes) == 1
    assert "--member=allUsers" in removes[0]
    assert "--role=roles/run.invoker" in removes[0]


def test_service_guard_aborts_when_binding_survives_removal(gcloud: GcloudStub) -> None:
    """除去したつもりで残っていたら『完了』にしない。"""
    gcloud.set_service_policy("roles/run.invoker\tallUsers\n")
    r = gcloud.run(SERVICE_GUARD)
    assert r.returncode == 1
    assert "全世界公開のまま" in r.stderr


def test_service_guard_aborts_when_removal_command_fails(gcloud: GcloudStub) -> None:
    gcloud.set_service_policy("roles/run.invoker\tallUsers\n")
    r = gcloud.run(SERVICE_GUARD, STUB_REMOVE_FAIL="1")
    assert r.returncode == 1
    assert "除去できなかった" in r.stderr


def test_service_guard_aborts_when_reverify_fetch_fails(gcloud: GcloudStub) -> None:
    gcloud.set_service_policy("roles/run.invoker\tallUsers\n", call=1)
    r = gcloud.run(SERVICE_GUARD, STUB_SVC_FAIL="2")
    assert r.returncode == 1
    assert "再取得できなかった" in r.stderr


def test_service_guard_detects_all_authenticated_users(gcloud: GcloudStub) -> None:
    """allAuthenticatedUsers は『Google アカウントを持つ全員』であり非公開ではない。"""
    gcloud.set_service_policy("roles/run.invoker\tallAuthenticatedUsers\n", call=1)
    gcloud.set_service_policy("", call=2)
    r = gcloud.run(SERVICE_GUARD)
    assert r.returncode == 0, r.stderr
    assert any("--member=allAuthenticatedUsers" in c for c in gcloud.calls)


def test_service_guard_does_not_false_positive_on_lookalike_member(gcloud: GcloudStub) -> None:
    """`user:allUsers@example.com` は公開ではない(部分一致で誤検出しないこと)。"""
    gcloud.set_service_policy("roles/run.invoker\tuser:allUsers@example.com\n")
    r = gcloud.run(SERVICE_GUARD)
    assert r.returncode == 0, r.stderr
    assert not any("remove-iam-policy-binding" in c for c in gcloud.calls)


def test_project_guard_passes_on_clean_policy(gcloud: GcloudStub) -> None:
    gcloud.set_project_policy("roles/owner\tuser:rep@example.com\n")
    r = gcloud.run(PROJECT_GUARD)
    assert r.returncode == 0, r.stderr
    assert "プロジェクトレベルの公開バインディングは無し" in r.stdout


def test_project_guard_aborts_on_public_run_invoker(gcloud: GcloudStub) -> None:
    """プロジェクト全体の run.invoker allUsers は全サービスを無認証で開ける。"""
    gcloud.set_project_policy("roles/owner\tuser:rep@example.com\nroles/run.invoker\tallUsers\n")
    r = gcloud.run(PROJECT_GUARD)
    assert r.returncode == 1
    assert "自動では消さない" in r.stderr


def test_project_guard_aborts_when_fetch_fails(gcloud: GcloudStub) -> None:
    r = gcloud.run(PROJECT_GUARD, STUB_PROJ_FAIL="1")
    assert r.returncode == 1
    assert "確認できないため中断" in r.stderr


# ── pg_hba 検査(アドレス列限定)─────────────────────────────────────────────────


def pg_hba_check(tmp_path: Path, content: str) -> subprocess.CompletedProcess[str]:
    hba = tmp_path / "pg_hba.conf"
    hba.write_text(content, encoding="utf-8")
    return run_bash(
        f'. "{PG_HBA_LIB}"; pg_hba_unexpected_lines "{hba}" "{DESIRED_HBA}"'
    )


SAFE_HBA = f"""\
# PostgreSQL Client Authentication Configuration File
local   all             postgres                                peer
local   all             all                                     peer
host    all             all             127.0.0.1/32            scram-sha-256
host    all             all             ::1/128                 scram-sha-256
{DESIRED_HBA}
"""


def test_pg_hba_accepts_loopback_and_desired_line(tmp_path: Path) -> None:
    r = pg_hba_check(tmp_path, SAFE_HBA)
    assert r.returncode == 0, r.stdout
    assert r.stdout.strip() == ""


def test_pg_hba_accepts_desired_line_with_different_spacing(tmp_path: Path) -> None:
    """空白の詰め方だけが違う既存行を『想定外』にしない(冪等性)。"""
    squeezed = "host ryza ryza_dashboard,ryza_boardroom 10.138.0.0/20 scram-sha-256"
    r = pg_hba_check(tmp_path, SAFE_HBA.replace(DESIRED_HBA, squeezed))
    assert r.returncode == 0, r.stdout


def test_pg_hba_aborts_on_world_open_line(tmp_path: Path) -> None:
    r = pg_hba_check(tmp_path, SAFE_HBA + "host    all    all    0.0.0.0/0    trust\n")
    assert r.returncode == 1
    assert "0.0.0.0/0" in r.stdout


def test_pg_hba_aborts_on_broad_line_with_localhost_in_comment(tmp_path: Path) -> None:
    """行全体マッチだと『コメントに localhost がある』だけで見逃していた(本 PR の是正点)。"""
    line = "host    all    all    10.0.0.0/8    md5   # localhost からの接続用\n"
    r = pg_hba_check(tmp_path, SAFE_HBA + line)
    assert r.returncode == 1
    assert "10.0.0.0/8" in r.stdout


def test_pg_hba_aborts_on_broad_line_with_localhost_as_database_name(tmp_path: Path) -> None:
    """データベース名/ロール名の 'localhost' でも誤って安全判定しない。"""
    line = "host    localhost    localhost    0.0.0.0/0    md5\n"
    r = pg_hba_check(tmp_path, SAFE_HBA + line)
    assert r.returncode == 1
    assert "0.0.0.0/0" in r.stdout


def test_pg_hba_ignores_commented_out_broad_line(tmp_path: Path) -> None:
    """コメントアウト済みの行は有効でないので中断理由にしない(偽陽性の抑止)。"""
    r = pg_hba_check(tmp_path, SAFE_HBA + "# host    all    all    0.0.0.0/0    trust\n")
    assert r.returncode == 0, r.stdout


def test_pg_hba_accepts_loopback_with_separate_netmask(tmp_path: Path) -> None:
    """CIDR ではなく address + netmask の2列形式もループバックとして扱う。"""
    line = "host    all    all    127.0.0.1    255.255.255.255    scram-sha-256\n"
    r = pg_hba_check(tmp_path, SAFE_HBA + line)
    assert r.returncode == 0, r.stdout


def test_pg_hba_aborts_on_samenet(tmp_path: Path) -> None:
    """samenet はサブネット全体でありループバックではない。"""
    r = pg_hba_check(tmp_path, SAFE_HBA + "host    all    all    samenet    md5\n")
    assert r.returncode == 1
    assert "samenet" in r.stdout


@pytest.mark.parametrize("kind", ["hostssl", "hostnossl", "hostgssenc", "hostnogssenc"])
def test_pg_hba_aborts_on_broad_host_variants(tmp_path: Path, kind: str) -> None:
    r = pg_hba_check(tmp_path, SAFE_HBA + f"{kind}    all    all    0.0.0.0/0    md5\n")
    assert r.returncode == 1
    assert kind in r.stdout


def test_pg_hba_aborts_on_include_directive(tmp_path: Path) -> None:
    """include 先は読めない。『見えないから安全』は検査の沈黙(再審査 条件1 と同じ)。"""
    r = pg_hba_check(tmp_path, SAFE_HBA + "include    /etc/postgresql/extra_hba.conf\n")
    assert r.returncode == 1
    assert "検査不能" in r.stdout


def test_pg_hba_aborts_on_broad_line_sharing_the_subnet_cidr(tmp_path: Path) -> None:
    """同じ CIDR でも database/user が `all all` の行は想定外(先勝ちで本設定を潰す)。"""
    line = "host    all    all    10.138.0.0/20    trust\n"
    r = pg_hba_check(tmp_path, SAFE_HBA + line)
    assert r.returncode == 1
    assert "trust" in r.stdout


# ── 本体スクリプトとの配線(切り出しによるドリフト防止)─────────────────────────


@pytest.mark.parametrize("script", [DEPLOY, GUARDS, PG_HBA_LIB])
def test_shell_syntax_is_valid(script: Path) -> None:
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr


def test_deploy_script_sources_the_guard_libraries() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    assert '. "${ROOT}/ops/lib/deploy-guards.sh"' in text
    assert 'base64 < "${ROOT}/ops/lib/pg_hba_check.sh"' in text
    assert "pg_hba_unexpected_lines" in text


@pytest.mark.parametrize(
    "func",
    ["guard_git_state", "guard_service_public_bindings", "guard_project_public_bindings"],
)
def test_deploy_script_wires_guards_with_abort(func: str) -> None:
    """ゲート呼び出しに `|| exit 1` が付いていること。付け忘れ = 統制の無効化。"""
    text = DEPLOY.read_text(encoding="utf-8")
    calls = [
        ln
        for ln in text.splitlines()
        if re.search(rf"(^|[^#\w]){func}\s", ln) and not ln.lstrip().startswith("#")
    ]
    assert calls, f"{func} の呼び出しが deploy-dashboard.sh に無い"
    for line in calls:
        assert "|| exit 1" in line, f"`|| exit 1` の無いゲート呼び出し: {line!r}"


def test_deploy_script_has_no_row_wide_pg_hba_match() -> None:
    """行全体の localhost マッチ(是正前の実装)が復活していないこと。"""
    text = DEPLOY.read_text(encoding="utf-8")
    assert "samehost|localhost" not in text
