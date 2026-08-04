"""証憑ストアの受け入れ基準テスト。

- store→verify が通る。実体を書き換えると verify が False
- 同一内容の store が同じ evidence_id を返す(重複排除)
- LocalStorage / GcsStorage(GCS はモック)が同一インターフェースで動く
"""

from __future__ import annotations

import hashlib
import json

import pytest

from ryza.provenance.evidence import EvidenceStore, GcsStorage, LocalStorage, validate_source


# ── GCS のインメモリ・フェイク(google-cloud-storage の最小 API) ─────────────
class _FakeBlob:
    def __init__(self, store: dict[str, bytes], name: str) -> None:
        self._store = store
        self._name = name

    def upload_from_string(self, data: bytes) -> None:
        self._store[self._name] = data

    def download_as_bytes(self) -> bytes:
        return self._store[self._name]

    def exists(self) -> bool:
        return self._name in self._store


class FakeBucket:
    """``google.cloud.storage.Bucket`` 互換の最小フェイク。"""

    def __init__(self, name: str = "ryza-evidence-test") -> None:
        self.name = name
        self._store: dict[str, bytes] = {}

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(self._store, name)


@pytest.fixture
def local_store(tmp_path):
    return EvidenceStore(LocalStorage(tmp_path / "evidence_root")), None


@pytest.fixture
def gcs_bucket():
    return FakeBucket()


@pytest.fixture
def gcs_store(gcs_bucket):
    return EvidenceStore(GcsStorage(gcs_bucket)), gcs_bucket


# ── store → verify → get(bytes / dict の両方) ────────────────────────────────
def test_store_verify_get_bytes(conn, local_store):
    store, _ = local_store
    payload = b"broker fill raw response \x00\x01\x02"
    eid = store.store(conn, "broker_fill", payload, source="ibkr_paper")
    assert isinstance(eid, int)
    assert store.verify(conn, eid) is True
    assert store.get(conn, eid) == payload


def test_store_dict_is_deterministic_json(conn, local_store):
    store, _ = local_store
    payload = {"b": 2, "a": 1, "nested": {"y": [3, 2, 1], "x": "値"}}
    eid = store.store(conn, "decision", payload, source="strategy.momentum")
    got = store.get(conn, eid)
    # キーソートされた決定論的 JSON として復元できる。
    assert json.loads(got.decode("utf-8")) == payload
    assert store.verify(conn, eid) is True


# ── DB 記録の sha256 が実体と一致 ──────────────────────────────────────────
def test_db_sha256_matches_payload(conn, local_store):
    store, _ = local_store
    payload = b"gcp billing export line"
    eid = store.store(conn, "gcp_billing", payload, source="gcp")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sha256, payload_ref, kind FROM ledger.evidence WHERE evidence_id = %s",
            (eid,),
        )
        sha, payload_ref, kind = cur.fetchone()
    assert bytes(sha) == hashlib.sha256(payload).digest()
    assert payload_ref.startswith("file://")
    # パスは evidence/{yyyy}/{mm}/{kind}/{sha256}.{ext} 規約に従う。
    assert f"/{kind}/" in payload_ref
    assert payload_ref.endswith(".bin")


# ── 改竄検知: 実体を書き換えると verify が False ─────────────────────────────
def test_verify_false_when_storage_tampered(conn, local_store):
    store, _ = local_store
    payload = b"original evidence"
    eid = store.store(conn, "price_snapshot", payload, source="jquants")
    assert store.verify(conn, eid) is True

    # ストレージ上の実体を直接書き換える(改竄をシミュレート)。
    with conn.cursor() as cur:
        cur.execute("SELECT payload_ref FROM ledger.evidence WHERE evidence_id = %s", (eid,))
        payload_ref = cur.fetchone()[0]
    key = store.storage.key_from_uri(payload_ref)
    (store.storage.root / key).write_bytes(b"tampered!")

    assert store.verify(conn, eid) is False


def test_verify_false_when_storage_missing(conn, local_store):
    store, _ = local_store
    eid = store.store(conn, "invoice", b"pdf bytes", source="vendor")
    with conn.cursor() as cur:
        cur.execute("SELECT payload_ref FROM ledger.evidence WHERE evidence_id = %s", (eid,))
        key = store.storage.key_from_uri(cur.fetchone()[0])
    (store.storage.root / key).unlink()
    assert store.verify(conn, eid) is False


