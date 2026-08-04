"""daily の curated ユニバース自動照合(2026-08-04 の ``fm.jim`` universe=0 の是正)。

固定するのは4点:

1. **反映**: ``config/universe/*.yaml`` が daily の中で自動的に DB へ照合される
2. **冪等**: 差分の無い日は ``unchanged`` だけが増え、**追記オンリー履歴
   (``market.instrument_classification_history``)に新規行を書かない**。毎日 35 行ずつ
   膨らめば「いつタグが変わったか」を履歴から読めなくなる(point-in-time の汚染)
3. **fail-closed**: 承認3段検査に落ちたファイルは反映せずスキップし、daily は止まらない。
   ただし黙殺せず実行サマリと専用 embed に出す
4. **撤回**: config から消えた銘柄はタグを剥がす(config が正 — 反映漏れは母集団を
   広いまま残すためリスク側に倒れる)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from ryza.jobs import daily
from ryza.jobs.daily import (
    _build_curated_alert,
    _curated_needs_attention,
    _curated_summary_value,
    reconcile_curated_universes,
    run_daily,
)
from ryza.risk.classify import CURATED_UNIVERSE_DIR, load_classification

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ── ヘルパ ───────────────────────────────────────────────────────────────────
def _write_universe(directory: Path, symbols, *, filename="test.yaml", **overrides) -> Path:
    """テスト用の curated ユニバース定義を書き出す(承認済み・ハッシュ整合)。"""
    from ryza.risk.classify import curated_content_digest

    entries = [
        {"symbol": s, "tags": ["liquid_equity"], "rationale": "テスト用の根拠"}
        for s in symbols
    ]
    doc = {
        "name": "test-liquid",
        "version": "1",
        "criterion": "テスト用の基準",
        "manages_tags": ["liquid_equity"],
        "approved_at": "2026-08-04",
        "approved_by": "representative",
        "entries": entries,
    }
    doc.update(overrides)
    doc.setdefault(
        "content_sha256", curated_content_digest(str(doc["criterion"]), entries)
    )
    path = directory / filename
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return path


def _insert_instrument(conn, symbol: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market.instruments (symbol, asset_class, venue, currency, valid_from)
            VALUES (%s, 'equity', 'TSE', 'JPY', now() - interval '30 days')
            RETURNING instrument_id
            """,
            (symbol,),
        )
        return cur.fetchone()[0]


def _history_count(conn, instrument_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM market.instrument_classification_history "
            "WHERE instrument_id = %s",
            (instrument_id,),
        )
        return cur.fetchone()[0]


def _run_daily(conn, run, config, make_daily_llms, **kwargs):
    research, press, _ = make_daily_llms()
    return run_daily(
        conn, run, research_llm=research, press_llm=press,
        config=config, dry_run=True, **kwargs,
    )


# ── 反映と冪等 ───────────────────────────────────────────────────────────────
def test_reconcile_grants_then_stays_unchanged(conn, run, curated_dir):
    """初回は granted、2回目以降は unchanged(件数の冪等)。"""
    inst = _insert_instrument(conn, "CUR1.T")
    _write_universe(curated_dir, ["CUR1.T"])
    # as_of は未来にできない(0026 の CHECK classification_history_as_of_not_future)。
    # 「昨日の daily → 今日の daily」を過去→現在で模す。
    yesterday = datetime.now(UTC) - timedelta(days=1)

    first = reconcile_curated_universes(conn, run, as_of=yesterday, directory=curated_dir)
    assert first["files"] == 1
    assert first["granted"] == 1 and first["unchanged"] == 0 and first["revoked"] == 0
    assert first["unresolved"] == [] and first["skipped"] == []
    loaded = load_classification(conn, inst)
    assert loaded is not None and "liquid_equity" in loaded.universe_tags

    second = reconcile_curated_universes(
        conn, run, as_of=datetime.now(UTC), directory=curated_dir
    )
    assert second["granted"] == 0 and second["unchanged"] == 1
    assert second["revoked"] == 0 and second["skipped"] == []


