"""証憑ストア: 不変保存 + sha256 改竄検知 + 重複排除。

設計書 `docs/design/10-data-accounting.md` §5(``ledger.evidence``)・§7(証憑ストア)準拠。

証憑の実体はストレージ(``EvidenceStorage``)に
``evidence/{yyyy}/{mm}/{kind}/{sha256}.{ext}`` のキーで不変保存し、メタ行を
``ledger.evidence`` に作成する。``ledger.evidence.sha256`` とストレージ上の実体の
sha256 を突合することで改竄を検知する(監査 A-1 の部品)。

ストレージは開発用 ``LocalStorage`` と本番用 ``GcsStorage`` を同一インターフェース
``EvidenceStorage`` で抽象化する。GCS バケットはバージョニング + 削除保護前提。

## posting.py が依存する安定 API(このシグネチャは維持する)

会計エンジン(``ryza.ledger.posting``)は ``EvidenceStore`` を1つ生成し、記帳
トランザクションと同じ psycopg 接続 ``conn`` を渡して以下を呼ぶ。本モジュールは
``conn`` を **commit しない**(呼び出し側のトランザクションに参加する)。これにより
「T-003 完了までの JSONB フォールバック」は本ストア経由に置き換えられる。

    store = EvidenceStore(LocalStorage(root))            # または GcsStorage(bucket)

    store.store(conn, kind, payload, source) -> int
        # kind: str(broker_fill|price_snapshot|llm_usage|invoice|decision ...)
        # payload: bytes | dict(dict は決定論的 JSON にシリアライズ)
        # source: str(取得元)
        # 戻り値: evidence_id。同一 sha256 は再保存せず既存 evidence_id を返す(重複排除)

    store.verify(conn, evidence_id) -> bool
        # ストレージ上の実体の sha256 と DB 記録を突合。改竄・欠損時は False

    store.get(conn, evidence_id) -> bytes
        # 実体のバイト列を返す(dict で保存したものは決定論的 JSON バイト列)
"""

from __future__ import annotations

import hashlib
import json
import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import psycopg

# ── source の様式検証(F-10 / A-12-04)─────────────────────────────────────────
# ``ledger.evidence.source`` は表示系(embed・週次レポート)に素で載る列であり、空白・改行・
# 制御文字・markdown メタ文字が混ざると、後段の描画が壊れる/なりすまし行を刺せる。
# ``re.fullmatch(r"[\\w][\\w.:/\\-]{0,127}", source)`` に制限する。Python の str に対する
# ``\\w`` は Unicode 単語文字なので、既存の日本語 source(TDnet / 日銀 / 米経済分析局BEA など)
# は素通りする。**ASCII 限定にしない**理由: 既存の実運用値 11 種のうち複数が日本語を含み、
# 遡及書き換えは追記オンリー原則の逸脱にあたるため。狙いは注入面の遮断であって語彙統一ではない。
_SOURCE_VALID_RE = re.compile(r"[\w][\w.:/\-]{0,127}")


def validate_source(value: str) -> str:
    """``ledger.evidence.source`` の様式を検証する純粋関数(F-10)。

    許可: 先頭 1 文字が Unicode 単語文字(``\\w`` — 日本語を含む)で、続く 0〜127 文字が
    ``\\w`` または ``. : / -`` のいずれか。空白・改行・制御文字・markdown メタ文字
    (``[]()*_~|<>`` 等 — ``\\w`` と ``.:/‐`` 以外の記号)は不可。全長 128 文字まで。

    Raises:
        ValueError: 型不一致・空文字・様式不一致

    設計判断: **注入面の遮断が目的**。既存の 11 種の実運用値(TDnet / 日銀 / BOE /
    米経済分析局BEA / FRB / ECB / FRED / intl_banks / investment_committee / demo /
    J-Quants)はすべて通る。ASCII 限定にしないのは既存の日本語 source の遡及書き換えを
    避けるため(証憑の source は付替不能)。
    """
    if not isinstance(value, str):
        raise ValueError(f"source は文字列である必要がある: {type(value).__name__}")
    if not value:
        raise ValueError("source は非空文字列である必要がある")
    if not _SOURCE_VALID_RE.fullmatch(value):
        raise ValueError(
            f"source={value!r} は許可された様式に一致しない。"
            "先頭は Unicode 単語文字、続きは単語文字または `. : / -` のみ、全長 128 まで"
            "(空白・改行・制御文字・markdown メタ文字は表示系への注入面のため不可)"
        )
    return value