# ── 重複排除: 同一内容の store が同じ evidence_id を返す ─────────────────────
def test_dedup_same_content_same_id(conn, local_store):
    store, _ = local_store
    payload = {"symbol": "7203.T", "price": 2500}
    eid1 = store.store(conn, "price_snapshot", payload, source="jquants")
    eid2 = store.store(conn, "price_snapshot", payload, source="jquants")
    assert eid1 == eid2
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ledger.evidence WHERE evidence_id = %s", (eid1,))
        assert cur.fetchone()[0] == 1


def test_different_content_different_id(conn, local_store):
    store, _ = local_store
    eid1 = store.store(conn, "price_snapshot", {"p": 1}, source="s")
    eid2 = store.store(conn, "price_snapshot", {"p": 2}, source="s")
    assert eid1 != eid2


# ── LocalStorage / GcsStorage が同一インターフェースで動く ───────────────────
def test_gcs_store_verify_get(conn, gcs_store):
    store, bucket = gcs_store
    payload = b"broker statement via gcs"
    eid = store.store(conn, "broker_statement", payload, source="saxo")
    assert store.verify(conn, eid) is True
    assert store.get(conn, eid) == payload
    # payload_ref が gs:// URI で、フェイクバケットに実体が入っている。
    with conn.cursor() as cur:
        cur.execute("SELECT payload_ref FROM ledger.evidence WHERE evidence_id = %s", (eid,))
        payload_ref = cur.fetchone()[0]
    assert payload_ref.startswith("gs://ryza-evidence-test/")
    assert len(bucket._store) == 1


def test_gcs_dedup(conn, gcs_store):
    store, bucket = gcs_store
    eid1 = store.store(conn, "llm_usage", {"tier": "mid", "tokens": 100}, source="anthropic")
    eid2 = store.store(conn, "llm_usage", {"tier": "mid", "tokens": 100}, source="anthropic")
    assert eid1 == eid2
    assert len(bucket._store) == 1


def test_gcs_tamper_detected(conn, gcs_store):
    store, bucket = gcs_store
    eid = store.store(conn, "broker_fill", b"gcs fill", source="binance_testnet")
    # フェイクバケット内の実体を差し替え。
    key = next(iter(bucket._store))
    bucket._store[key] = b"tampered"
    assert store.verify(conn, eid) is False


# ── F-10: source 様式の検証(表示系への注入面の遮断)────────────────────────
# 実運用値 11 種(TDnet / 日銀 / BOE / 米経済分析局BEA / FRB / ECB / FRED /
# intl_banks / investment_committee / demo / J-Quants)+ URL・パス形式が通ること。
@pytest.mark.parametrize(
    "value",
    [
        # 既存実運用の 11 値(遡及書き換えを避けるための下限保証)
        "TDnet",
        "日銀",
        "BOE",
        "米経済分析局BEA",
        "FRB",
        "ECB",
        "FRED",
        "intl_banks",
        "investment_committee",
        "demo",
        "J-Quants",
        # 追加の許容形(URL 相当・パス相当)
        "anthropic",
        "binance_testnet",
        "https://api.example.com/v1/quotes",
        "path/to/artifact.json",
        "run:1234-abcd",
        "TDnet/2026-08-04",
        "a" + "b" * 127,  # 上限 128 文字
    ],
)
def test_validate_source_accepts_real_values(value):
    assert validate_source(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",  # 空文字
        "has space",  # 空白
        "line1\nline2",  # 改行
        "with\ttab",  # タブ
        "*bold*",  # markdown メタ
        "[link](x)",
        "under_score|pipe",
        "<script>",
        "a" + "b" * 128,  # 129 文字(上限超え)
        " leading_space",
        "trailing_space ",
        "\x00null",  # 制御文字
    ],
)
def test_validate_source_rejects_bad(value):
    with pytest.raises(ValueError):
        validate_source(value)


def test_validate_source_rejects_non_str():
    with pytest.raises(ValueError):
        validate_source(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        validate_source(123)  # type: ignore[arg-type]


def test_store_rejects_bad_source(conn, local_store):
    """EvidenceStore.store 経由でも同じ検証が効くこと(A-12-04)。"""
    store, _ = local_store
    with pytest.raises(ValueError, match="source"):
        store.store(conn, "llm_usage", {"x": 1}, source="has space")
    conn.rollback()
