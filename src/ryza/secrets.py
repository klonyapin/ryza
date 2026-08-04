"""secrets — 秘密情報ローダ(env 優先 → Secret Manager フォールバック、stdlib のみ)。

Issue #30。research(``providers.load_api_key``)・ingest(J-Quants / FRED / e-Stat)が
共有する鍵取得の共通口。取得順序は全モジュール共通:

1. **環境変数優先**: 渡された env 名を順に見て、最初の非空値を返す(ローカル開発・
   テスト・明示注入をそのまま尊重する)。
2. **Secret Manager フォールバック**: env に無く ``GCP_PROJECT``(または引数
   ``project``)が判明する場合のみ、GCE メタデータサーバでアクセストークンを取り
   Secret Manager REST で ``versions/latest`` を読む(T-006 bot の stdlib 流儀。
   SDK を import しないのは proto-plus/protobuf の版差回避・追加依存なしのため)。
3. **どちらも無ければ None**: 呼び出し側が各自のエラー型(``JQuantsAuthError`` 等)で
   「資格情報未設定」を表現する。Secret 取得の失敗(非 GCE 環境・権限不足・未登録)も
   warning ログの上 None に落とす — daily はこれを failed でなく skipped として扱う。

**診断性(Issue #38)**: 「未設定」の理由(env 未設定/GCP_PROJECT 不明/Secret 取得失敗)
が daily の skip 理由から見えないと運用ミス(Secret 本体だけ作成して値=バージョン
未登録、env 伝播漏れ等)の切り分けに VM ログが要る。``probe_secret`` は値に加えて
「取得できなかった理由」を返し、呼び出し側はこれをエラーメッセージ(→ ops サマリ)に
載せる。

HTTP は ``_urlopen`` 経由(テストはここをモックし、実ネットワークを一切呼ばない)。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import IO, Any

logger = logging.getLogger(__name__)

_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)


def _urlopen(req: urllib.request.Request, timeout: float) -> IO[bytes]:
    """``urllib.request.urlopen`` の差し替え口(テストはここをモックする)。"""
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310


# GCE 判定のプロセス内キャッシュ(``is_running_on_gce`` が使う)。
# None は未判定、bool は前回判定結果。テストは ``reset_gce_cache`` で明示的にクリアする
# か、``_gce_cache`` を直接 False/True にセットして分岐を固定できる。
_gce_cache: bool | None = None


def reset_gce_cache() -> None:
    """``is_running_on_gce`` のプロセス内キャッシュをクリアする(テスト用)。"""
    global _gce_cache
    _gce_cache = None


def is_running_on_gce(timeout: float = 1.0) -> bool:
    """このプロセスが GCE(Metadata サーバに到達可能)上で動いているか。

    T-024 で会計エンジンの ``RYZA_EVIDENCE_DIR`` 必須ガードが本番検出に使う共通判定。
    ``access_secret`` の Secret Manager フェッチと同じメタデータサーバ URL・同じ
    ``Metadata-Flavor: Google`` ヘッダを用いる(``access_secret`` の挙動は不変で、この
    関数は独立に呼び出されて成否だけを返す)。プロセス内で結果をキャッシュするため、
    ``create_evidence`` の呼び出しごとに毎回メタデータサーバへ問い合わせない。
    キャッシュのリセットは ``reset_gce_cache()``。テストは ``_urlopen`` をモックしても
    よいし、``_gce_cache`` を直接 True/False に固定してもよい(ledger のテストは後者を
    採り、ネットワーク往復を発生させない)。
    """
    global _gce_cache
    if _gce_cache is not None:
        return _gce_cache
    req = urllib.request.Request(
        _METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}
    )
    try:
        _urlopen(req, timeout).close()
    except (OSError, TimeoutError, urllib.error.URLError):
        # 非 GCE 環境では名前解決失敗・接続拒否・タイムアウトのいずれかになる。
        _gce_cache = False
        return False
    _gce_cache = True
    return True


def access_secret(secret: str, *, project: str, timeout: float = 10.0) -> str:
    """Secret Manager から値を取得する(GCE メタデータ + REST)。失敗は例外送出。"""
    meta = urllib.request.Request(
        _METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}
    )
    token_payload: dict[str, Any] = json.load(_urlopen(meta, timeout))
    access_token = token_payload["access_token"]
    req = urllib.request.Request(
        f"https://secretmanager.googleapis.com/v1/projects/{project}"
        f"/secrets/{secret}/versions/latest:access",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    payload: dict[str, Any] = json.load(_urlopen(req, timeout))
    return base64.b64decode(payload["payload"]["data"]).decode("utf-8")


@dataclass(frozen=True)
class SecretLookup:
    """秘密情報の取得結果。``value`` が None のとき ``reason`` に理由(診断用)。"""

    value: str | None
    reason: str | None = None


def probe_secret(
    *,
    env: Sequence[str],
    secret: str | None = None,
    project: str | None = None,
    timeout: float = 10.0,
) -> SecretLookup:
    """env 優先で秘密情報を引き、無ければ Secret Manager。取得できない理由も返す。

    - ``env``: 優先順に見る環境変数名。最初の非空値を返す。
    - ``secret``: Secret Manager のシークレット名。None なら env のみ。
    - ``project``: GCP プロジェクト ID。省略時は env ``GCP_PROJECT``。どちらも無ければ
      Secret Manager は試さない(非 GCE 環境で metadata へ無駄に接続しない)。
    - Secret 取得の失敗(接続不可・権限不足・未登録・バージョン未追加)は warning ログの
      上、失敗内容を ``reason`` に載せて返す(Issue #38: skip 理由の可視化)。
    """
    for name in env:
        value = os.environ.get(name)
        if value:
            return SecretLookup(value)
    env_desc = "/".join(env)
    if not secret:
        return SecretLookup(None, f"env {env_desc} 未設定")
    project = project or os.environ.get("GCP_PROJECT", "")
    if not project:
        return SecretLookup(
            None,
            f"env {env_desc} 未設定・GCP_PROJECT 未設定のため "
            f"Secret {secret!r} を未試行",
        )
    try:
        return SecretLookup(access_secret(secret, project=project, timeout=timeout))
    except (OSError, TimeoutError, KeyError, ValueError) as exc:
        # OSError は urllib.error.URLError/HTTPError を含む。JSONDecodeError は ValueError。
        logger.warning("Secret Manager から %r を取得できません: %s", secret, exc)
        hint = ""
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
            # Secret 本体が未作成、または本体だけ作成して値(バージョン)未追加でも 404
            # (2026-08-03 の estat-app-id 実例)。
            hint = "(Secret 未登録またはバージョン未追加)"
        return SecretLookup(
            None,
            f"env {env_desc} 未設定・Secret {secret!r}"
            f"(project {project}) 取得失敗{hint}: {exc}",
        )


def load_secret(
    *,
    env: Sequence[str],
    secret: str | None = None,
    project: str | None = None,
    timeout: float = 10.0,
) -> str | None:
    """``probe_secret`` の値のみ版(既存呼び出し互換。理由が要るなら probe_secret)。"""
    return probe_secret(
        env=env, secret=secret, project=project, timeout=timeout
    ).value


__all__ = [
    "SecretLookup",
    "access_secret",
    "is_running_on_gce",
    "load_secret",
    "probe_secret",
    "reset_gce_cache",
]
