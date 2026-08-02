"""DB 接続ヘルパー。接続文字列は環境変数 RYZA_DATABASE_URL から取得する。"""

from __future__ import annotations

import os

import psycopg

DEFAULT_URL = "postgresql://ryza:ryza@localhost:5432/ryza"


def database_url() -> str:
    """RYZA_DATABASE_URL があればそれを、なければローカル開発用の既定値を返す。"""
    return os.environ.get("RYZA_DATABASE_URL", DEFAULT_URL)


def connect(autocommit: bool = False) -> psycopg.Connection:
    """psycopg v3 の接続を開く。"""
    return psycopg.connect(database_url(), autocommit=autocommit)
