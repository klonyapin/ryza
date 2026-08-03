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
from typing import IO, Any

logger = logging.getLogger(__name__)

_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)


def _urlopen(req: urllib.request.Request, timeout: float) -> IO[bytes]:
    """``urllib.request.urlopen`` の差し替え口(テストはここをモックする)。"""
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310


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


def load_secret(
    *,
    env: Sequence[str],
    secret: str | None = None,
    project: str | None = None,
    timeout: float = 10.0,
) -> str | None:
    """env 優先で秘密情報を引き、無ければ Secret Manager、それも無ければ None。

    - ``env``: 優先順に見る環境変数名。最初の非空値を返す。
    - ``secret``: Secret Manager のシークレット名。None なら env のみ。
    - ``project``: GCP プロジェクト ID。省略時は env ``GCP_PROJECT``。どちらも無ければ
      Secret Manager は試さない(非 GCE 環境で metadata へ無駄に接続しない)。
    - Secret 取得の失敗(接続不可・権限不足・未登録)は warning ログの上 None を返す。
    """
    for name in env:
        value = os.environ.get(name)
        if value:
            return value
    if not secret:
        return None
    project = project or os.environ.get("GCP_PROJECT", "")
    if not project:
        return None
    try:
        return access_secret(secret, project=project, timeout=timeout)
    except (OSError, TimeoutError, KeyError, ValueError) as exc:
        # OSError は urllib.error.URLError/HTTPError を含む。JSONDecodeError は ValueError。
        logger.warning("Secret Manager から %r を取得できません: %s", secret, exc)
        return None


__all__ = ["access_secret", "load_secret"]
