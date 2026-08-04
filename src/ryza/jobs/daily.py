"""daily — 日次サイクルの常駐オーケストレータ(T-013)。

設計 30-press-discord §2・00-system-design §2/§10。1 日 1 回、以下を順に走らせる:

  取込 → 前処理(縮退) → 分析エージェント → 市場観更新 → curated ユニバース照合
  → **FM(戦略)** → 執行(デモ) → 締め(照合→NAV)→ リスク(T-015: limits_state 更新+
  リスクレポート)→ 朝刊生成 → outbox → 実行サマリ

**FM 段(T-017)**: Jim(非 LLM・日次)を毎日、Ben(LLM・週次)を ``config/fm_ben.yaml``
の実行曜日に走らせ、提案を ``gate_and_record`` へ通す。**FM ごとに別段**
(``fm.jim`` / ``fm.ben``)にしてあり、Ben の例外で Jim の決定論注文が巻き戻らない
(独立役員審査 T-017 C-5)。**分析の後・執行の前**に置く
(FM 提案 → ゲート → 執行の順 — 設計リード裁定 2026-08-03)。Kill Switch 中は提案自体を
作らない(ゲートも G-0 で block するが、通らないと分かっている案を作らない)。
※ **ルール**による銘柄分類(``market.instrument_classification``)を作るのは risk 段
(T-015)なので、新規に取り込まれた銘柄が FM の候補になるのは翌日以降になる。一方
**curated タグ**は直前の curated 段が当日反映するため、config の付与・撤回は当日効く。

**執行段(T-016)**: 00 §9 の「ゲート → 執行 → 会計記帳 → 照合 → NAV 確定」のうち
ゲート以降を担う(ゲートは注文起票側 = FM 段が ``gate_and_record`` で通す)。
注文が無い日は執行は no-op だが、
締め(MTM・NAV 記帳 → risk.nav_daily)は毎日走らせて NAV 系列を絶やさない(risk 段の
入力)。Kill Switch 中は新規執行のみスキップし、締め(内部会計)は走らせる。
照合ブレイクは ops チャンネルへ embed で通知する。

**curated 段(2026-08-04 の ``fm.jim`` universe=0 事象の是正)**: ``config/universe/*.yaml``
(承認済みの curated ユニバース定義)を毎日 DB へ照合する。以前は「手順書の CLI を人が
一度実行する」運用で、実行漏れが**無言のドリフト**になっていた(実際に承認済みリストが
未反映のまま初回運用を迎え、ユニバースが空だった)。config を正と宣言する以上、config と
DB の一致は機構で保証しなければならない。撤回(config から銘柄を消す)の反映漏れは
売買母集団を広いまま残すため、付与の漏れより危険である。**FM 段の前**に置き、当日の
撤回が当日の提案に効くようにする(設計リード裁定 2026-08-04)。詳細は
``reconcile_curated_universes``。

**risk 段(T-015)**: 00 §9 の順序どおり会計締めの直後に置く(設計リード裁定
2026-08-03)— execution 段の締めが書いた当日の ``ledger.nav_snapshots``(NAV の正。
``risk.nav_daily`` は執行照合を重ねた risk 用ビュー)を読んで limits_state を更新し、
リスクレポートを ops へ投入する。**execution 段が落ちた日は締めの失敗を risk 段へ
渡す**(``close_ok``)— 締めが走っていない日は当日スナップショットも再締めも無く、
リスク日次は前日までの未再締め系列を測ることになるため、レポート先頭に警告を出して
urgent にする(独立審査 再々審査 起草者の留意点 (a))。

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
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from ryza import org
from ryza.bot import COLOR_FLASH, COLOR_NORMAL, DISCLAIMER
from ryza.bot.killswitch import is_engaged
from ryza.bot.outbox import enqueue
from ryza.db.conn import connect
from ryza.execution.close import run_demo_close
from ryza.execution.config import ExecutionConfig
from ryza.execution.demo import DemoBroker
from ryza.execution.runner import run_pending
from ryza.fm.ben import run_ben
from ryza.fm.config import BenConfig
from ryza.fm.jim import run_jim
from ryza.fm.theses import quarantine_stats
from ryza.ips import load_and_validate
from ryza.ledger.closing import RESTATEMENT_URGENT_BUSINESS_DAYS, urgent_restatements
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
from ryza.risk.classify import (
    CURATED_UNIVERSE_DIR,
    apply_curated_universe,
    load_curated_universe,
)
from ryza.risk.daily import run_risk_daily

JST = ZoneInfo("Asia/Tokyo")

# 朝刊 embed の既定タイトル(``embeds.build_morning_embed`` の既定)。冪等判定の目印に使う。
MORNING_TITLE = "Ryza 朝刊"

# 執行段の対象帳簿。デモ二系統のうち daily が回すのはデモのみ(実弾は定款第3条の専決事項)。
DEMO_BOOK = "DEMO_FUND"

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


# FM 段の実行サマリに載せる件数キー(orders 明細は embed に載せない — 冗長なため)。
_FM_SUMMARY_KEYS = (
    "universe", "entries", "exits", "candidates", "closes",
    "quarantined_holdings",  # 根拠を失った保有(審査 C-11)
    "proposed", "passed", "blocked", "skipped",
)


def _fm_summary(result: dict[str, Any]) -> dict[str, Any]:
    """FM 実行結果を件数だけに圧縮する(注文明細は trading.orders 側が正)。"""
    summary = {k: result[k] for k in _FM_SUMMARY_KEYS if k in result}
    if "skipped" in result and isinstance(result["skipped"], str):
        summary["skipped"] = result["skipped"]
    if "rejected" in result:
        summary["rejected"] = len(result["rejected"])
    # E6(point-in-time ユニバース)未達の但し書きは黙って落とさない(審査 C-4)。
    # 通常運転(当日 as_of・履歴カバー済み)では note は None なので何も足さない。
    note = (result.get("pit_universe") or {}).get("note")
    if note:
        summary["e6_note"] = note
    return summary


def _build_breaks_embed(breaks: list[dict[str, Any]], *, as_of: datetime) -> dict[str, Any]:
    """照合ブレイク通知(#運営)の embed を組む(執行照合・ポジション照合共通)。"""
    jst_str = as_of.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
    fields = [
        {
            "name": str(b.get("item", "?")),
            "value": (
                f"帳簿={b.get('ours')} / 相手={b.get('theirs')}"
                f"(book={b.get('book_id')}, {b.get('recon_date')})"
            )[:1024],
            "inline": False,
        }
        # embed の field 上限対策で先頭 10 件のみ表示。永続化の現状(審査条件①):
        # ポジション照合ブレイクは ledger.reconciliations に全件記録されるが、
        # 執行照合(executions×仕訳)ブレイクはこの通知と risk.nav_daily.detail の
        # 要約のみで明細の永続化は未実装 — ops/reminders.yaml
        # (execution-recon-persistence)で将来タスクとして登録済み。
        for b in breaks[:10]
    ]
    return {
        "title": f"⚠️ 執行・会計照合ブレイク {jst_str}",
        "description": (
            "executions×仕訳/ポジション照合に不一致。NAV は provisional のまま。"
            "解消するまで確定しない(00 §9)。"
        ),
        "color": COLOR_FLASH,
        "fields": fields,
        "footer": {"text": DISCLAIMER},
    }


# ── FM 提案の検疫の可視化(独立役員審査 T-017 C-10 の裁定)────────────────────
# 検疫(trading.fm_theses_quarantine)は解除できない封じ込めであり、判断履歴と建玉根拠を
# 恒久的にプロンプトから外す。**silent に大量検疫されないこと**が採用条件のため、
# 日次サマリに必ず件数(当日増分/累計)を出し、閾値超えは別 embed で警告する。
_QUARANTINE_MASS_COUNT = 5       # 1 日の増分がこれ以上なら mass-quarantine
_QUARANTINE_MASS_RATIO = 0.10    # 全提案に占める累計比率がこれ以上なら mass-quarantine


def _is_mass_quarantine(stats: dict[str, int]) -> bool:
    """当日増分または累計比率が閾値を超えたか(増分ゼロなら常に False)。"""
    if stats["today"] <= 0:
        return False
    if stats["today"] >= _QUARANTINE_MASS_COUNT:
        return True
    total_theses = stats["theses_total"]
    return bool(
        total_theses and stats["total"] / total_theses >= _QUARANTINE_MASS_RATIO
    )


def _quarantine_field(stats: dict[str, int]) -> dict[str, Any]:
    """実行サマリの「検疫」フィールド(増分ゼロでも必ず出す)。"""
    mark = "⚠️" if stats["today"] > 0 else "✅"
    value = (
        f"{mark} 当日増分 {stats['today']} 件 / 累計 {stats['total']} 件"
        f"(全提案 {stats['theses_total']} 件)"
    )
    if _is_mass_quarantine(stats):
        value = f"🚨 mass-quarantine — {value}"
    return {"name": "検疫(FM 提案)", "value": value[:1024], "inline": False}


def _build_restatement_embed(
    restated: list[dict[str, Any]], *, as_of: datetime
) -> dict[str, Any]:
    """確定 NAV の書き換え(restatement)通知(#運営)。照合ブレイクと同格の専用 embed。

    再締めは「既に確定・報告した NAV を後から動かす」操作である(独立審査 重要-2 の
    是正は同時に過去改変の経路でもある)。実行サマリの 1 行に混ぜず独立フィールドで
    出し、``RESTATEMENT_URGENT_BUSINESS_DAYS`` より古い日が含まれるときは urgent 扱いで
    色とタイトルを変える(上限や承認は設けない — 是正を止めるより可視化を優先する)。
    """
    jst_str = as_of.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
    urgent = urgent_restatements(restated)
    fields = [
        {
            "name": f"{r['date']}(締め {r['age_business_days']} 回前)",
            "value": (
                f"NAV {r['nav_before']} → {r['nav_after']}"
                f"(status={r['status']} 据え置き / 建玉明細は無効化)"
                # 分岐は**累積の状態**(mtm_pending)で行う — 今回の run の
                # recon_invalidated で切ると、2 回目以降の再締めで再適用に失敗した日に
                # 警告が一切出ない(独立審査 新-12)。
                + ("\n評価替えを当日終値で再適用(as_of リプレイ)" if r.get("mtm_reapplied")
                   else "\n評価替えは前回の再適用値を引き継ぎ(当日バー欠測)"
                   if r.get("mtm_carried_forward")
                   else "\n⚠️ 当日バーが無く評価替え未再適用(建玉は取得原価)"
                   if r.get("mtm_pending") else "")
                + ("\n⚠️ nav_daily に行が無く risk 側は未追随" if r["nav_daily_missing"] else "")
            )[:1024],
            "inline": False,
        }
        for r in restated[:10]  # embed の field 上限対策(件数は description に出す)
    ]
    return {
        "title": (
            f"🚨 確定 NAV の書き換え {jst_str}" if urgent
            else f"📝 NAV の再締め訂正 {jst_str}"
        ),
        "description": (
            f"締めの後に立った仕訳を取り込み、確定済み NAV を {len(restated)} 日ぶん"
            "書き換えた(水位検出)。status は締め時点の照合の結論のため据え置き、"
            "detail に restated / positions_stale を記録している。"
            + (
                f"\n**うち {len(urgent)} 日は "
                f"{RESTATEMENT_URGENT_BUSINESS_DAYS} 営業日より古い既報値の書き換え** — "
                "遅延記帳の原因を確認すること。"
                if urgent else ""
            )
        ),
        "color": COLOR_FLASH if urgent else COLOR_NORMAL,
        "fields": fields,
        "author": org.author_for_role("audit"),
        "footer": {"text": DISCLAIMER},
    }


def _build_residue_embed(
    residue: dict[str, Any], *, book_id: str, day: str, as_of: datetime
) -> dict[str, Any]:
    """説明不能な残渣(原価恒等式の破れ)の通知(#運営 — 独立審査 新-15)。

    照合ブレイクと同格の専用 embed にする。実行サマリの 1 行に混ぜると ✅ 付きで埋もれる
    (再-7 と同じ欠陥)。残渣は放置すると評価替えの経路を通らないまま NAV に居座り、
    ``book_returns`` → ``ewma_vol`` → 誤 ``vol_exceeded`` の経路に乗る。

    検出は締めが行う(``ledger.closing`` — 勘定分離 0034 以降は「原価勘定の残高 = 建玉
    イベント再生の取得原価」という恒等式の破れとして検出する)。**この検査が覆うのは原価
    勘定側だけである**(独立審査 新-19): 評価調整勘定を直接叩く偽装はここには出ず、
    ``post_mark_to_market`` の posted_by 検証と 0034 の DB トリガが防ぐ。
    """
    jst_str = as_of.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
    return {
        "title": f"⚠️ 説明不能な建玉残渣 {jst_str}",
        "description": (
            f"{book_id} {day}: 原価勘定の残高が建玉再生の取得原価と一致しない銘柄が "
            f"{len(residue)} 件ある。締めは評価調整勘定しか洗い替えないため原価側は"
            "手つかずである — 逆仕訳のオペミス(買いだけを取り消して売りを残す等)か、"
            "数量を再生できない申告の約定証憑を疑うこと(証憑を持たない直接記帳は 0034 の"
            "原価勘定ガードが書込時に拒否するので、ここには現れない)。"
            "試算表はゼロバランスのままなので気づけない。"
        ),
        "color": COLOR_FLASH,
        "fields": [
            {
                "name": f"銘柄 {iid}",
                "value": (
                    f"原価勘定 {v['book_value']} / 再生原価 {v.get('replay_cost', '?')}"
                    f"({v.get('reason', '?')})"
                ),
                "inline": True,
            }
            for iid, v in list(residue.items())[:10]
        ],
        "author": org.author_for_role("audit"),
        "footer": {"text": DISCLAIMER},
    }


# ── curated ユニバースの自動照合(2026-08-04 事象の是正)──────────────────────
# 段の名前。実行サマリの描画分岐と ops_summary からの参照に使う。
CURATED_STAGE = "curated"

# 実行サマリ・警告 embed に列挙する symbol / スキップ理由の上限(embed の 1024 字制限対策)。
_CURATED_LIST_LIMIT = 10


def reconcile_curated_universes(
    conn: psycopg.Connection,
    run: Run,
    *,
    as_of: datetime,
    directory: Path | str | None = None,
) -> dict[str, Any]:
    """``config/universe/*.yaml`` を DB へ**冪等に照合**する(config が正)。

    2026-08-04 の ``fm.jim`` universe=0 は、承認済みの ``jim-curated.yaml`` を DB へ
    反映する操作が「手順書の CLI を人が一度実行する」運用だったために起きた。config を
    正と宣言しながら、config と DB の一致を保証する機構が無かった — 反映漏れも反映忘れも
    無言のドリフトになる。とくに**撤回**(config から銘柄を消す = 売買母集団を狭める)の
    未反映はリスク側に倒れるため、照合は毎日走らせて差分をゼロに保つ。

    **冪等**: 反映は ``apply_curated_universe`` → ``upsert_classification`` 経由で、内容が
    その as_of 時点の有効行と同一なら**履歴表(0026)へ追記しない**。したがって差分の無い
    日は ``unchanged`` が増えるだけで、追記オンリー履歴が毎日 35 行ずつ膨らむことはない
    (膨らめば point-in-time 履歴が「いつ変わったか」を示せなくなる)。

    **fail-closed の維持**: ローダの承認3段検査(``approved_at`` / ``approved_by`` /
    ``content_sha256``)はそのまま。検査に落ちたファイルは**反映せずスキップ**し、理由を
    ``skipped`` に残す。例外で daily 全体を止めないのは、未承認の 1 ファイルが朝刊・締め・
    リスクまで巻き添えにするのは過剰だからである。ただし黙殺もしない — ``skipped`` が
    非空の日は実行サマリが 🚨 になり、専用の警告 embed が #運営 へ出る。

    ファイル単位で savepoint を張るのは、1 ファイルの反映失敗(DB エラー等)で他ファイルの
    反映まで巻き戻さないため。返り値は
    ``{files, granted, unchanged, revoked, unresolved, unclassifiable, skipped}``。
    ``unresolved`` / ``unclassifiable`` は ``<ファイル名>:<symbol>`` の形で、どの config の
    どの行が刺さっているかを summary だけで特定できるようにする。
    """
    base = Path(directory) if directory is not None else CURATED_UNIVERSE_DIR
    result: dict[str, Any] = {
        "files": 0,
        "granted": 0,
        "unchanged": 0,
        "revoked": 0,
        "unresolved": [],
        "unclassifiable": [],
        "skipped": [],
    }
    for path in sorted(base.glob("*.yaml")):
        try:
            universe = load_curated_universe(path)
        except Exception as exc:  # noqa: BLE001 - 読めない/承認検査に落ちた = 反映しない
            # 未承認・ハッシュ不一致・YAML 破損はすべて「反映しない」に倒す(fail-closed)。
            # 例外を投げ直さないのは daily 全体を止めないため。理由は summary に必ず出る。
            result["skipped"].append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue
        try:
            with conn.transaction():  # ファイル単位の savepoint(他ファイルを巻き込まない)
                applied = apply_curated_universe(
                    conn, universe, run_id=run.run_id, as_of=as_of
                )
        except Exception as exc:  # noqa: BLE001 - 1 ファイルの反映失敗は他を止めない
            result["skipped"].append(f"{path.name}: 反映失敗 {type(exc).__name__}: {exc}")
            continue
        result["files"] += 1
        for key in ("granted", "unchanged", "revoked"):
            result[key] += applied[key]
        for key in ("unresolved", "unclassifiable"):
            result[key].extend(f"{path.name}:{s}" for s in applied[key])
    return result


def _curated_needs_attention(detail: dict[str, Any]) -> bool:
    """人が見るべき日か。**未反映ファイル・未解決 symbol・撤回**のいずれかがあれば真。

    撤回を含めるのは、母集団が狭まったこと自体が運用上の事件だからである(意図した撤回
    でも、反映されたことを確認できなければ「config が正」を主張できない)。``unchanged``
    だけの日は静かに通す — 毎日 🚨 が出る運用は警告を無効化する。
    """
    return bool(
        detail.get("skipped") or detail.get("unresolved") or detail.get("revoked")
    )


def _curated_summary_value(detail: dict[str, Any]) -> str:
    """実行サマリの curated 段フィールド値(差分ゼロの日でも必ず件数を出す)。"""
    mark = "🚨" if _curated_needs_attention(detail) else "✅"
    parts = [
        f"{mark} files={detail.get('files', 0)} granted={detail.get('granted', 0)} "
        f"unchanged={detail.get('unchanged', 0)} revoked={detail.get('revoked', 0)}"
    ]
    for key, label in (("unresolved", "未解決"), ("unclassifiable", "分類不能")):
        items = detail.get(key) or []
        if items:
            shown = ", ".join(items[:_CURATED_LIST_LIMIT])
            parts.append(f"{label} {len(items)} 件: {shown}")
    for reason in (detail.get("skipped") or [])[:_CURATED_LIST_LIMIT]:
        parts.append(f"⚠️ 未反映 {reason}")
    return " / ".join(parts)[:1024]


def _build_curated_alert(detail: dict[str, Any], *, as_of: datetime) -> dict[str, Any]:
    """curated 照合の警告 embed(#運営)。照合ブレイク・残渣と同格の専用 embed。

    実行サマリの 1 行に混ぜると ✅ 付きの列に埋もれて ``[:1024]`` で切られる(再-7 と
    同じ欠陥)。**未反映ファイルは「承認済みの config が効いていない」ことそのもの**で
    あり、2026-08-04 に起きた事象の再発である。
    """
    jst_str = as_of.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
    fields: list[dict[str, Any]] = []
    if detail.get("skipped"):
        fields.append({
            "name": f"未反映のファイル({len(detail['skipped'])} 件)",
            "value": "\n".join(detail["skipped"][:_CURATED_LIST_LIMIT])[:1024],
            "inline": False,
        })
    if detail.get("revoked"):
        fields.append({
            "name": "タグ撤回",
            "value": (
                f"{detail['revoked']} 銘柄から config 外のタグを剥がした"
                "(売買母集団が狭まった — 意図した撤回か確認すること)"
            )[:1024],
            "inline": False,
        })
    if detail.get("unresolved"):
        fields.append({
            "name": f"銘柄マスタに無い symbol({len(detail['unresolved'])} 件)",
            "value": (
                ", ".join(detail["unresolved"][:_CURATED_LIST_LIMIT])
                + "\n綴り間違いか、未取込の銘柄。毎回ゼロであるべき"
            )[:1024],
            "inline": False,
        })
    return {
        "title": f"⚠️ curated ユニバース照合 {jst_str}",
        "description": (
            "config/universe/*.yaml と market.instrument_classification の照合で差分・"
            "未反映が出た。**config が正**であり、未反映のファイルは承認済みのリストが"
            "効いていないことを意味する(2026-08-04 の fm.jim universe=0 と同型の事象)。"
            "手順は docs/ops/fm-curated-universe.md。"
        ),
        "color": COLOR_FLASH,
        "author": org.author_for_role("audit"),
        "fields": fields,
        "footer": {"text": DISCLAIMER},
    }


def _build_quarantine_alert(stats: dict[str, int], *, as_of: datetime) -> dict[str, Any]:
    """mass-quarantine の警告 embed(#運営)。照合ブレイクと同じ扱いで別途投入する。"""
    jst_str = as_of.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
    return {
        "title": f"🚨 FM 提案の大量検疫 {jst_str}",
        "description": (
            f"当日 {stats['today']} 件を検疫(累計 {stats['total']} / "
            f"全提案 {stats['theses_total']})。検疫は**解除できない**ため、"
            "判断履歴と建玉根拠がプロンプトから恒久的に外れる。登録経路(手動 SQL・"
            "quarantine_thesis)の実施者と理由を確認すること。"
        ),
        "color": COLOR_FLASH,
        "author": org.author_for_role("audit"),
        "footer": {"text": DISCLAIMER},
    }


def _build_ops_embed(
    stages: list[StageResult],
    *,
    kill_switch: bool,
    posted: bool,
    as_of: datetime,
    dry_run: bool,
    quarantine: dict[str, int] | None = None,
) -> dict[str, Any]:
    """実行サマリ(#運営)の embed を組む。"""
    jst_str = as_of.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
    fields: list[dict[str, Any]] = []
    for s in stages:
        mark = "✅" if s.ok else "⚠️"
        if s.error:
            value = f"{mark} 失敗: {s.error}"[:1024]
        elif s.name == CURATED_STAGE:  # curated 段: 差分ゼロの日でも件数を必ず出す。
            value = _curated_summary_value(s.detail)
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
    if quarantine is not None:
        fields.append(_quarantine_field(quarantine))
    title = "日次サイクル(dry-run)" if dry_run else "日次サイクル"
    return {
        "title": f"{title} {jst_str}",
        "description": (
            "日次サイクルの実行サマリ(取込→前処理→分析→curated 照合→FM→執行/締め"
            "→リスク→朝刊)。"
        ),
        "color": COLOR_NORMAL,
        # 運用報告の発信者 = 監査部門のキャラクター(台帳 org.yaml から役職キーで解決)。
        "author": org.author_for_role("audit"),
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
    fm_llm: StructuredLLM | None = None,
    press_cfg: PressConfig | None = None,
    as_of: datetime | None = None,
    ingest: IngestFn | None = None,
    image: ImageResult | None = None,
    channel_press: str = "press",
    channel_ops: str = "ops",
    dry_run: bool = False,
    curated_dir: Path | str | None = None,
) -> DailyResult:
    """日次サイクルを 1 回実行する。

    各段は独立に失敗許容(savepoint)。朝刊は当日既投稿ならスキップ(冪等)。Kill Switch 中は
    朝刊投稿をスキップする。``conn`` のコミット制御は ``_run_stage`` に委ね、呼び出し側は
    最終的な ``Run.finish`` を担う。

    ``fm_llm`` は Ben(週次・LLM)用の ``StructuredLLM``(``dept_tag='fm.ben'``)。
    None なら Ben をスキップする(Jim は非 LLM のため常に走る)。

    ``curated_dir`` は curated ユニバース定義の探索先(既定 ``config/universe``)。
    本番では既定のまま。テストが同梱リストの内容に依存しないよう差し替え口を開けている。
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

    # ── 4. curated ユニバースの照合(config → DB。2026-08-04 事象の是正)────────
    # **FM 段の前**に置く(設計リード裁定 2026-08-04)。本タスクの動機は「撤回の未反映が
    # リスク側に倒れる」ことであり、config から銘柄を消した当日に Jim が依然その銘柄を
    # 提案できるなら目的を果たさない。付与も同時に当日有効になるが、curated 定義の変更は
    # PR マージ(`Approved:` トレーラつき代表承認・A-18-1 が突合)を経ているため、
    # 当日有効で問題ない。
    stages.append(
        _run_stage(
            conn,
            CURATED_STAGE,
            lambda: reconcile_curated_universes(
                conn, run, as_of=as_of, directory=curated_dir
            ),
        )
    )

    # ── 5. FM(戦略): Jim 日次 + Ben 週次 → ゲート → 注文案 — T-017 ────────────
    # **FM ごとに別段(別 savepoint)**にする(独立役員審査 T-017 C-5)。1段にまとめると
    # Ben(LLM・週次)の例外で同じ段の Jim(決定論・日次)の提案・注文まで巻き戻り、
    # 日次の決定論シグナルが週次の LLM 障害に巻き込まれる。段の失敗許容は FM 単位で効かせる。
    def _fm_jim() -> dict[str, Any]:
        if is_engaged(conn):
            # Kill Switch 中は提案を作らない(ゲートも block するが、通らない案は作らない)。
            return {"skipped": "kill_switch"}
        fm_ips, fm_mandates = load_and_validate()
        return _fm_summary(
            run_jim(
                conn, run, book_id=DEMO_BOOK, as_of=as_of,
                ips=fm_ips, mandates=fm_mandates,
            )
        )

    stages.append(_run_stage(conn, "fm.jim", _fm_jim))

    def _fm_ben() -> dict[str, Any]:
        if is_engaged(conn):
            return {"skipped": "kill_switch"}
        ben_cfg = BenConfig.load()
        weekday = as_of.astimezone(JST).isoweekday()
        if fm_llm is None:
            return {"skipped": "LLM 未注入"}
        if weekday != ben_cfg.weekday:
            return {"skipped": f"週次(実行曜日={ben_cfg.weekday} 当日={weekday})"}
        fm_ips, fm_mandates = load_and_validate()
        return _fm_summary(
            run_ben(
                conn, run, fm_llm, model=config.model_for(ben_cfg.model_tier),
                book_id=DEMO_BOOK, as_of=as_of, cfg=ben_cfg,
                ips=fm_ips, mandates=fm_mandates,
            )
        )

    stages.append(_run_stage(conn, "fm.ben", _fm_ben))

    # ── 6. 執行(デモ)→ 締め(照合 → NAV 確定)— T-016 ──────────────────────
    def _execution() -> dict[str, Any]:
        detail: dict[str, Any] = {}
        breaks: list[dict[str, Any]] = []
        if is_engaged(conn):
            # Kill Switch 中は新規執行をスキップ(通過済み注文も出さない)。
            # 締め(内部会計・NAV 記帳)は運用監視のため走らせる。
            detail["orders"] = "skipped(kill_switch)"
        else:
            broker = DemoBroker(
                conn, config=ExecutionConfig.load(), trade_date=jst_date
            )
            pending = run_pending(
                conn, book_id=DEMO_BOOK, broker=broker,
                run_id=run.run_id, entry_date=jst_date,
            )
            detail.update(
                filled=pending["filled"], rejected=pending["rejected"],
                expired=pending["expired"], errors=len(pending["errors"]),
            )
        close_result = run_demo_close(
            conn, book_id=DEMO_BOOK, date=jst_date,
            run_id=run.run_id, on_break=breaks.append,
        )
        detail["nav"] = str(close_result["nav"])
        detail["nav_status"] = close_result["status"]
        # 確定 NAV の書き換え(restatement)は照合ブレイクと同格の事象として**専用 embed**
        # で出す。実行サマリの 1 行に混ぜると ✅ 付きで埋もれ [:1024] で切られる
        # (navflow 重要-4 と同じ欠陥 — 独立審査 再-7)。サマリ側は件数だけに留める。
        restated = [r for r in close_result["reclose"] if r["restated"]]
        if restated:
            detail["restated_days"] = len(restated)
            enqueue(
                conn, channel_ops,
                _build_restatement_embed(restated, as_of=as_of), run.run_id,
            )
        # 原価勘定の残高が建玉再生の原価と合わない銘柄(評価替えの経路では消えない)。
        # 検出は締めが行い(ledger.closing — 独立審査 新-15)、ここで人へ届ける。
        residue = close_result["ledger"].get("unexplained_residue") or {}
        if residue:
            detail["unexplained_residue"] = len(residue)
            enqueue(
                conn, channel_ops,
                _build_residue_embed(
                    residue, book_id=DEMO_BOOK, day=jst_date.isoformat(), as_of=as_of
                ),
                run.run_id,
            )
        if breaks:
            detail["breaks"] = len(breaks)
            enqueue(conn, channel_ops, _build_breaks_embed(breaks, as_of=as_of), run.run_id)
        return detail

    execution_stage = _run_stage(conn, "execution", _execution)
    stages.append(execution_stage)

    # ── 7. リスクエンジン(T-015)──────────────────────────────────────────────
    # 00 §9 の順序どおり会計締め(execution 段の照合→NAV 確定)の直後に置く(設計
    # リード裁定 2026-08-03)。execution 段が書いた当日 NAV を読んで limits_state を
    # 更新する。決定論・LLM 不関与のため dry-run でもそのまま実行する。
    #
    # **締めの成否を渡す**(独立審査 再々審査 (a)): execution 段は savepoint で囲まれて
    # いるので、段が落ちた日は当日のスナップショットも再締めも**残らない**(段の
    # ロールバックで消える)。つまり ``execution_stage.ok`` はそのまま「当日の締めが
    # 系列に反映されたか」であり、偽なら risk 段は未再締めの・前日までの系列を測る。
    # その事実を伏せたまま DD・実現ボラ・ES を出さない(risk 側が先頭表示+urgent)。
    stages.append(
        _run_stage(
            conn,
            "risk",
            lambda: run_risk_daily(
                conn, run, as_of=as_of,
                close_ok=execution_stage.ok, close_error=execution_stage.error,
            ),
        )
    )

    # ── 8. 朝刊生成(冪等・Kill Switch ゲート)───────────────────────────────
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

    # ── 9. 実行サマリを #運営 へ ──────────────────────────────────────────────
    def _ops_summary() -> dict[str, Any]:
        # 検疫の件数は毎日必ず出す(解除できない封じ込めの検知可能化 — 審査 C-10)。
        stats = quarantine_stats(conn, as_of=as_of)
        # risk 段が**全域例外で**落ちた場合、実行サマリを urgent で昇格する(部分失敗は
        # risk_daily 側の urgent 埋め込みが拾う二段構え — 独立役員審査 2026-08-04 M-1)。
        # G-10 の限度状態鮮度検査(独立役員審査 2026-08-03 T-015 統合条件)の**根っこ**
        # に当たる: engine が update しない日が積み重なれば as_of が古びて、いずれ
        # ゲートが block を返し始める。全域停止の 1 日目から確実に urgent で捕らえる。
        # 呼び出し順序(stages への risk 段追加)が壊れたら fail-closed で例外を出す —
        # ops_summary 段自体は _run_stage が握るので日次サイクルは止まらない。
        risk_stage = next((s for s in stages if s.name == "risk"), None)
        assert risk_stage is not None
        risk_failed = not risk_stage.ok
        embed = _build_ops_embed(
            stages, kill_switch=state["kill_switch"], posted=state["posted"],
            as_of=as_of, dry_run=dry_run, quarantine=stats,
        )
        oid = enqueue(conn, channel_ops, embed, run.run_id, urgent=risk_failed)
        state["ops_outbox_id"] = oid
        detail = {
            "ops_outbox_id": oid,
            "quarantine_today": stats["today"],
            "risk_failed": risk_failed,
        }
        if _is_mass_quarantine(stats):
            detail["quarantine_alert_outbox_id"] = enqueue(
                conn, channel_ops, _build_quarantine_alert(stats, as_of=as_of), run.run_id
            )
        # curated 照合の差分・未反映は専用 embed で出す(サマリ 1 行に埋もれさせない)。
        curated = next((s for s in stages if s.name == CURATED_STAGE), None)
        if curated is not None and curated.ok and _curated_needs_attention(curated.detail):
            detail["curated_alert_outbox_id"] = enqueue(
                conn, channel_ops,
                _build_curated_alert(curated.detail, as_of=as_of), run.run_id,
            )
        return detail

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
) -> tuple[StructuredLLM, StructuredLLM, StructuredLLM]:
    """research / press / fm 用の ``StructuredLLM`` を組む(dry-run はフィクスチャ)。

    部門タグ(``dept_tag``)を分けるのはユニットエコノミクス台帳の前提(§5・
    CLAUDE.md「LLM 呼び出しには部門・タスク種別タグを付ける」)。
    """
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
    fm_llm = StructuredLLM(provider, run, dept_tag="fm.ben", price_per_1k=price)
    return research_llm, press_llm, fm_llm


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
        research_llm, press_llm, fm_llm = _build_llms(config, run, dry_run=args.dry_run)
        result = run_daily(
            conn, run, research_llm=research_llm, press_llm=press_llm,
            fm_llm=fm_llm, config=config, dry_run=args.dry_run,
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
