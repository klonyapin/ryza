"""dashboard/queries — 運用ダッシュボードの読み取り専用 DB 層(Issue #10)。

Streamlit UI(``app.py``)から分離した純粋なクエリ関数群。**すべて SELECT のみ**で
書込は行わない。接続先はコード全体と同じ ``RYZA_DATABASE_URL``(``ryza.db.conn``)。

接続は用途で2本に分ける(独立役員審査 2026-08-03 重大-2 の是正):
``connect_readonly`` は読取専用ロール、``connect_boardroom`` は役員室の書込専用
ロールを指す。DB ロールの権限そのものが境界であり、アプリ側の設定はその補強にすぎない。

テーブルは migrations/ に定義されたものだけを参照する:
- 概況   … ``ops.trading_state``(0012) / ``meta.runs``(0001) / ``press.outbox``(0007)
- 成績   … ``ledger.nav_snapshots``(0005)+出資フロー(``ledger.journal_lines``)
- リスク … ``risk.limits_state``(0014) / ``risk.limits_state_events``(0015)
- 取込   … ``docs.documents``(0003) / ``market.bars`` / ``market.indicators``(0002)
           + 鮮度 SLA は ``ryza.ingest.freshness`` の SLA 表を共有
- 報道   … ``press.outbox``(0007)
- コスト … ``meta.runs.cost``(0001。構造は provenance/runs.py: by_tier)
- 市場観 … ``docs.market_view``(0003) / ``docs.market_view_daily``(0010)

**スキーマに無い指標は作らない**(T-018)。UI が欲しがっても列が無ければクエリを
書かず、ページ側で「未実装」と明示する。

テストは ``tests/dashboard/``(テスト専用 DB。UI 自体はテスト対象外)。
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import yaml

from ryza.db.conn import connect
from ryza.ingest.freshness import DEFAULT_SLAS, FreshnessSLA, _latest_as_of

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: 役員室の書込専用ロールの接続 URL(Cloud Run では Secret Manager から env 注入)。
#: 未設定ならローカル開発とみなし ``RYZA_DATABASE_URL`` にフォールバックする。
BOARDROOM_URL_ENV = "RYZA_BOARDROOM_DATABASE_URL"


def connect_readonly() -> psycopg.Connection:
    """読取ページ用の接続(ダッシュボードは操作系を一切持たない)。

    防御は二層で、**主たる層は DB ロールの権限**である。Cloud Run では
    ``RYZA_DATABASE_URL`` が読取専用ロール ``ryza_dashboard``(SELECT のみ GRANT・
    ``default_transaction_read_only = on``)を指すため、書込は権限エラーで拒否される
    (``ops/deploy-dashboard.sh``)。第二層がこの ``SET SESSION CHARACTERISTICS`` で、
    ローカル開発のように特権ロールで接続した場合にも書込を
    ``read_only_sql_transaction`` として弾く。第二層はクライアント側の設定であり
    ``SET`` で解除しうるため、単独では境界にならない。
    """
    conn = connect(autocommit=True)
    conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    return conn


def connect_boardroom() -> psycopg.Connection:
    """役員室専用の**書込可**接続(autocommit)。

    Cloud Run では ``RYZA_BOARDROOM_DATABASE_URL``(Secret ``ryza-boardroom-db-url``)が
    最小権限ロール ``ryza_boardroom`` を指す。このロールが書けるのは
    ``governance.minutes`` / ``governance.minute_resolutions`` / ``governance.stances``
    の INSERT と ``meta.runs`` の INSERT/UPDATE だけで、帳簿・取引状態・監査対象への
    経路を持たない(``ops/deploy-dashboard.sh``)。

    autocommit なのは実ジョブの Run と同じ流儀(即時永続化)。書込先は追記オンリーの
    ため、途中失敗しても改竄は起きない。env 未設定時は ``RYZA_DATABASE_URL`` へ
    フォールバックする(ローカル開発は単一ロール運用)。
    """
    dsn = os.environ.get(BOARDROOM_URL_ENV)
    if dsn:
        return psycopg.connect(dsn, autocommit=True)
    return connect(autocommit=True)


def _rows(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    """カーソルの結果を列名付き dict のリストへ。"""
    cols = [d.name for d in cur.description or []]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


# ── 概況 ──────────────────────────────────────────────────────────────────────
def fetch_trading_state(conn: psycopg.Connection) -> dict[str, Any] | None:
    """取引状態(``ops.trading_state`` シングルトン行)。未初期化なら None。"""
    with conn.cursor() as cur:
        cur.execute("SELECT state, reason, updated_by, updated_at FROM ops.trading_state")
        rows = _rows(cur)
    return rows[0] if rows else None


def fetch_recent_runs(conn: psycopg.Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    """直近のジョブ実行(``meta.runs``)。コストは合計値のみ添える。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_id, job_name, status, started_at, finished_at,
                   (cost ->> 'total_tokens')::bigint         AS total_tokens,
                   (cost ->> 'total_cost_estimate')::numeric AS total_cost_estimate
            FROM meta.runs
            ORDER BY run_id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return _rows(cur)


