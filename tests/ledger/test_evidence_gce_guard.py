"""本番(GCE)証憑ストア必須ガードの検証(T-024 / A-12 F-7)。

``_evidence_store`` は GCE 上で ``RYZA_EVIDENCE_DIR`` 未設定なら fail-closed で例外
を送出する。開発・CI(非 GCE)では従来どおりインラインへ落ちる。
"""

from __future__ import annotations

import io
import json

import pytest

from ryza import secrets
from ryza.ledger import _util


def _force_gce(value: bool) -> None:
    """``ryza.secrets._gce_cache`` を上書きして GCE 判定を固定する。"""
    secrets._gce_cache = value


@pytest.fixture(autouse=True)
def _restore_gce_cache():
    """各テスト後に GCE 判定キャッシュをテスト既定(False)へ戻す。

    ``_force_gce(True)`` が他テストへ漏れると本番ガードが誤発火するため、
    tests/conftest.py の pytest_configure と同じ値に毎回リセットする。
    """
    yield
    secrets._gce_cache = False


def test_gce_and_env_unset_raises_before_writing_any_evidence(conn, monkeypatch):
    """GCE 検出 + ``RYZA_EVIDENCE_DIR`` 未設定 → 記帳前に RuntimeError。

    メッセージには是正方法(``RYZA_EVIDENCE_DIR``)と理由(不変保存)を含む。
    証憑は 1 件も書かれない(トランザクション内で例外送出、行数は不変)。
    """
    _force_gce(True)
    monkeypatch.delenv("RYZA_EVIDENCE_DIR", raising=False)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ledger.evidence")
        before = cur.fetchone()[0]

    with pytest.raises(RuntimeError) as excinfo:
        _util.create_evidence(
            conn, kind="decision", payload={"k": "v"}, source="test"
        )
    msg = str(excinfo.value)
    assert "RYZA_EVIDENCE_DIR" in msg
    assert "不変" in msg  # 「不変保存を満たさない」旨

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ledger.evidence")
        after = cur.fetchone()[0]
    assert after == before  # 1 件も書かずに死んでいる


def test_gce_with_env_set_writes_to_file_store(conn, tmp_path, monkeypatch):
    """GCE 検出 + ``RYZA_EVIDENCE_DIR`` 設定 → ストア(file://)経由で正常に格納。"""
    _force_gce(True)
    evdir = tmp_path / "ev"
    monkeypatch.setenv("RYZA_EVIDENCE_DIR", str(evdir))

    eid = _util.create_evidence(
        conn, kind="decision", payload={"k": "v"}, source="test"
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload_ref FROM ledger.evidence WHERE evidence_id = %s", (eid,)
        )
        payload_ref = cur.fetchone()[0]
    assert payload_ref.startswith("file://")


def test_non_gce_and_env_unset_falls_back_to_inline(conn, monkeypatch):
    """非 GCE(開発・CI)+ 未設定 → 従来どおりインライン JSON。"""
    _force_gce(False)
    monkeypatch.delenv("RYZA_EVIDENCE_DIR", raising=False)

    eid = _util.create_evidence(
        conn, kind="decision", payload={"k": "v"}, source="test"
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload_ref FROM ledger.evidence WHERE evidence_id = %s", (eid,)
        )
        payload_ref = cur.fetchone()[0]
    assert not payload_ref.startswith(("file://", "gs://"))
    assert json.loads(payload_ref) == {"k": "v"}


def test_gce_detection_cached_across_calls(conn, monkeypatch):
    """複数の証憑作成試行でメタデータサーバ問い合わせは 1 回しか走らない。

    GCE ガードは env 未設定時に ``is_running_on_gce()`` を呼ぶ。キャッシュ空の状態で
    ``_util.create_evidence`` を 3 回試み、各回 ``RuntimeError`` になる(=書き込みは
    生じない)ことを確認しつつ、``ryza.secrets._urlopen`` の呼び出しが 1 回に留まる
    ことを見る(証憑作成のたびにメタデータサーバへ問い合わせない — T-024 指示書)。
    """
    saved_cache = secrets._gce_cache
    secrets.reset_gce_cache()
    monkeypatch.delenv("RYZA_EVIDENCE_DIR", raising=False)

    calls: list[str] = []

    def _fake(req, timeout):
        calls.append(req.full_url)
        # 到達可能をシミュレート(トークン JSON を返す。読み捨てても構わない)。
        return io.BytesIO(json.dumps({"access_token": "TOKEN"}).encode())

    monkeypatch.setattr(secrets, "_urlopen", _fake)

    try:
        for i in range(3):
            with pytest.raises(RuntimeError):
                _util.create_evidence(
                    conn, kind="decision", payload={"i": i}, source="test"
                )
        assert len(calls) == 1
        assert "metadata.google.internal" in calls[0]
    finally:
        secrets._gce_cache = saved_cache