def test_reconcile_does_not_grow_history_when_unchanged(conn, run, curated_dir):
    """**冪等性の本体**: 差分の無い再実行は履歴表に新規行を書かない。

    毎日走らせるため、unchanged でも 1 行ずつ積むと分類履歴が日数×銘柄数で膨らみ、
    「いつ liquid_equity になったか」を履歴から読めなくなる(point-in-time の汚染)。
    ここで固定しているのは ``upsert_classification`` の「その as_of 時点で有効な行と
    内容が同一なら追記しない」挙動である(``apply_curated_universe`` はこの口を通る)。
    """
    inst = _insert_instrument(conn, "CUR2.T")
    _write_universe(curated_dir, ["CUR2.T"])
    # 6 日前に初回反映 → 以後 5 日ぶんの daily(as_of は未来にできないので過去から現在へ)。
    base = datetime.now(UTC) - timedelta(days=6)

    reconcile_curated_universes(conn, run, as_of=base, directory=curated_dir)
    after_first = _history_count(conn, inst)
    assert after_first == 1  # 付与は必ず履歴に 1 行だけ残る(PIT の要)

    for day in range(1, 6):
        result = reconcile_curated_universes(
            conn, run, as_of=base + timedelta(days=day), directory=curated_dir
        )
        assert result["unchanged"] == 1 and result["granted"] == 0
        # 各日の実行後も履歴は増えていない(毎日 1 行ずつ積まない)。
        assert _history_count(conn, inst) == after_first


def test_reconcile_revokes_symbol_dropped_from_config(conn, run, curated_dir):
    """config から消した銘柄はタグを剥がす(撤回も config 駆動 — 反映漏れは危険側)。"""
    keep = _insert_instrument(conn, "CUR3.T")
    drop = _insert_instrument(conn, "CUR4.T")
    _write_universe(curated_dir, ["CUR3.T", "CUR4.T"])
    first = reconcile_curated_universes(
        conn, run, as_of=datetime.now(UTC) - timedelta(days=1), directory=curated_dir
    )
    assert first["granted"] == 2

    _write_universe(curated_dir, ["CUR3.T"])  # 同名ファイルを上書き = 撤回
    second = reconcile_curated_universes(
        conn, run, as_of=datetime.now(UTC), directory=curated_dir
    )
    assert second["revoked"] == 1 and second["unchanged"] == 1
    assert "liquid_equity" in load_classification(conn, keep).universe_tags
    assert "liquid_equity" not in load_classification(conn, drop).universe_tags


def test_reconcile_reports_unresolved_symbols(conn, run, curated_dir):
    """銘柄マスタに無い symbol は例外にせず、ファイル名つきで露出する。"""
    _write_universe(curated_dir, ["NOPE.T"])
    result = reconcile_curated_universes(
        conn, run, as_of=datetime.now(UTC), directory=curated_dir
    )
    assert result["files"] == 1 and result["granted"] == 0
    assert result["unresolved"] == ["test.yaml:NOPE.T"]


# ── fail-closed: 承認検査に落ちたファイル ────────────────────────────────────
def test_reconcile_skips_unapproved_file_without_stopping(conn, run, curated_dir):
    """未承認ファイルは反映せずスキップし、理由を残す(例外で daily を止めない)。"""
    inst = _insert_instrument(conn, "CUR5.T")
    _write_universe(
        curated_dir, ["CUR5.T"], filename="unapproved.yaml", approved_at=None,
    )
    result = reconcile_curated_universes(
        conn, run, as_of=datetime.now(UTC), directory=curated_dir
    )
    assert result["files"] == 0 and result["granted"] == 0
    assert len(result["skipped"]) == 1
    assert result["skipped"][0].startswith("unapproved.yaml:")
    assert "未承認" in result["skipped"][0]
    assert load_classification(conn, inst) is None  # 反映されていない(fail-closed)