def fetch_latest_daily_summary(conn: psycopg.Connection) -> dict[str, Any] | None:
    """直近の日次サイクル実行サマリ(#運営 へ投入された embed)。

    ``jobs.daily`` は各ステージ結果を ``press.outbox``(channel='ops')の
    「日次サイクル」embed の fields として残す(daily.py ``_build_ops_embed``)。
    ステージ結果を別テーブルに持たない設計のため、ここから読む。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, created_at, sent_at, embed_json
            FROM press.outbox
            WHERE channel = 'ops'
              AND embed_json ->> 'title' LIKE '日次サイクル%'
            ORDER BY id DESC
            LIMIT 1
            """
        )
        rows = _rows(cur)
    return rows[0] if rows else None


def fetch_latest_daily_run(conn: psycopg.Connection) -> dict[str, Any] | None:
    """直近の日次サイクル実行(``meta.runs`` の ``job_name='jobs.daily'``)。

    ステージ単位の所要時間は記録されていない(``jobs.daily`` は各段の成否と要約だけを
    ``StageResult`` に持ち、outbox embed の field として残す)。したがってここで返せる
    所要はサイクル全体の ``finished_at − started_at`` のみで、段別所要は未実装。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_id, status, started_at, finished_at,
                   EXTRACT(EPOCH FROM (coalesce(finished_at, now()) - started_at))
                       AS duration_seconds
            FROM meta.runs
            WHERE job_name = 'jobs.daily'
            ORDER BY run_id DESC
            LIMIT 1
            """
        )
        rows = _rows(cur)
    return rows[0] if rows else None


def fetch_outbox_pending(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """未配送(``sent_at IS NULL``)の通知をチャンネル別に集計。

    ``{"channel", "pending", "oldest_created_at", "oldest_age_hours"}``。「何件・最古は
    何時間前」がそのまま概況ブロック⑤と承認ページのサマリ行になる。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT channel,
                   count(*)          AS pending,
                   min(created_at)   AS oldest_created_at,
                   EXTRACT(EPOCH FROM (now() - min(created_at))) / 3600 AS oldest_age_hours
            FROM press.outbox
            WHERE sent_at IS NULL
            GROUP BY channel
            ORDER BY 4 DESC NULLS LAST
            """
        )
        return _rows(cur)


# ── 成績(NAV)────────────────────────────────────────────────────────────────
#: 既定の対象帳簿。デモ運用中はファンド帳簿が 1 本(0006 seed の DEMO_FUND)。
DEFAULT_BOOK_ID = "DEMO_FUND"


def fetch_nav_series(
    conn: psycopg.Connection, *, book_id: str = DEFAULT_BOOK_ID
) -> list[dict[str, Any]]:
    """帳簿の日次 NAV 系列(日付昇順)と当日の外部フロー純額。

    NAV の正は ``ledger.nav_snapshots``(``ryza.risk.daily.load_nav_series`` と同じ選択。
    ``risk.nav_daily`` は執行照合を重ねた risk 用ビューであり、正ではない)。

    外部フロー(出資・払戻)は ``ledger.accounts.category='equity'`` かつ
    ``account_id <> 'retained'``(拠出資本勘定)への仕訳の日次合算で、これを引かないと
    増資日のリターンが跳ねる。集計式も risk エンジンと同一にしてある。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.snap_date AS day, s.nav, s.status,
                   coalesce(f.net_flow, 0) AS net_flow
            FROM ledger.nav_snapshots s
            LEFT JOIN (
                SELECT je.entry_date AS d, sum(jl.credit - jl.debit) AS net_flow
                FROM ledger.journal_lines jl
                JOIN ledger.journal_entries je ON je.entry_id = jl.entry_id
                JOIN ledger.accounts a
                  ON a.book_id = jl.book_id AND a.account_id = jl.account_id
                WHERE jl.book_id = %(book)s
                  AND a.category = 'equity' AND a.account_id <> 'retained'
                GROUP BY je.entry_date
            ) f ON f.d = s.snap_date
            WHERE s.book_id = %(book)s
            ORDER BY s.snap_date
            """,
            {"book": book_id},
        )
        return _rows(cur)