# ────────────────────────────────────────────────────────────────────────────
# ストレージ抽象
# ────────────────────────────────────────────────────────────────────────────
class EvidenceStorage(ABC):
    """証憑実体の不変オブジェクトストア。キーは相対パス(``evidence/...``)。

    実装は上書きを避け(不変保存)、``uri()`` で ``ledger.evidence.payload_ref`` に
    格納する URI を返す。``key_from_uri()`` はその逆変換で、``verify`` / ``get`` が
    DB の payload_ref から実体を引くために使う。
    """

    @abstractmethod
    def put(self, key: str, data: bytes) -> None:
        """``key`` に ``data`` を保存する(既存キーは不変前提で上書きしない)。"""

    @abstractmethod
    def get(self, key: str) -> bytes:
        """``key`` の実体を返す。存在しなければ例外。"""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """``key`` が存在するか。"""

    @abstractmethod
    def uri(self, key: str) -> str:
        """``key`` の永続 URI(payload_ref に格納する値)。"""

    @abstractmethod
    def key_from_uri(self, uri: str) -> str:
        """``uri()`` の逆変換。自ストレージが発行した URI からキーを復元する。"""


class LocalStorage(EvidenceStorage):
    """開発用: ローカルディレクトリをストアとして使う。

    URI は ``file://{root}/{key}`` 形式。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        return self.root / key

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 不変保存: 既に同一キー(= 同一 sha256)があれば書き直さない。
        if not path.exists():
            path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def uri(self, key: str) -> str:
        return f"file://{self.root}/{key}"

    def key_from_uri(self, uri: str) -> str:
        prefix = f"file://{self.root}/"
        if not uri.startswith(prefix):
            raise ValueError(f"このストレージの URI ではない: {uri}")
        return uri[len(prefix):]


class _GcsBucket(Protocol):
    """``GcsStorage`` が依存する google-cloud-storage の Bucket 相当の最小 API。"""

    name: str

    def blob(self, name: str) -> Any: ...


class GcsStorage(EvidenceStorage):
    """本番用: GCS バケットをストアとして使う。

    ``bucket`` は ``google.cloud.storage.Bucket`` 互換オブジェクト
    (``bucket.blob(name).upload_from_string / download_as_bytes / exists``)。
    実インフラ(バケット作成・バージョニング・削除保護)はデプロイタスクの範囲で、
    本クラスは注入されたバケットに対して同一インターフェースで動く。テストでは
    インメモリのフェイクバケットを注入する。

    URI は ``gs://{bucket}/{key}`` 形式。
    """

    def __init__(self, bucket: _GcsBucket, bucket_name: str | None = None) -> None:
        self.bucket = bucket
        self.bucket_name = bucket_name or getattr(bucket, "name", None)
        if not self.bucket_name:
            raise ValueError("bucket_name を解決できない(bucket.name も未設定)")

    def put(self, key: str, data: bytes) -> None:
        blob = self.bucket.blob(key)
        # 不変保存: 既存キーは書き直さない(バケット側の削除保護と併せ二重の担保)。
        if not blob.exists():
            blob.upload_from_string(data)

    def get(self, key: str) -> bytes:
        return self.bucket.blob(key).download_as_bytes()

    def exists(self, key: str) -> bool:
        return self.bucket.blob(key).exists()

    def uri(self, key: str) -> str:
        return f"gs://{self.bucket_name}/{key}"

    def key_from_uri(self, uri: str) -> str:
        prefix = f"gs://{self.bucket_name}/"
        if not uri.startswith(prefix):
            raise ValueError(f"このストレージの URI ではない: {uri}")
        return uri[len(prefix):]


# ────────────────────────────────────────────────────────────────────────────
# ペイロードのシリアライズ
# ────────────────────────────────────────────────────────────────────────────
def _serialize(payload: bytes | dict[str, Any] | list[Any]) -> tuple[bytes, str]:
    """(バイト列, 拡張子) を返す。

    dict / list は決定論的 JSON(キーソート・ASCII 非強制)に変換する。同一内容が
    常に同一バイト列 → 同一 sha256 になり、重複排除が効く。
    """
    if isinstance(payload, bytes):
        return payload, "bin"
    if isinstance(payload, (dict, list)):
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return text.encode("utf-8"), "json"
    raise TypeError(f"payload は bytes | dict | list のみ: {type(payload).__name__}")


# ────────────────────────────────────────────────────────────────────────────
# 証憑ストア本体
# ────────────────────────────────────────────────────────────────────────────
class EvidenceStore:
    """証憑ストア。1つのストレージバックエンドを保持し、DB 接続は呼び出しごとに受け取る。

    DB 書き込みは渡された ``conn`` のトランザクションに参加し、本クラスは commit しない
    (呼び出し側が制御する)。posting.py はこれを記帳トランザクションと共有する。
    """

    def __init__(self, storage: EvidenceStorage) -> None:
        self.storage = storage

    def store(
        self,
        conn: psycopg.Connection,
        kind: str,
        payload: bytes | dict[str, Any] | list[Any],
        source: str,
    ) -> int:
        """証憑を保存し ``evidence_id`` を返す。同一 sha256 は再保存せず既存 ID を返す。"""
        # F-10: 表示系(embed・週次レポート)への注入面を writer で塞ぐ。既存 11 種は通る。
        validate_source(source)
        data, ext = _serialize(payload)
        digest = hashlib.sha256(data).digest()

        # 重複排除: 同一 sha256 の証憑が既にあれば、それを返す(再保存しない)。
        with conn.cursor() as cur:
            cur.execute(
                "SELECT evidence_id FROM ledger.evidence WHERE sha256 = %s LIMIT 1",
                (digest,),
            )
            row = cur.fetchone()
            if row is not None:
                return row[0]

        now = datetime.now(UTC)
        key = f"evidence/{now:%Y}/{now:%m}/{kind}/{digest.hex()}.{ext}"
        self.storage.put(key, data)
        payload_ref = self.storage.uri(key)

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ledger.evidence (kind, payload_ref, sha256, source, retrieved_at)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING evidence_id
                """,
                (kind, payload_ref, digest, source, now),
            )
            return cur.fetchone()[0]

    def verify(self, conn: psycopg.Connection, evidence_id: int) -> bool:
        """ストレージ上の実体の sha256 と DB 記録を突合する(監査 A-1)。

        改竄・欠損・URI 不整合はいずれも False。
        """
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload_ref, sha256 FROM ledger.evidence WHERE evidence_id = %s",
                (evidence_id,),
            )
            row = cur.fetchone()
        if row is None:
            return False
        payload_ref, stored_sha = row
        try:
            key = self.storage.key_from_uri(payload_ref)
            data = self.storage.get(key)
        except (ValueError, FileNotFoundError, KeyError, OSError):
            return False
        return hashlib.sha256(data).digest() == bytes(stored_sha)

    def get(self, conn: psycopg.Connection, evidence_id: int) -> bytes:
        """証憑実体のバイト列を返す。

        dict / list で保存したものは決定論的 JSON バイト列として返る
        (呼び出し側で ``json.loads`` する)。
        """
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload_ref FROM ledger.evidence WHERE evidence_id = %s",
                (evidence_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise KeyError(f"evidence_id {evidence_id} は存在しない")
        key = self.storage.key_from_uri(row[0])
        return self.storage.get(key)
