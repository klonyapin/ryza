"""daily — 日次サイクルの常駐オーケストレータ(T-013)。

設計 30-press-discord §2・00-system-design §2/§10。1 日 1 回、以下を順に走らせる:

  取込 → 前処理(縮退) → 分析エージェント → 市場観更新 → 朝刊生成 → outbox
  → リスク(T-015: limits_state 更新+リスクレポート) → 実行サマリ

**各段は独立に失敗許容**: 各段を savepoint(``conn.transaction()``)で囲み、失敗しても
後続段は走る(前段失敗時は前日データで動く)。実行サマリを ``#運営``(ops)へ投入する。

**冪等**: 朝刊は「その日(JST)の ``press.outbox`` に既に朝刊 embed があればスキップ」。同日再実行で
二重投稿しない(受け入れ基準)。

**Kill Switch**: ``ops.kill_switch`` が立っている場合、取込・分析のみ行い朝刊投稿はスキップする
(フラグは参照のみ・操作は bot の領分)。実行サマリ(#運営 への稼働報告)は Kill Switch 中も投入する
(運用監視のため。市場向けの朝刊 publication だけを止める)。

**前処理の縮退モード(§制約)**: VM は e2-micro(RAM 1GB)で torch を積まないため、埋め込みは
``HashingEmbedder``(stdlib・ダミー)を使い、準重複検出は無効化(``near_threshold=-1.0``)して
content_hash 完全一致のみで重複排除する。埋め込みバックフィルは別途ローカルで実施する設計。
※ ``preprocess/runner`` は埋め込み器を注入可能なため、縮退パスはここで embedder を差し替えるだけで
足り、``src/ryza/preprocess/`` の変更は不要。

**設計との乖離(コメント明記)**: 30 §1 は日次を「Cloud Run Jobs」とするが、DB が VM 内
PostgreSQL(localhost)にあり Cloud Run から届かないため、当面は GCE VM 上の systemd timer で回す
(``ops/deploy-daily.sh``)。Cloud SQL 移行後に Cloud Run Jobs へ寄せる。

**テスト**: 実 API・実ネットワークを呼ばない。LLM は注入した ``StructuredLLM``(フィクスチャ
プロバイダ)経由。CLI ``--dry-run`` は ``DryRunProvider`` で LLM 呼び出しを差し替える。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from ryza.bot import COLOR_NORMAL, DISCLAIMER
from ryza.bot.killswitch import is_engaged
from ryza.bot.outbox import enqueue
from ryza.db.conn import connect
from ryza.preprocess.embed import HashingEmbedder
from ryza.preprocess.runner import run_preprocess
from ryza.press.config import PressConfig
from ryza.press.images import ImageResult
from ryza.press.morning import run_morning
from ryza.provenance import Run, start_run
from ryza.research import market_view
from ryza.research.agents import editor, macro, micro, sentiment
from ryza.research.llm import StructuredLLM
from ryza.research.providers import AnthropicProvider, DryRunProvider, LLMConfig
from ryza.risk.daily import run_risk_daily

JST = ZoneInfo("Asia/Tokyo")

# 朝刊 embed の既定タイトル(``embeds.build_morning_embed`` の既定)。冪等判定の目印に使う。
MORNING_TITLE = "Ryza 朝刊"

# 縮退前処理: 準重複検出を無効化する near_threshold(cos 距離は >= 0 なので -1.0 で常に非該当)。
_DEGRADED_NEAR_THRESHOLD = -1.0

# 取込段のフック: (conn, run, as_of) を受け、任意の集計 dict を返す。省略時は取込をスキップ。
IngestFn = Callable[[psycopg.Connection, Run, datetime], dict[str, Any]]

# 1 ソースの取込を実行するコーラブル(as_of を受け、任意の結果を返す。失敗は例外送出)。
IngestSource = Callable[[datetime], Any]


def _default_ingest_sources() -> list[tuple[str, IngestSource]]:
    """実取込ソース(名前, 実行コーラブル)を順序付きで返す(T-009 + T-012 一括拡張)。

    各ソースは自前の autocommit 接続・Run・Fetcher・証憑ストアを持つ CLI ``main`` を呼ぶ
    (``src/ryza/ingest/`` は **import 利用のみ**・変更しない)。``main`` は成功時 0 を返す。
    """
    from ryza.ingest import (
        calendar,
        edgar,
        edinet,
        estat,
        fred,
        intl_banks,
        jquants,
        news_rss,
        tdnet,
    )

    def _date(as_of: datetime) -> str:
        return as_of.astimezone(JST).date().isoformat()

    return [
        ("jquants", lambda as_of: jquants.main(["--date", _date(as_of)])),
        # tdnet は日付範囲取得(対象日+前日)。recent.rss だと決算集中日(1000件超/日)に
        # 日次 1 回の実行では取りこぼすため(tdnet.py の既定 URL 参照)。
        ("tdnet", lambda as_of: tdnet.main(["--date", _date(as_of)])),
        ("edinet", lambda as_of: edinet.main(["--date", _date(as_of)])),
        ("news_rss", lambda as_of: news_rss.main([])),
        ("fred", lambda as_of: fred.main([])),
        ("calendar", lambda as_of: calendar.main([])),
        # ── T-012 一括拡張分 ────────────────────────────────────────────────
        ("edgar", lambda as_of: edgar.main([])),
        ("estat", lambda as_of: estat.main([])),
        ("intl_banks", lambda as_of: intl_banks.main([])),
    ]


def _auth_error_types() -> tuple[type[BaseException], ...]:
    """資格情報未設定を表す取込側の例外型(これらは失敗でなく skipped として報告する)。"""
    from ryza.ingest.estat import EstatAuthError
    from ryza.ingest.fred import FredAuthError
    from ryza.ingest.jquants import JQuantsAuthError

    return (JQuantsAuthError, FredAuthError, EstatAuthError)


def run_ingest_sources(
    as_of: datetime,
    *,
    dry_run: bool = False,
    sources: list[tuple[str, IngestSource]] | None = None,
    auth_errors: tuple[type[BaseException], ...] | None = None,
) -> dict[str, Any]:
    """実取込ソースを順に呼ぶ(ソースごとに失敗許容)。

    - ``dry_run``: 実ネットワークを一切呼ばず、全ソースを ``skipped`` として報告する。
    - 資格情報未設定(``auth_errors``)は失敗でなく ``skipped``(理由付き)。
    - その他の例外は ``failed``(理由付き)として記録し、後続ソースは続行する。
    """
    sources = sources if sources is not None else _default_ingest_sources()
    auth_errors = auth_errors if auth_errors is not None else _auth_error_types()
    per_source: dict[str, dict[str, Any]] = {}
    counts = {"ok": 0, "skipped": 0, "failed": 0}
    for name, fn in sources:
        if dry_run:
            per_source[name] = {"status": "skipped", "reason": "dry-run(実ネットワーク不使用)"}
            counts["skipped"] += 1
            continue
        try:
            result = fn(as_of)
        except auth_errors as exc:  # 資格情報未設定 → skipped
            per_source[name] = {"status": "skipped", "reason": f"資格情報未設定: {exc}"}
            counts["skipped"] += 1
        except Exception as exc:  # noqa: BLE001 - 1 ソースの失敗は握って他を止めない
            per_source[name] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            counts["failed"] += 1
        else:
            per_source[name] = {"status": "ok", "result": result}
            counts["ok"] += 1
    return {"sources": per_source, **counts}


def make_default_ingest(
    *,
    dry_run: bool = False,
    sources: list[tuple[str, IngestSource]] | None = None,
) -> IngestFn:
    """実取込を daily に本配線する ``IngestFn`` を作る(各ソースは自前接続で完結)。"""

    def _ingest(
        _conn: psycopg.Connection, _run: Run, as_of: datetime
    ) -> dict[str, Any]:
        return run_ingest_sources(as_of, dry_run=dry_run, sources=sources)

    return _ingest


@dataclass
class StageResult:
    """1 段の実行結果(成否と要約)。"""

    name: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class DailyResult:
    """日次サイクル 1 回の結果。"""

    as_of: datetime
    stages: list[StageResult]
    morning_outbox_id: int | None
    posted: bool
    kill_switch: bool
    dry_run: bool
    ops_outbox_id: int | None = None

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.stages)

    def stage(self, name: str) -> StageResult | None:
        return next((s for s in self.stages if s.name == name), None)


def _run_stage(
    conn: psycopg.Connection, name: str, fn: Callable[[], dict[str, Any] | None]
) -> StageResult:
    """1 段を savepoint(``conn.transaction()``)で囲んで実行する。

    成功時は savepoint を解放(共有 tx 参加時)またはコミット(トップレベル tx 時)し、失敗時は
    その段だけロールバックして例外を握る(後続段は走らせる)。この二挙動は ``conn`` が既に
    トランザクション中か否かで自動的に切り替わる(テスト=共有 tx で savepoint、本番=独立 conn で
    段ごとコミット)。
    """
    try:
        with conn.transaction():
            detail = fn() or {}
        return StageResult(name=name, ok=True, detail=detail)
    except Exception as exc:  # noqa: BLE001 - 段の失敗は握って後続を走らせる(失敗許容)
        return StageResult(name=name, ok=False, error=f"{type(exc).__name__}: {exc}")


def _morning_already_posted(
    conn: psycopg.Connection, channel: str, jst_date: Any
) -> bool:
    """その日(JST)の ``press.outbox`` に朝刊 embed が既にあるか(冪等判定)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM press.outbox
            WHERE channel = %s
              AND embed_json->>'title' = %s
              AND (created_at AT TIME ZONE 'Asia/Tokyo')::date = %s
            LIMIT 1
            """,
            (channel, MORNING_TITLE, jst_date),
        )
        return cur.fetchone() is not None


def _ensure_market_view(conn: psycopg.Connection, run: Run, as_of: datetime) -> None:
    """市場観が未初期化なら空の版でブートストラップする(editor 適用の前提)。"""
    if market_view.load_current(conn) is None:
        market_view.initialize(
            conn, run, regime={}, key_risks=[], basis_refs=[], as_of=as_of
        )


def _build_ops_embed(
    stages: list[StageResult], *, kill_switch: bool, posted: bool, as_of: datetime, dry_run: bool
) -> dict[str, Any]:
    """実行サマリ(#運営)の embed を組む。"""
    jst_str = as_of.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
    fields: list[dict[str, Any]] = []
    for s in stages:
        mark = "✅" if s.ok else "⚠️"
        if s.error:
            value = f"{mark} 失敗: {s.error}"[:1024]
        elif "sources" in s.detail:  # 取込段: ソース別ステータスを compact に。
            per = ", ".join(f"{n}:{v['status']}" for n, v in s.detail["sources"].items())
            value = (
                f"{mark} ok={s.detail['ok']} skipped={s.detail['skipped']} "
                f"failed={s.detail['failed']} — {per}"
            )[:1024]
        else:
            detail = ", ".join(f"{k}={v}" for k, v in s.detail.items()) or "ok"
            value = f"{mark} {detail}"[:1024]
        fields.append({"name": s.name, "value": value, "inline": False})
    fields.append(
        {"name": "朝刊", "value": ("投稿済み" if posted else "スキップ/なし"), "inline": True}
    )
    fields.append(
        {"name": "Kill Switch", "value": ("⛔ 有効" if kill_switch else "✅ 通常"), "inline": True}
    )
    title = "日次サイクル(dry-run)" if dry_run else "日次サイクル"
    return {
        "title": f"{title} {jst_str}",
        "description": "日次サイクルの実行サマリ(取込→前処理→分析→市場観→朝刊)。",
        "color": COLOR_NORMAL,
        "fields": fields,
        "footer": {"text": DISCLAIMER},
    }


def run_daily(
    conn: psycopg.Connection,
    run: Run,
    *,
    research_llm: StructuredLLM,
    press_llm: StructuredLLM,
    config: LLMConfig,
    press_cfg: PressConfig | None = None,
    as_of: datetime | None = None,
    ingest: IngestFn | None = None,
    image: ImageResult | None = None,
    channel_press: str = "press",
    channel_ops: str = "ops",
    dry_run: bool = False,
) -> DailyResult:
    """日次サイクルを 1 回実行する。

    各段は独立に失敗許容(savepoint)。朝刊は当日既投稿ならスキップ(冪等)。Kill Switch 中は
    朝刊投稿をスキップする。``conn`` のコミット制御は ``_run_stage`` に委ね、呼び出し側は
    最終的な ``Run.finish`` を担う。
    """
    as_of = as_of or datetime.now(UTC)
    jst_date = as_of.astimezone(JST).date()
    mid_model = config.model_for("mid")
    stages: list[StageResult] = []
    state: dict[str, Any] = {
        "kill_switch": False, "posted": False, "morning_outbox_id": None
    }

    # ── 1. 取込(任意・モック可)──────────────────────────────────────────────
    if ingest is not None:
        stages.append(_run_stage(conn, "ingest", lambda: ingest(conn, run, as_of)))
    else:
        stages.append(StageResult("ingest", ok=True, detail={"skipped": "no ingest fn"}))

    # ── 2. 前処理(縮退モード: HashingEmbedder + 準重複無効)──────────────────
    def _preprocess() -> dict[str, Any]:
        outcomes = run_preprocess(
            conn, run, embedder=HashingEmbedder(),
            near_threshold=_DEGRADED_NEAR_THRESHOLD,
        )
        return {"processed": len(outcomes), "mode": "degraded"}

    stages.append(_run_stage(conn, "preprocess", _preprocess))

    # ── 3. 分析エージェント → 市場観更新 → 日次スナップショット ────────────────
    def _analysis() -> dict[str, Any]:
        m = macro.analyze(conn, run, research_llm, model=mid_model, as_of=as_of)
        mi = micro.analyze(conn, run, research_llm, model=mid_model, as_of=as_of)
        se = sentiment.analyze(conn, run, research_llm, model=mid_model, as_of=as_of)
        _ensure_market_view(conn, run, as_of)
        report_id, apply_result = editor.run_editor(
            conn, run, research_llm, model=mid_model, as_of=as_of
        )
        snap = market_view.snapshot_daily(conn, run, as_of=as_of)
        return {
            "macro": m, "micro": mi, "sentiment": se, "editor": report_id,
            "view_updated": apply_result.view_id if apply_result else None,
            "snapshot": snap,
        }

    stages.append(_run_stage(conn, "analysis", _analysis))

    # ── 4. 朝刊生成(冪等・Kill Switch ゲート)───────────────────────────────
    def _morning() -> dict[str, Any]:
        kill = is_engaged(conn)
        state["kill_switch"] = kill
        if kill:
            return {"skipped": "kill_switch"}
        if _morning_already_posted(conn, channel_press, jst_date):
            return {"skipped": "already_posted"}
        result = run_morning(
            conn, run, press_llm, cfg=press_cfg, model=mid_model,
            as_of=as_of, image=image, channel=channel_press,
        )
        state["morning_outbox_id"] = result.outbox_id
        state["posted"] = result.outbox_id is not None
        return {
            "outbox_id": result.outbox_id,
            "accepted": len(result.accepted),
            "rejected": len(result.rejected),
        }

    stages.append(_run_stage(conn, "morning", _morning))

    # ── 5. リスクエンジン(T-015)──────────────────────────────────────────────
    # 00 §9 の順序では会計締め(記帳→NAV→照合→NAV 確定)の直後・リスクレポートの
    # 位置。会計締めの daily 配線が入った際は、この risk 段はその後に置くこと。
    # 決定論・LLM 不関与のため dry-run でもそのまま実行する。
    stages.append(
        _run_stage(conn, "risk", lambda: run_risk_daily(conn, run, as_of=as_of))
    )

    # ── 6. 実行サマリを #運営 へ ──────────────────────────────────────────────
    def _ops_summary() -> dict[str, Any]:
        embed = _build_ops_embed(
            stages, kill_switch=state["kill_switch"], posted=state["posted"],
            as_of=as_of, dry_run=dry_run,
        )
        oid = enqueue(conn, channel_ops, embed, run.run_id)
        state["ops_outbox_id"] = oid
        return {"ops_outbox_id": oid}

    stages.append(_run_stage(conn, "ops_summary", _ops_summary))

    return DailyResult(
        as_of=as_of,
        stages=stages,
        morning_outbox_id=state["morning_outbox_id"],
        posted=state["posted"],
        kill_switch=state["kill_switch"],
        dry_run=dry_run,
        ops_outbox_id=state.get("ops_outbox_id"),
    )


def _build_llms(
    config: LLMConfig, run: Run, *, dry_run: bool
) -> tuple[StructuredLLM, StructuredLLM]:
    """research / press 用の ``StructuredLLM`` を組む(dry-run はフィクスチャプロバイダ)。"""
    if dry_run:
        provider: Any = DryRunProvider()
    else:
        provider = AnthropicProvider(
            api_version=config.api_version,
            max_tokens=config.max_tokens_for("mid"),
        )
    price = config.price_map()
    research_llm = StructuredLLM(provider, run, dept_tag="research", price_per_1k=price)
    press_llm = StructuredLLM(provider, run, dept_tag="press", price_per_1k=price)
    return research_llm, press_llm


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI 実行パス
    """CLI: 日次サイクルを 1 回実行する。``uv run python -m ryza.jobs.daily [--dry-run]``"""
    parser = argparse.ArgumentParser(description="Ryza 日次サイクル(T-013)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="LLM 呼び出しをフィクスチャに差し替えて実行(実 API を呼ばない)",
    )
    args = parser.parse_args(argv)

    config = LLMConfig.load()
    # start_run は自前の autocommit 接続で running 行を即時永続化する(作業用 conn とは別)。
    run = start_run("jobs.daily", {"dry_run": args.dry_run})
    conn = connect()
    try:
        research_llm, press_llm = _build_llms(config, run, dry_run=args.dry_run)
        result = run_daily(
            conn, run, research_llm=research_llm, press_llm=press_llm,
            config=config, dry_run=args.dry_run,
            ingest=make_default_ingest(dry_run=args.dry_run),
        )
        run.finish("success" if result.ok else "failed")
    except Exception:
        run.finish("failed")
        raise
    finally:
        conn.close()

    for s in result.stages:
        mark = "OK " if s.ok else "NG "
        print(f"[{mark}] {s.name}: {s.error or s.detail}", file=sys.stderr)
    print(
        f"日次サイクル完了(posted={result.posted} kill_switch={result.kill_switch} "
        f"dry_run={result.dry_run})",
        file=sys.stderr,
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