# ── リスク ────────────────────────────────────────────────────────────────────
def fetch_limits_state(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """帳簿別のリスクフラグ(``risk.limits_state``)。行が無い帳簿はゲートが fail-closed。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT book_id, dd_soft, dd_hard, vol_exceeded, es_exceeded, as_of, run_id
            FROM risk.limits_state
            ORDER BY book_id
            """
        )
        return _rows(cur)


def fetch_latest_risk_metrics(
    conn: psycopg.Connection, *, book_id: str = DEFAULT_BOOK_ID
) -> dict[str, Any] | None:
    """直近のリスク測定値(``risk.limits_state_events.metrics``)。未計測なら None。

    測定値(DD・実現ボラ・ES95)は ``limits_state`` には無く(同テーブルは boolean の
    フラグだけ)、追記オンリーの台帳側に ``metrics`` jsonb として残る
    (``ryza.risk.state.state_metrics``)。**ダッシュボードは DD を自前で再計算しない** —
    リスクエンジンが出した測定値をリネージ付きでそのまま表示する(不変原則1・3)。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, event, metrics, actor, as_of, run_id, created_at
            FROM risk.limits_state_events
            WHERE book_id = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (book_id,),
        )
        rows = _rows(cur)
    return rows[0] if rows else None


# ── 取込 ──────────────────────────────────────────────────────────────────────
def fetch_freshness(
    conn: psycopg.Connection,
    *,
    slas: list[FreshnessSLA] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """ソース別の最終取込時刻と鮮度 SLA 状態(ok|stale|no_data)。

    SLA 表と「最終取込時点 = as_of の最大値」の定義は ``ryza.ingest.freshness`` と
    共有する(表の二重管理を避ける)。
    """
    slas = slas if slas is not None else DEFAULT_SLAS
    now = now or datetime.now(UTC)
    out: list[dict[str, Any]] = []
    for sla in slas:
        last = _latest_as_of(conn, sla)
        if last is None:
            status, age_hours = "no_data", None
        else:
            age = now - last
            age_hours = age.total_seconds() / 3600
            status = "stale" if age > sla.max_age else "ok"
        out.append(
            {
                "label": sla.label,
                "kind": sla.kind,
                "key": sla.key,
                "sla_hours": sla.max_age.total_seconds() / 3600,
                "last_as_of": last,
                "age_hours": age_hours,
                "status": status,
            }
        )
    return out


def fetch_ingest_daily_counts(
    conn: psycopg.Connection, *, days: int = 30
) -> list[dict[str, Any]]:
    """docs.documents / market.bars / market.indicators の日別取込件数(JST・as_of 基準)。

    返り値は ``{"day", "table", "count"}`` の縦持ち(UI 側でピボットする)。
    """
    sql = """
        SELECT day, 'docs.documents' AS "table", count(*) AS count
        FROM (SELECT (as_of AT TIME ZONE 'Asia/Tokyo')::date AS day
              FROM docs.documents
              WHERE as_of >= now() - make_interval(days => %(days)s)) d
        GROUP BY day
        UNION ALL
        SELECT day, 'market.bars', count(*)
        FROM (SELECT (as_of AT TIME ZONE 'Asia/Tokyo')::date AS day
              FROM market.bars
              WHERE as_of >= now() - make_interval(days => %(days)s)) b
        GROUP BY day
        UNION ALL
        SELECT day, 'market.indicators', count(*)
        FROM (SELECT (as_of AT TIME ZONE 'Asia/Tokyo')::date AS day
              FROM market.indicators
              WHERE as_of >= now() - make_interval(days => %(days)s)) i
        GROUP BY day
        ORDER BY day, "table"
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"days": days})
        return _rows(cur)


