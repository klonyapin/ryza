"""ryza.secrets — 秘密情報ローダ(env 優先 → Secret Manager フォールバック)のテスト。

Issue #30。**HTTP は全てモック**: GCE メタデータ・Secret Manager REST は
``ryza.secrets._urlopen`` を差し替え、実ネットワークを一切呼ばない。
"""

from __future__ import annotations

import base64
import io
import json
import urllib.error

import pytest

from ryza import secrets

_ENV = ("RYZA_TEST_SECRET_A", "RYZA_TEST_SECRET_B")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (*_ENV, "GCP_PROJECT"):
        monkeypatch.delenv(name, raising=False)


def fake_urlopen(values: dict[str, str], calls: list[str] | None = None):
    """メタデータ token → Secret Manager access を模す ``_urlopen`` フェイク。"""

    def _fake(req, timeout):
        url = req.full_url
        if calls is not None:
            calls.append(url)
        if "metadata.google.internal" in url:
            assert req.headers.get("Metadata-flavor") == "Google"
            return io.BytesIO(json.dumps({"access_token": "TOKEN"}).encode())
        assert req.headers.get("Authorization") == "Bearer TOKEN"
        name = url.split("/secrets/")[1].split("/")[0]
        if name not in values:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        data = base64.b64encode(values[name].encode()).decode()
        return io.BytesIO(json.dumps({"payload": {"data": data}}).encode())

    return _fake


def test_env_first_var_wins(monkeypatch):
    monkeypatch.setenv(_ENV[0], "first")
    monkeypatch.setenv(_ENV[1], "second")
    assert secrets.load_secret(env=_ENV) == "first"


def test_env_second_var_falls_back(monkeypatch):
    monkeypatch.setenv(_ENV[1], "second")
    assert secrets.load_secret(env=_ENV) == "second"


def test_env_takes_priority_over_secret(monkeypatch):
    """env があれば Secret Manager へは一切アクセスしない。"""
    calls: list[str] = []
    monkeypatch.setattr(secrets, "_urlopen", fake_urlopen({"k": "sm"}, calls))
    monkeypatch.setenv(_ENV[0], "from-env")
    monkeypatch.setenv("GCP_PROJECT", "proj")
    assert secrets.load_secret(env=_ENV, secret="k") == "from-env"
    assert calls == []


def test_secret_fallback_via_metadata(monkeypatch):
    """env 無し + GCP_PROJECT で Secret Manager REST から取得する。"""
    calls: list[str] = []
    monkeypatch.setattr(
        secrets, "_urlopen", fake_urlopen({"jquants-api-key": "SMKEY"}, calls)
    )
    monkeypatch.setenv("GCP_PROJECT", "proj-1")
    assert secrets.load_secret(env=_ENV, secret="jquants-api-key") == "SMKEY"
    assert any("metadata.google.internal" in u for u in calls)
    assert any(
        "projects/proj-1/secrets/jquants-api-key/versions/latest:access" in u
        for u in calls
    )


def test_secret_fallback_project_arg_overrides_env(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(secrets, "_urlopen", fake_urlopen({"k": "v"}, calls))
    monkeypatch.setenv("GCP_PROJECT", "env-proj")
    assert secrets.load_secret(env=_ENV, secret="k", project="arg-proj") == "v"
    assert any("projects/arg-proj/" in u for u in calls)


def test_no_env_no_project_returns_none_without_network(monkeypatch):
    """GCP_PROJECT 不明なら metadata へ接続せず None(非 GCE 環境の想定)。"""
    calls: list[str] = []
    monkeypatch.setattr(secrets, "_urlopen", fake_urlopen({}, calls))
    assert secrets.load_secret(env=_ENV, secret="k") is None
    assert calls == []


def test_no_secret_name_returns_none(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT", "proj")
    assert secrets.load_secret(env=_ENV) is None


def test_secret_access_failure_returns_none(monkeypatch, caplog):
    """未登録 Secret(404)等の失敗は例外でなく None(daily は skipped 扱い)。"""
    monkeypatch.setattr(secrets, "_urlopen", fake_urlopen({}))
    monkeypatch.setenv("GCP_PROJECT", "proj")
    with caplog.at_level("WARNING", logger="ryza.secrets"):
        assert secrets.load_secret(env=_ENV, secret="missing") is None
    assert any("missing" in r.message for r in caplog.records)


def test_metadata_unreachable_returns_none(monkeypatch):
    """metadata サーバ不達(非 GCE で GCP_PROJECT だけある)も None。"""

    def _down(req, timeout):
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(secrets, "_urlopen", _down)
    monkeypatch.setenv("GCP_PROJECT", "proj")
    assert secrets.load_secret(env=_ENV, secret="k") is None


# ── probe_secret: 取得できない理由の可視化(Issue #38)─────────────────────────
def test_probe_secret_env_hit_has_no_reason(monkeypatch):
    monkeypatch.setenv(_ENV[0], "v")
    res = secrets.probe_secret(env=_ENV)
    assert res == secrets.SecretLookup("v")


def test_probe_secret_reason_no_project(monkeypatch):
    """GCP_PROJECT 未設定(env 伝播漏れ)が理由として判別できる。"""
    res = secrets.probe_secret(env=_ENV, secret="k")
    assert res.value is None
    assert "GCP_PROJECT 未設定" in res.reason
    assert "'k'" in res.reason


def test_probe_secret_reason_404_hints_missing_version(monkeypatch):
    """404 は「Secret 未登録またはバージョン未追加」ヒント付き(2026-08-03 の実例)。"""
    monkeypatch.setattr(secrets, "_urlopen", fake_urlopen({}))
    monkeypatch.setenv("GCP_PROJECT", "proj")
    res = secrets.probe_secret(env=_ENV, secret="estat-app-id")
    assert res.value is None
    assert "404" in res.reason
    assert "バージョン未追加" in res.reason


def test_probe_secret_reason_no_secret_name(monkeypatch):
    res = secrets.probe_secret(env=_ENV)
    assert res.value is None
    assert "未設定" in res.reason


# ── is_running_on_gce: T-024 の共通 GCE 判定 ─────────────────────────────────
def test_is_running_on_gce_true_when_metadata_reachable(monkeypatch):
    """メタデータサーバに到達できれば True。"""
    monkeypatch.setattr(secrets, "_urlopen", fake_urlopen({}))
    secrets.reset_gce_cache()
    try:
        assert secrets.is_running_on_gce() is True
    finally:
        secrets.reset_gce_cache()


def test_is_running_on_gce_false_when_metadata_unreachable(monkeypatch):
    """メタデータサーバ不達(非 GCE)なら False(例外は握って False)。"""

    def _down(req, timeout):
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(secrets, "_urlopen", _down)
    secrets.reset_gce_cache()
    try:
        assert secrets.is_running_on_gce() is False
    finally:
        secrets.reset_gce_cache()


def test_is_running_on_gce_cached_across_calls(monkeypatch):
    """判定結果はプロセス内でキャッシュされ、``_urlopen`` は 1 度しか呼ばれない。"""
    calls: list[str] = []
    monkeypatch.setattr(secrets, "_urlopen", fake_urlopen({}, calls))
    secrets.reset_gce_cache()
    try:
        secrets.is_running_on_gce()
        secrets.is_running_on_gce()
        secrets.is_running_on_gce()
        assert len(calls) == 1
    finally:
        secrets.reset_gce_cache()