def test_reconcile_applies_valid_files_despite_a_broken_one(conn, run, curated_dir):
    """1 ファイルが検査に落ちても、他の承認済みファイルは反映する。"""
    ok_inst = _insert_instrument(conn, "CUR6.T")
    _write_universe(curated_dir, ["CUR6.T"], filename="a-ok.yaml")
    _write_universe(
        curated_dir, ["CUR6.T"], filename="b-badhash.yaml",
        content_sha256="0" * 64,
    )
    result = reconcile_curated_universes(
        conn, run, as_of=datetime.now(UTC), directory=curated_dir
    )
    assert result["files"] == 1 and result["granted"] == 1
    assert len(result["skipped"]) == 1
    assert "b-badhash.yaml" in result["skipped"][0]
    assert "liquid_equity" in load_classification(conn, ok_inst).universe_tags


def test_reconcile_skips_malformed_yaml(conn, run, curated_dir):
    """YAML として壊れているファイルも「反映しない」に倒す(例外を漏らさない)。"""
    (curated_dir / "broken.yaml").write_text("entries: [\n", encoding="utf-8")
    result = reconcile_curated_universes(
        conn, run, as_of=datetime.now(UTC), directory=curated_dir
    )
    assert result["files"] == 0 and len(result["skipped"]) == 1


def test_shipped_universe_dir_is_applied_by_the_automatic_path(conn, run):
    """**本事象の回帰**: 同梱の承認済み config が自動経路から実際に読める。

    2026-08-04 の universe=0 は「承認済みの jim-curated.yaml が DB へ反映されていない」
    ことが原因だった。ここでは既定パス(``config/universe``)を明示して照合を走らせ、
    ファイルが 1 件以上読めて **skipped がゼロ**であることを固定する(承認が外れる・
    ハッシュがずれる・ローダの検査が厳しくなる、のいずれでも落ちる)。
    """
    assert CURATED_UNIVERSE_DIR == _REPO_ROOT / "config" / "universe"
    result = reconcile_curated_universes(
        conn, run, as_of=datetime.now(UTC), directory=CURATED_UNIVERSE_DIR
    )
    assert result["files"] >= 1
    assert result["skipped"] == []


# ── daily への配線 ───────────────────────────────────────────────────────────
def test_daily_curated_stage_runs_before_risk(
    conn, run, llm_config, make_daily_llms, curated_dir
):
    """curated 段は risk 段(分類ステップ)の直前に入る。"""
    _insert_instrument(conn, "CUR7.T")
    _write_universe(curated_dir, ["CUR7.T"])
    result = _run_daily(conn, run, llm_config, make_daily_llms)
    names = [s.name for s in result.stages]
    assert names.index(daily.CURATED_STAGE) == names.index("risk") - 1

    stage = result.stage(daily.CURATED_STAGE)
    assert stage is not None and stage.ok, stage.error
    assert stage.detail["files"] == 1 and stage.detail["granted"] == 1


def test_daily_curated_stage_is_idempotent_across_runs(
    conn, run, llm_config, make_daily_llms, curated_dir
):
    """翌日の daily を走らせても履歴は増えない(daily を通した冪等性)。"""
    inst = _insert_instrument(conn, "CUR8.T")
    _write_universe(curated_dir, ["CUR8.T"])
    first = _run_daily(
        conn, run, llm_config, make_daily_llms,
        as_of=datetime.now(UTC) - timedelta(days=1),
    )
    assert first.stage(daily.CURATED_STAGE).detail["granted"] == 1
    rows = _history_count(conn, inst)
    assert rows == 1

    second = _run_daily(conn, run, llm_config, make_daily_llms)
    detail = second.stage(daily.CURATED_STAGE).detail
    assert detail["granted"] == 0 and detail["unchanged"] == 1
    assert _history_count(conn, inst) == rows