# ── 報道 ──────────────────────────────────────────────────────────────────────
def fetch_recent_outbox(
    conn: psycopg.Connection, *, channel: str | None = None, limit: int = 10
) -> list[dict[str, Any]]:
    """``press.outbox`` の直近投入分(embed 込み)。channel 指定で絞り込み。"""
    where = "WHERE channel = %(channel)s" if channel is not None else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, channel, urgent, created_at, sent_at, sent_message_id, embed_json
            FROM press.outbox
            {where}
            ORDER BY id DESC
            LIMIT %(limit)s
            """,  # noqa: S608 - where は固定文字列(値は必ずプレースホルダ)
            {"channel": channel, "limit": limit},
        )
        return _rows(cur)


# ── コスト ────────────────────────────────────────────────────────────────────
def fetch_cost_daily(conn: psycopg.Connection, *, days: int = 30) -> list[dict[str, Any]]:
    """``meta.runs.cost`` の日別・部門別・モデル階層別の集計。

    cost jsonb には部門次元が無い(``by_tier`` のみ。provenance/runs.py)ため、
    **部門は job_name の先頭セグメント**(``ingest.jquants.daily`` → ``ingest``)で
    代理する。日付は started_at の JST 日付。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT (r.started_at AT TIME ZONE 'Asia/Tokyo')::date AS day,
                   split_part(r.job_name, '.', 1)                 AS dept,
                   t.tier                                         AS tier,
                   sum((t.info ->> 'calls')::bigint)              AS calls,
                   sum((t.info ->> 'tokens')::bigint)             AS tokens,
                   sum((t.info ->> 'cost_estimate')::numeric)     AS cost_estimate
            FROM meta.runs r
            CROSS JOIN LATERAL jsonb_each(r.cost -> 'by_tier') AS t(tier, info)
            WHERE r.cost IS NOT NULL
              AND r.started_at >= now() - make_interval(days => %s)
            GROUP BY 1, 2, 3
            ORDER BY 1 DESC, 2, 3
            """,
            (days,),
        )
        return _rows(cur)


def fetch_cost_summary(conn: psycopg.Connection) -> dict[str, Any]:
    """**当月(暦月・JST)**のコスト合計と分母(実行回数)。

    窓を 30 日ローリングではなく暦月にしたのは、比較対象が**月次**予算
    (config/llm.yaml の budget.monthly_jpy → いずれ ledger.budgets の月次予算行)
    だからである。分子と分母の期間が食い違うと消化率が意味を持たない(中-9)。

    「1 ジョブ実行あたりコスト」を出すために、コスト記録のある実行数(``cost_runs``)と
    全実行数(``all_runs``)の両方を返す。累計トークン数は返さない — 単調増加する絶対値は
    行動を変えない vanity metric(調査ノート A10)。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FILTER (WHERE cost IS NOT NULL)              AS cost_runs,
                   count(*)                                             AS all_runs,
                   coalesce(sum((cost ->> 'total_cost_estimate')::numeric), 0)
                                                                        AS total_cost,
                   (date_trunc('month', now() AT TIME ZONE 'Asia/Tokyo')
                        AT TIME ZONE 'Asia/Tokyo')                      AS since
            FROM meta.runs
            WHERE started_at >= date_trunc('month', now() AT TIME ZONE 'Asia/Tokyo')
                                   AT TIME ZONE 'Asia/Tokyo'
            """
        )
        return _rows(cur)[0]


# ── 市場観 ────────────────────────────────────────────────────────────────────
def fetch_current_market_view(conn: psycopg.Connection) -> dict[str, Any] | None:
    """現在の市場観(``docs.market_view`` の最新版)。未初期化なら None。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT view_id, ts, regime, key_risks, changes, basis_refs, run_id
            FROM docs.market_view
            ORDER BY view_id DESC
            LIMIT 1
            """
        )
        rows = _rows(cur)
    return rows[0] if rows else None


def fetch_market_view_snapshots(
    conn: psycopg.Connection, *, limit: int = 14
) -> list[dict[str, Any]]:
    """日次スナップショット(各営業日の確定版 = ``docs.market_view_daily``)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT snapshot_date, view_id, ts, as_of, snapshot_id
            FROM docs.market_view_daily
            ORDER BY snapshot_date DESC
            LIMIT %s
            """,
            (limit,),
        )
        return _rows(cur)


# ── 開発ステータス(Issue #10: site/ の統合)────────────────────────────────────
def load_site_status(path: Path | None = None) -> dict[str, Any] | None:
    """開発ステータスサイトのデータ(``site/data.js``)を dict で返す。

    データの正は GitHub Milestones/Issues(``site/build.py`` が生成)。ここでは
    生成済み data.js を読むだけで、ネットワークも DB も使わない。無ければ None。
    """
    path = path if path is not None else _REPO_ROOT / "site" / "data.js"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    match = re.search(r"window\.RYZA_DATA\s*=\s*(\{.*\})\s*;?\s*$", text, re.DOTALL)
    if match is None:
        return None
    return json.loads(match.group(1))


# ── 承認・通知(組織サイト化 — 2026-08-03 代表指示)────────────────────────────
def fetch_decisions(conn: psycopg.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    """承認フローの決定履歴(``governance.decisions``・0007)。

    みなし承認(定款第3条 v0.4)は ``decision='deemed'`` で記録される設計
    (governance.yaml deemed_approval)。現行スキーマの CHECK は
    approve|reject|question のみのため deemed 行はまだ存在しないが、UI 側は
    decision 値で「みなし/明示」を区別する(スキーマ拡張時に自動追随)。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, proposal_ref, kind, decision, decided_by, note, decided_at
            FROM governance.decisions
            ORDER BY id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return _rows(cur)


def fetch_running_runs(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """実行中のジョブ(``meta.runs`` の status='running')。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_id, job_name, started_at, params
            FROM meta.runs
            WHERE status = 'running'
            ORDER BY run_id DESC
            """
        )
        return _rows(cur)


def load_reminders(path: Path | None = None) -> list[dict[str, Any]]:
    """リマインダー・レジストリ(``ops/reminders.yaml``)の一覧。

    「セッション内の約束は無効 — 将来アクションは必ずここに登録」(CLAUDE.md)の
    実体。pending が「代表が知るべき将来アクション」。ファイル読取のみ(DB 不要)。
    """
    path = path if path is not None else _REPO_ROOT / "ops" / "reminders.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    out = []
    for r in data.get("reminders", []):
        action = r.get("action") or {}
        out.append(
            {
                "id": r.get("id"),
                "what": r.get("what"),
                "status": r.get("status", "pending"),
                "action_type": action.get("type"),
                "conditions": ", ".join(
                    str(c.get("type")) for c in r.get("conditions", [])
                ),
            }
        )
    return out


def load_org(path: Path | None = None) -> dict[str, Any]:
    """組織メンバー台帳(``config/org.yaml``・キャラクター設定の正)。"""
    path = path if path is not None else _REPO_ROOT / "config" / "org.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_governance(path: Path | None = None) -> dict[str, Any]:
    """権限マトリクス・統制テーブル(``config/governance.yaml``・定款の機械可読版)。"""
    path = path if path is not None else _REPO_ROOT / "config" / "governance.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_llm_budget(path: Path | None = None) -> dict[str, Any]:
    """LLM の月次予算(``config/llm.yaml`` の ``budget``)。

    予算の**最終的な**正は経営管理部の予算科目(``ledger.budgets``・category=llm_*)で、
    ここは承認フローが動くまでの既定値。キーが無い設定でも壊れないよう空 dict を返す
    (呼び出し側は「予算未設定」と表示して比率を偽造しない)。
    """
    path = path if path is not None else _REPO_ROOT / "config" / "llm.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("budget") or {}


def load_ips_limits(path: Path | None = None) -> dict[str, Any]:
    """IPS のハードリミット(``config/ips.yaml`` の ``hard_limits``)。

    bullet の分母(dd_soft_limit / dd_hard_limit / realized_vol_limit /
    daily_es95_nav_max)を引くためだけに読む。IPS は保護領域であり、ここは読取専用。
    """
    path = path if path is not None else _REPO_ROOT / "config" / "ips.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("hard_limits") or {}


def load_roadmap(path: Path | None = None) -> dict[str, Any]:
    """全体計画(``config/roadmap.yaml``・curated)。更新は設計リードの責務。

    静的な計画(フェーズ・マイルストーン)はこのファイルが正で、動的な状態
    (Issues/PR/meta.runs)は「計画」ページが重ね合わせて表示する。
    """
    path = path if path is not None else _REPO_ROOT / "config" / "roadmap.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))