def test_daily_summary_always_carries_curated_counts(
    conn, run, llm_config, make_daily_llms, curated_dir
):
    """差分ゼロの日でも実行サマリに curated の件数が載る(無音のドリフトを作らない)。"""
    _insert_instrument(conn, "CUR9.T")
    _write_universe(curated_dir, ["CUR9.T"])
    result = _run_daily(conn, run, llm_config, make_daily_llms)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT embed_json FROM press.outbox WHERE id = %s", (result.ops_outbox_id,)
        )
        embed = cur.fetchone()[0]
    field = next(f for f in embed["fields"] if f["name"] == daily.CURATED_STAGE)
    assert "files=1" in field["value"] and "granted=1" in field["value"]
    # 差分は granted のみ(未反映・未解決・撤回なし)なので警告は出ない。
    assert "curated_alert_outbox_id" not in result.stage("ops_summary").detail


def test_daily_raises_alert_when_a_config_is_not_applied(
    conn, run, llm_config, make_daily_llms, curated_dir
):
    """**今日の事象の再発検知**: 承認検査に落ちた config があれば専用 embed で警告する。"""
    _write_universe(curated_dir, ["CUR10.T"], approved_by="design_lead")
    result = _run_daily(conn, run, llm_config, make_daily_llms)

    stage = result.stage(daily.CURATED_STAGE)
    assert stage.ok and stage.detail["files"] == 0 and stage.detail["skipped"]
    assert result.ok  # daily 全体は止まらない
    detail = result.stage("ops_summary").detail
    assert "curated_alert_outbox_id" in detail
    with conn.cursor() as cur:
        cur.execute(
            "SELECT embed_json FROM press.outbox WHERE id = %s",
            (detail["curated_alert_outbox_id"],),
        )
        embed = cur.fetchone()[0]
    assert "curated ユニバース照合" in embed["title"]
    assert "未反映のファイル(1 件)" in [f["name"] for f in embed["fields"]]


def test_daily_curated_stage_failure_does_not_stop_the_cycle(
    conn, run, llm_config, make_daily_llms, curated_dir, monkeypatch
):
    """段が例外で落ちても後続(risk・朝刊・サマリ)は走る(失敗許容)。"""
    def _boom(*_args, **_kwargs):
        raise RuntimeError("curated boom")

    monkeypatch.setattr(daily, "reconcile_curated_universes", _boom)
    result = _run_daily(conn, run, llm_config, make_daily_llms)
    stage = result.stage(daily.CURATED_STAGE)
    assert not stage.ok and "curated boom" in stage.error
    assert result.stage("risk").ok and result.stage("ops_summary").ok


# ── 決定論の描画ロジック(DB 不要)────────────────────────────────────────────
def _detail(**over):
    base = {
        "files": 1, "granted": 0, "unchanged": 35, "revoked": 0,
        "unresolved": [], "unclassifiable": [], "skipped": [],
    }
    base.update(over)
    return base


def test_attention_rules_are_deterministic():
    """unchanged だけの日は静かに通す(毎日 🚨 が出る運用は警告を無効化する)。"""
    assert not _curated_needs_attention(_detail())
    assert not _curated_needs_attention(_detail(granted=3))
    assert _curated_needs_attention(_detail(revoked=1))
    assert _curated_needs_attention(_detail(unresolved=["a.yaml:X.T"]))
    assert _curated_needs_attention(_detail(skipped=["a.yaml: 未承認"]))


def test_summary_value_marks_and_truncates():
    ok = _curated_summary_value(_detail())
    assert ok.startswith("✅") and "unchanged=35" in ok

    bad = _curated_summary_value(
        _detail(revoked=2, unresolved=[f"a.yaml:S{i}.T" for i in range(30)],
                skipped=["b.yaml: 未承認"])
    )
    assert bad.startswith("🚨") and "revoked=2" in bad
    assert "未解決 30 件" in bad and "未反映 b.yaml" in bad
    assert len(bad) <= 1024


def test_alert_embed_names_the_unapplied_files():
    embed = _build_curated_alert(
        _detail(skipped=["jim-curated.yaml: 未承認"], revoked=1),
        as_of=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
    )
    names = [f["name"] for f in embed["fields"]]
    assert "未反映のファイル(1 件)" in names and "タグ撤回" in names
    assert "docs/ops/fm-curated-universe.md" in embed["description"]
