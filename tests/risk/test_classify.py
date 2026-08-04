"""銘柄マスタ由来の決定論分類(T-015 — T-014 引き継ぎ: G-2 配線と「空 vs 未取得」)。

中盤は **point-in-time 履歴化**(0026・独立役員審査 T-017 C-4 の是正)の回帰:
分類の変更が過去に漏れないこと(look-ahead 排除)・履歴が追記オンリーであること・
履歴がカバーしていない as_of には E6 未達の但し書きが付くこと。

後半は **IPS §8.1 資産クラス列**(0028)と **curated ユニバース**の回帰。前者は
「分類の正を1箇所にする」ための列であり、語彙が config/ips.yaml・classify.py・DB の
CHECK の三者で一致していることを固定する(規約ではなくテストで決着 — 議論規約4)。
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
import yaml

from ryza.risk.classify import (
    IPS_ASSET_CLASSES,
    Classification,
    CuratedUniverseError,
    apply_curated_universe,
    classification_pit_status,
    classify_current_instruments,
    classify_instrument,
    curated_content_digest,
    history_coverage_since,
    load_classification,
    load_classification_at,
    load_curated_universe,
    upsert_classification,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_0028 = _REPO_ROOT / "migrations" / "0028_classification_asset_class.sql"
_HISTORY_TABLE = "market.instrument_classification_history"


# ── ルール分類(純ロジック)────────────────────────────────────────────────────
def test_tse_equity_classification():
    c = classify_instrument(symbol="7203.T", asset_class="equity", venue="TSE")
    assert c == Classification(
        universe_tags=("jp_equity_cash",),
        instrument_flags=(),
        is_single_name=True,
        product="listed_equity_cash",
        unit_size=Decimal(100),
        asset_class="equity_jp",
    )


def test_us_equity_classification():
    c = classify_instrument(symbol="AAPL", asset_class="equity", venue="NASDAQ")
    assert c is not None
    assert c.universe_tags == ("us_equity_cash",)
    assert c.is_single_name is True and c.unit_size is None
    assert c.asset_class == "equity_us"


def test_fx_classification():
    c = classify_instrument(symbol="USD/JPY", asset_class="fx", venue="SAXO")
    assert c is not None
    assert c.universe_tags == ("fx",) and c.product == "exchange_fx"
    assert c.is_single_name is False
    assert c.asset_class == "fx"


def test_etf_and_futures_not_rule_classified():
    """ETF はレバ/インバース該当をマスタから否定できない — フラグなし主張は fail-open の
    ため None(curated 供給のみ)。先物は指数/金利/商品の別がマスタに無い。"""
    assert classify_instrument(symbol="1570.T", asset_class="etf", venue="TSE") is None
    assert classify_instrument(symbol="NK225M", asset_class="future", venue="OSE") is None
    assert classify_instrument(symbol="BTC-PERP", asset_class="crypto", venue="DERIBIT") is None


def test_unknown_venue_equity_unclassified():
    assert classify_instrument(symbol="X", asset_class="equity", venue="LSE") is None


# ── DB: 空 vs 未取得の区別 ────────────────────────────────────────────────────
def _insert_instrument(conn, *, symbol, asset_class, venue):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market.instruments (symbol, asset_class, venue, currency, valid_from)
            VALUES (%s, %s, %s, 'JPY', now())
            RETURNING instrument_id
            """,
            (symbol, asset_class, venue),
        )
        return cur.fetchone()[0]


def test_no_row_means_unfetched(conn):
    """行なし=未取得 → None(呼び出し側はタグ空で提案を組み、G-2 が fail-closed block)。"""
    assert load_classification(conn, 999_999_999) is None


def test_empty_arrays_mean_fetched_none_applicable(conn, run_id):
    """行あり・配列空=「取得済み該当なし」— 未取得(None)と区別できる(審査条件7)。"""
    inst = _insert_instrument(conn, symbol="X1", asset_class="equity", venue="TSE")
    curated = Classification(
        universe_tags=(),
        instrument_flags=(),
        is_single_name=True,
        product="listed_equity_cash",
        unit_size=Decimal(100),
    )
    upsert_classification(conn, inst, curated, run_id=run_id, source="curated")
    loaded = load_classification(conn, inst)
    assert loaded is not None
    assert loaded.universe_tags == () and loaded.instrument_flags == ()


def test_classify_current_instruments_sweep(conn, run_id):
    tse = _insert_instrument(conn, symbol="7203.T", asset_class="equity", venue="TSE")
    fut = _insert_instrument(conn, symbol="NK225M", asset_class="future", venue="OSE")
    counts = classify_current_instruments(conn, run_id=run_id)
    assert counts["classified"] >= 1
    assert counts["unclassifiable"] >= 1
    assert load_classification(conn, tse) is not None
    assert load_classification(conn, fut) is None  # 未分類のまま(curated 待ち)
    # 再実行: 分類済みは対象外(冪等)、未分類は未分類のまま数え直される。
    counts2 = classify_current_instruments(conn, run_id=run_id)
    assert counts2["classified"] == 0
    assert counts2["already"] >= 1


def test_sweep_does_not_overwrite_curated(conn, run_id):
    inst = _insert_instrument(conn, symbol="9984.T", asset_class="equity", venue="TSE")
    curated = Classification(
        universe_tags=("jp_equity_cash", "liquid_equity"),
        instrument_flags=(),
        is_single_name=True,
        product="listed_equity_cash",
        unit_size=Decimal(100),
    )
    upsert_classification(conn, inst, curated, run_id=run_id, source="curated")
    classify_current_instruments(conn, run_id=run_id)
    loaded = load_classification(conn, inst)
    assert loaded is not None and "liquid_equity" in loaded.universe_tags


# ── point-in-time 履歴(0026 — 審査 C-4)──────────────────────────────────────
def _tagged(*tags: str) -> Classification:
    return Classification(
        universe_tags=tags,
        instrument_flags=(),
        is_single_name=True,
        product="listed_equity_cash",
        unit_size=Decimal(100),
        asset_class="equity_jp",
    )


def _history(conn, instrument_id: int) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT universe_tags, as_of, source, backfilled
            FROM market.instrument_classification_history
            WHERE instrument_id = %s ORDER BY history_id
            """,
            (instrument_id,),
        )
        return cur.fetchall()


def _expect_rejected(conn, sql: str, error: type[Exception]) -> None:
    """SQL が拒否されることを確認する(SAVEPOINT でトランザクションを守る)。"""
    with pytest.raises(error), conn.transaction():
        with conn.cursor() as cur:
            cur.execute(sql)


def test_upsert_writes_history_and_current(conn, run_id):
    """分類確定は履歴への追記と現在値の更新を同時に行う(同一トランザクション)。"""
    inst = _insert_instrument(conn, symbol="H1.T", asset_class="equity", venue="TSE")
    t1 = datetime.now(UTC) - timedelta(days=30)
    upsert_classification(conn, inst, _tagged("jp_equity_cash"), run_id=run_id, as_of=t1)

    rows = _history(conn, inst)
    assert len(rows) == 1
    assert rows[0][0] == ["jp_equity_cash"] and rows[0][1] == t1
    assert rows[0][3] is False  # バックフィル行ではない(実時刻の記録)
    assert load_classification(conn, inst) == _tagged("jp_equity_cash")


def test_history_appends_only_on_change(conn, run_id):
    """同内容の再分類では履歴を増やさない(日次スイープの再実行で膨らませない)。"""
    inst = _insert_instrument(conn, symbol="H2.T", asset_class="equity", venue="TSE")
    t1 = datetime.now(UTC) - timedelta(days=30)
    same = _tagged("jp_equity_cash")
    upsert_classification(conn, inst, same, run_id=run_id, as_of=t1)
    upsert_classification(conn, inst, same, run_id=run_id, as_of=t1 + timedelta(days=1))
    assert len(_history(conn, inst)) == 1

    # 内容が変われば追記される(上書きではない)。
    upsert_classification(
        conn, inst, _tagged("jp_equity_cash", "liquid_equity"),
        run_id=run_id, as_of=t1 + timedelta(days=2),
    )
    rows = _history(conn, inst)
    assert len(rows) == 2
    assert rows[0][0] == ["jp_equity_cash"]
    assert rows[1][0] == ["jp_equity_cash", "liquid_equity"]


def test_classification_at_past_as_of_ignores_later_change(
    conn, run_id, record_classification_history
):
    """**look-ahead 排除**: 過去 as_of は変更前の分類を見る(不変原則4)。"""
    inst = _insert_instrument(conn, symbol="H3.T", asset_class="equity", venue="TSE")
    t1 = datetime.now(UTC) - timedelta(days=30)
    t2 = datetime.now(UTC) - timedelta(days=10)
    # 当時に記録された分類(記録時刻 = 知識時点)。
    record_classification_history(
        conn, inst, _tagged("jp_equity_cash"), run_id=run_id, as_of=t1, created_at=t1
    )
    record_classification_history(
        conn, inst, _tagged("jp_equity_cash", "liquid_equity"),
        run_id=run_id, as_of=t2, created_at=t2,
    )

    before = load_classification_at(conn, inst, as_of=t1 - timedelta(days=1))
    assert before is None  # まだ分類されていない時点(= 未分類 → ゲートが block)

    middle = load_classification_at(conn, inst, as_of=t2 - timedelta(days=1))
    assert middle is not None and middle.universe_tags == ("jp_equity_cash",)

    after = load_classification_at(conn, inst, as_of=datetime.now(UTC))
    assert after is not None and "liquid_equity" in after.universe_tags


def test_read_ignores_rows_recorded_after_the_judgment_time(
    conn, run_id, record_classification_history
):
    """**審査 C-16**: 今日追記した行は過去の判断時点からは見えない(bitemporal)。

    追記オンリーは「追記による過去の書き換え」を止めない。時間軸を 2 本にして初めて
    「今日 1 行足すだけで過去のリプレイが変わる」経路が塞がる。
    """
    inst = _insert_instrument(conn, symbol="H5.T", asset_class="equity", venue="TSE")
    t30 = datetime.now(UTC) - timedelta(days=30)
    replay = datetime.now(UTC) - timedelta(days=20)
    record_classification_history(
        conn, inst, _tagged("jp_equity_cash"), run_id=run_id, as_of=t30, created_at=t30
    )
    assert load_classification_at(conn, inst, as_of=replay) == _tagged("jp_equity_cash")

    # 25 日前を主張する行を今日追記する。
    record_classification_history(
        conn, inst, _tagged(), run_id=run_id,
        as_of=datetime.now(UTC) - timedelta(days=25), created_at=datetime.now(UTC),
    )
    assert load_classification_at(conn, inst, as_of=replay) == _tagged("jp_equity_cash")
    # 今日の判断からは見える(記録済みの最新知識)。
    assert load_classification_at(conn, inst, as_of=datetime.now(UTC)) == _tagged()


def test_future_as_of_is_rejected(conn, run_id):
    """未来の知識時点は受け付けない(CHECK as_of <= created_at — 審査 C-16)。"""
    inst = _insert_instrument(conn, symbol="H6.T", asset_class="equity", venue="TSE")
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        upsert_classification(
            conn, inst, _tagged("jp_equity_cash"),
            run_id=run_id, as_of=datetime.now(UTC) + timedelta(days=1),
        )


def test_recorded_at_cannot_be_declared_by_the_writer(conn, run_id):
    """**審査 C-19**: created_at は申告値ではなく DB が刻む(カバレッジの偽装を塞ぐ)。"""
    inst = _insert_instrument(conn, symbol="H7.T", asset_class="equity", venue="TSE")
    old = datetime(2000, 1, 1, tzinfo=UTC)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market.instrument_classification_history
                (instrument_id, universe_tags, instrument_flags, is_single_name,
                 product, unit_size, source, as_of, run_id, created_at)
            VALUES (%s, '{}', '{}', true, 'listed_equity_cash', 100, 'curated', %s, %s, %s)
            RETURNING created_at
            """,
            (inst, old, run_id, old),
        )
        stamped = cur.fetchone()[0]
    assert stamped > datetime.now(UTC) - timedelta(minutes=5)  # 申告した 2000 年ではない


def test_coverage_comes_from_history_data_not_from_schema_migrations(conn, run_id):
    """**審査 C-19**: カバレッジは履歴表のデータ由来で、改竄検知のない表に依存しない。

    ``meta.schema_migrations`` は追記オンリー保護が無く、UPDATE 1 文で「400 日前から
    E6 達成」と偽装できた。履歴表由来なら、同じ UPDATE はカバレッジを動かさない
    (履歴表側の created_at は UPDATE 自体が拒まれる)。
    """
    inst = _insert_instrument(conn, symbol="H8.T", asset_class="equity", venue="TSE")
    upsert_classification(conn, inst, _tagged("jp_equity_cash"), run_id=run_id)
    since = history_coverage_since(conn)
    assert since is not None

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE meta.schema_migrations SET applied_at = now() - interval '400 days'"
        )
    assert history_coverage_since(conn) == since  # 影響を受けない

    _expect_rejected(
        conn,
        "UPDATE market.instrument_classification_history "
        "SET created_at = now() - interval '400 days'",
        psycopg.errors.RaiseException,
    )
    assert history_coverage_since(conn) == since


def test_backdated_correction_is_not_swallowed(conn, run_id):
    """**審査 C-18**: 圧縮の比較対象は「その as_of で有効な行」— 訂正が無音で消えない。

    t1=A / t2=AB のあとに t1.5=AB の訂正を入れると、全体最新(AB)と比べる実装では
    「内容が同じ」として捨てられ、t1.5 の分類は A のまま残っていた。
    """
    inst = _insert_instrument(conn, symbol="H9.T", asset_class="equity", venue="TSE")
    t1 = datetime.now(UTC) - timedelta(days=30)
    t2 = datetime.now(UTC) - timedelta(days=10)
    t15 = datetime.now(UTC) - timedelta(days=20)
    a, ab = _tagged("jp_equity_cash"), _tagged("jp_equity_cash", "liquid_equity")
    upsert_classification(conn, inst, a, run_id=run_id, as_of=t1)
    upsert_classification(conn, inst, ab, run_id=run_id, as_of=t2)

    upsert_classification(conn, inst, ab, run_id=run_id, as_of=t15)
    rows = _history(conn, inst)
    assert len(rows) == 3
    assert [r[1] for r in rows] == [t1, t2, t15]
    # 訂正は記録されている(読出しに現れるのは bitemporal の第2軸を満たしてから)。
    assert rows[2][0] == ["jp_equity_cash", "liquid_equity"]

    # 同じ as_of でその時点の内容と同一なら、やはり追記しない(圧縮は生きている)。
    upsert_classification(conn, inst, ab, run_id=run_id, as_of=t15)
    assert len(_history(conn, inst)) == 3


def test_history_is_append_only(conn, run_id):
    """履歴は UPDATE・DELETE・TRUNCATE のいずれも拒む(0015/0018/0023 と同基準)。"""
    inst = _insert_instrument(conn, symbol="H4.T", asset_class="equity", venue="TSE")
    upsert_classification(conn, inst, _tagged("jp_equity_cash"), run_id=run_id)
    table = "market.instrument_classification_history"
    _expect_rejected(
        conn,
        f"UPDATE {table} SET universe_tags = '{{}}' WHERE instrument_id = {inst}",  # noqa: S608
        psycopg.errors.RaiseException,
    )
    _expect_rejected(
        conn,
        f"DELETE FROM {table} WHERE instrument_id = {inst}",  # noqa: S608
        psycopg.errors.RaiseException,
    )
    _expect_rejected(conn, f"TRUNCATE {table}", psycopg.errors.RaiseException)
    assert len(_history(conn, inst)) == 1


def test_pit_status_uncovered_before_history_starts(conn, run_id):
    """履歴の記録開始より前の as_of は E6 未達のまま(移行前を達成と偽らない)。"""
    inst = _insert_instrument(conn, symbol="HA.T", asset_class="equity", venue="TSE")
    upsert_classification(conn, inst, _tagged("jp_equity_cash"), run_id=run_id)
    since = history_coverage_since(conn)
    assert since is not None, "履歴に記録が1件も無い(0026 が未適用の可能性)"

    covered = classification_pit_status(conn, as_of=since + timedelta(seconds=1))
    assert covered["covered"] is True and covered["note"] is None

    uncovered = classification_pit_status(conn, as_of=since - timedelta(days=1))
    assert uncovered["covered"] is False
    assert "E6" in uncovered["note"] and since.isoformat() in uncovered["note"]


# ── IPS §8.1 資産クラス列(0028)─────────────────────────────────────────────
def _constraint_vocabulary(conn, name: str) -> set[str]:
    """CHECK 制約の定義から許可リテラルを抜き出す(DB 側の語彙の実測)。

    正規表現は ``'([^']+)'::text`` で拡張的に読む(pass5-5 の指摘)。狭い
    ``[a-z_]+`` は「英小文字とアンダースコアだけ」の暗黙前提を持ち、将来 0028 以降で
    語彙が拡張される(ハイフン・数字・大文字)と**検査から漏れる**。三者一致の
    fail-closed を維持するには、DB から抜くのは「クオート内の任意文字列」でよい。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s",
            (name,),
        )
        row = cur.fetchone()
    assert row is not None, f"CHECK 制約 {name} が無い(0028 が未適用の可能性)"
    return set(re.findall(r"'([^']+)'::text", row[0]))


def test_asset_class_vocabulary_is_the_same_in_config_code_and_db(conn, ips):
    """語彙の正は IPS §8.1。config・コード・DB の三者一致をテストで固定する。

    3箇所に書かれている以上、ずれは「いつか起きる」。ずれた瞬間に落ちる場所を作る
    (curated 供給の typo はゲート実行時まで露見しないため、書込時に落としたい)。
    """
    assert set(ips.asset_classes) == set(IPS_ASSET_CLASSES)
    for name in (
        "instrument_classification_asset_class_vocab",
        "classification_history_asset_class_vocab",
    ):
        assert _constraint_vocabulary(conn, name) == set(IPS_ASSET_CLASSES)


def test_asset_class_round_trips_through_current_and_history(conn, run_id):
    """書いた資産クラスが現在値・履歴の両方から読み出せる(ゲート入力の正)。"""
    inst = _insert_instrument(conn, symbol="AC1.T", asset_class="equity", venue="TSE")
    t1 = datetime.now(UTC) - timedelta(days=3)
    upsert_classification(conn, inst, _tagged("jp_equity_cash"), run_id=run_id, as_of=t1)
    current = load_classification(conn, inst)
    assert current is not None and current.asset_class == "equity_jp"
    at = load_classification_at(conn, inst, as_of=datetime.now(UTC))
    assert at is not None and at.asset_class == "equity_jp"


def test_asset_class_change_appends_history(conn, run_id):
    """資産クラスだけが変わった場合も履歴に追記される(圧縮の比較対象に入っている)。"""
    inst = _insert_instrument(conn, symbol="AC2.T", asset_class="equity", venue="TSE")
    t1 = datetime.now(UTC) - timedelta(days=5)
    base = _tagged("jp_equity_cash")
    upsert_classification(conn, inst, base, run_id=run_id, as_of=t1)
    upsert_classification(
        conn, inst, replace(base, asset_class="equity_other"),
        run_id=run_id, as_of=t1 + timedelta(days=1),
    )
    assert len(_history(conn, inst)) == 2


def test_unknown_asset_class_is_rejected_before_the_db(conn, run_id):
    """語彙外は書込時に拒否する(DB の CHECK と二重の防御 — curated の typo 対策)。"""
    inst = _insert_instrument(conn, symbol="AC3.T", asset_class="equity", venue="TSE")
    with pytest.raises(ValueError, match="タクソノミー"):
        upsert_classification(
            conn, inst, replace(_tagged("jp_equity_cash"), asset_class="equity_JP"),
            run_id=run_id,
        )


def test_null_asset_class_is_allowed_and_means_unclassified(conn, run_id):
    """NULL は語彙違反ではなく「分類不能」— 読出し側が候補から落とす(fail-closed)。"""
    inst = _insert_instrument(conn, symbol="AC4.T", asset_class="etf", venue="TSE")
    upsert_classification(
        conn, inst, replace(_tagged("etf"), asset_class=None),
        run_id=run_id, source="curated",
    )
    loaded = load_classification(conn, inst)
    assert loaded is not None and loaded.asset_class is None


def _backfill_section() -> str:
    """0028 のバックフィル区間(マーカー間)を**実物として**取り出す。

    区間には解除するトリガの**名前指定**・DO ブロック・自己検査まで含まれる。文単位で
    抜くのをやめたのは、DO ブロック内の `;` で壊れるうえ、「解除の粒度」「解除の戻し」
    「自己検査」という C-22 の要点がテストの外に出てしまうためである。
    """
    sql = _MIGRATION_0028.read_text(encoding="utf-8")
    start = sql.index("-- >>> BACKFILL BEGIN") + len("-- >>> BACKFILL BEGIN")
    end = sql.index("-- <<< BACKFILL END")
    section = sql[start:end]
    assert "DISABLE TRIGGER instrument_classification_history_no_mutation" in section, (
        "解除は名前指定の1本に限る(審査 C-22)"
    )
    assert "DISABLE TRIGGER USER" not in section, "表の全トリガを落としてはならない"
    return section


def _backfill_note(conn, instrument_id: int) -> list[str | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT asset_class, asset_class_backfill_note "  # noqa: S608
            f"FROM {_HISTORY_TABLE} WHERE instrument_id = %s ORDER BY history_id",
            (instrument_id,),
        )
        return cur.fetchall()


def test_backfill_derives_asset_class_from_the_instrument_master(conn, run_id):
    """**0028 のバックフィル**: 既存行を現行の導出ロジックで埋める(migration の実物を実行)。

    テスト DB は新規作成のため、migration 適用時には埋める対象が無い。導出ロジック自体を
    回帰にするため、asset_class が NULL の行を作ってから **migration ファイルのバックフィル
    区間をそのまま**流す。
    """
    tse = _insert_instrument(conn, symbol="BF1.T", asset_class="equity", venue="TSE")
    us = _insert_instrument(conn, symbol="BF2", asset_class="equity", venue="NASDAQ")
    lse = _insert_instrument(conn, symbol="BF3", asset_class="equity", venue="LSE")
    for inst in (tse, us, lse):
        upsert_classification(
            conn, inst, replace(_tagged("jp_equity_cash"), asset_class=None),
            run_id=run_id, source="curated",
        )
        assert load_classification(conn, inst).asset_class is None

    with conn.cursor() as cur:
        cur.execute(_backfill_section())

    assert load_classification(conn, tse).asset_class == "equity_jp"
    assert load_classification(conn, us).asset_class == "equity_us"
    assert load_classification(conn, lse).asset_class is None  # 分類不能は NULL のまま
    at = load_classification_at(conn, tse, as_of=datetime.now(UTC))
    assert at is not None and at.asset_class == "equity_jp"


def test_backfill_marks_reconstructed_rows(conn, run_id):
    """**審査 C-23**: 再構成した値は行から識別できる(コメントではなくデータで区別)。

    分類できなかった行にも印が付く — 「0028 が見て分類できなかった」と「0028 以後に
    書かれた行」は別物であり、後者だけが note NULL であるべきだからである。
    """
    tse = _insert_instrument(conn, symbol="BN1.T", asset_class="equity", venue="TSE")
    lse = _insert_instrument(conn, symbol="BN2", asset_class="equity", venue="LSE")
    for inst in (tse, lse):
        upsert_classification(
            conn, inst, replace(_tagged("jp_equity_cash"), asset_class=None),
            run_id=run_id, source="curated",
        )
    with conn.cursor() as cur:
        cur.execute(_backfill_section())

    (cls, note), = _backfill_note(conn, tse)
    assert cls == "equity_jp" and note is not None and "0028" in note
    (cls_lse, note_lse), = _backfill_note(conn, lse)
    assert cls_lse is None and note_lse is not None

    # 0028 以後に通常経路で書かれた行には印が付かない(区別が成立している)。
    upsert_classification(
        conn, tse, _tagged("jp_equity_cash", "liquid_equity"),
        run_id=run_id, as_of=datetime.now(UTC),
    )
    assert _backfill_note(conn, tse)[-1] == ("equity_jp", None)


def test_backfill_restores_the_append_only_guard(conn, run_id):
    """**審査 C-22**: 区間を通したあと、当該表の全トリガが有効に戻っている。

    自己検査 DO ブロックが区間内にあるため、戻し忘れは migration 自体の失敗になる
    (この実行が例外なく終わること自体が検査の成功)。ここでは結果も直接確認する。
    """
    inst = _insert_instrument(conn, symbol="BG1.T", asset_class="equity", venue="TSE")
    upsert_classification(
        conn, inst, replace(_tagged("jp_equity_cash"), asset_class=None),
        run_id=run_id, source="curated",
    )
    with conn.cursor() as cur:
        cur.execute(_backfill_section())
        cur.execute(
            "SELECT tgname, tgenabled FROM pg_trigger "
            f"WHERE tgrelid = '{_HISTORY_TABLE}'::regclass AND NOT tgisinternal",  # noqa: S608
        )
        states = dict(cur.fetchall())
    assert states and all(state == "O" for state in states.values()), states
    # ガードが実際に効いている(区間の後で UPDATE が拒まれる)。
    _expect_rejected(
        conn,
        f"UPDATE {_HISTORY_TABLE} SET universe_tags = '{{}}' "  # noqa: S608
        f"WHERE instrument_id = {inst}",
        psycopg.errors.RaiseException,
    )


def test_backfill_is_point_in_time_across_scd2_versions(conn, run_id):
    """**審査の敵対的プローブと同型**: venue が変わった銘柄で当時の資産クラスを再構成する。

    NASDAQ → TSE に上場替えした銘柄では、旧版期間の履歴行は ``equity_us`` でなければ
    ならない。``valid_to IS NULL``(= 現行版)を使う look-ahead 実装ならここが
    ``equity_jp`` になって落ちる — 単一版の銘柄しか使わないテストでは検出できない。
    """
    t_old = datetime.now(UTC) - timedelta(days=60)
    t_new = datetime.now(UTC) - timedelta(days=10)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market.instruments
                (symbol, asset_class, venue, currency, valid_from, valid_to)
            VALUES ('SCD1', 'equity', 'NASDAQ', 'USD', %s, %s)
            RETURNING instrument_id
            """,
            (t_old - timedelta(days=30), t_new),
        )
        inst = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO market.instruments
                (instrument_id, symbol, asset_class, venue, currency, valid_from, valid_to)
            OVERRIDING SYSTEM VALUE
            VALUES (%s, 'SCD1.T', 'equity', 'TSE', 'JPY', %s, NULL)
            """,
            (inst, t_new),
        )
    # 旧版期間と新版期間に1行ずつ、asset_class 未設定の履歴を置く。
    for stamp in (t_old, t_new + timedelta(days=1)):
        upsert_classification(
            conn, inst, replace(_tagged("jp_equity_cash"), asset_class=None),
            run_id=run_id, source=f"curated:{stamp.isoformat()}", as_of=stamp,
        )

    with conn.cursor() as cur:
        cur.execute(_backfill_section())

    rows = _backfill_note(conn, inst)
    assert [r[0] for r in rows] == ["equity_us", "equity_jp"], rows
    assert all(r[1] is not None for r in rows)
    # 履歴行を直接見るのは意図的である。``load_classification_at`` は bitemporal
    # (created_at < 判断時点の当日終端)のため、今このテストで書いた行は 60 日前の
    # 判断時点からは**見えないのが正しい** — 再構成の正しさはここでは行の値で見る。


# ── curated ユニバース(流動性タグの供給)─────────────────────────────────────
def _write_universe(tmp_path, entries, **overrides) -> Path:
    """テスト用の curated ユニバース YAML を書き出す(同一パスを上書きする)。

    ``content_sha256`` は既定で実内容から計算する(``overrides`` で壊せる)。
    """
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
        "content_sha256", curated_content_digest(str(doc["criterion"]), list(entries))
    )
    path = tmp_path / "universe.yaml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return path


def _entry(symbol: str, tags=("liquid_equity",), rationale="日経225 構成") -> dict:
    return {"symbol": symbol, "tags": list(tags), "rationale": rationale}


def test_loader_requires_rationale(tmp_path):
    """根拠の無い銘柄は読み込み時に拒否する(1行足すだけで売買母集団が広がらない)。"""
    path = _write_universe(tmp_path, [{"symbol": "7203.T", "tags": ["liquid_equity"]}])
    with pytest.raises(CuratedUniverseError, match="rationale"):
        load_curated_universe(path)


def test_loader_requires_approval(tmp_path):
    """未承認(approved_at 空)のリストは反映経路に入れない。"""
    path = _write_universe(tmp_path, [_entry("7203.T")], approved_at=None)
    with pytest.raises(CuratedUniverseError, match="未承認"):
        load_curated_universe(path)


def test_loader_rejects_freeform_approval_date(tmp_path):
    """**審査 C-24**: approved_at は ISO 日付。「いつか」は承認日ではない。"""
    path = _write_universe(tmp_path, [_entry("7203.T")], approved_at="いつか")
    with pytest.raises(CuratedUniverseError, match="ISO 日付"):
        load_curated_universe(path)


def test_loader_rejects_self_declared_approver(tmp_path):
    """**審査 C-24**: 承認できるのは代表のみ(起草者が自分で名乗れない)。"""
    path = _write_universe(tmp_path, [_entry("7203.T")], approved_by="dev-lead")
    with pytest.raises(CuratedUniverseError, match="representative"):
        load_curated_universe(path)


def test_loader_detects_content_swap_under_the_same_version(tmp_path):
    """**審査 C-25**: 同じ v1 のまま中身を差し替えると読み込みが失敗する。"""
    original = [_entry("7203.T")]
    path = _write_universe(tmp_path, original)
    stale = yaml.safe_load(path.read_text(encoding="utf-8"))["content_sha256"]
    swapped = _write_universe(
        tmp_path, [_entry("9984.T")], content_sha256=stale
    )
    with pytest.raises(CuratedUniverseError, match="content_sha256"):
        load_curated_universe(swapped)


def test_source_carries_the_content_hash(tmp_path):
    """出所に内容ハッシュが入る(適用済みリストを監査で一意に復元できる)。"""
    universe = load_curated_universe(_write_universe(tmp_path, [_entry("7203.T")]))
    assert universe.source.startswith("curated:test-liquid:v1:")
    assert universe.source.endswith(universe.content_sha256[:12])


def test_content_digest_ignores_key_order_and_tag_order(tmp_path):
    """ハッシュは内容に依存し、YAML の書式には依存しない(無用な再承認を強いない)。"""
    a = curated_content_digest("基準", [{"symbol": "A", "tags": ["x", "y"], "rationale": "r"}])
    b = curated_content_digest("基準", [{"rationale": "r", "tags": ["y", "x"], "symbol": "A"}])
    assert a == b
    assert a != curated_content_digest(
        "別の基準", [{"symbol": "A", "tags": ["x", "y"], "rationale": "r"}]
    )
    assert a != curated_content_digest(
        "基準", [{"symbol": "A", "tags": ["x", "y"], "rationale": "別の根拠"}]
    )


def test_loader_rejects_tags_outside_manages(tmp_path):
    """manages_tags の外のタグは付与しない(撤回できないタグを作らない)。"""
    path = _write_universe(tmp_path, [_entry("7203.T", tags=("etf",))])
    with pytest.raises(CuratedUniverseError, match="manages_tags"):
        load_curated_universe(path)


def test_loader_rejects_duplicate_symbols(tmp_path):
    path = _write_universe(tmp_path, [_entry("7203.T"), _entry("7203.T")])
    with pytest.raises(CuratedUniverseError, match="重複"):
        load_curated_universe(path)


def test_shipped_jim_universe_is_wellformed_and_approved():
    """同梱の jim-curated.yaml は代表承認済みとして読み込める(2026-08-04 承認)。

    未承認時代の前身テストは「読み込み拒否」を固定していた(fail-closed の要)。
    代表の明示承認(Discord 2026-08-04「売買候補は承認」・PR #99)により状態が変わった
    ため、本テストは「承認済みの版が正しく読め、内容ハッシュ・承認記録が有効である」
    ことを固定する。承認を取り消す場合は approved_at/approved_by を null に戻し、
    このテストを前身の形に戻すこと(取消もコード変更として残る)。
    """
    path = _REPO_ROOT / "config" / "universe" / "jim-curated.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["manages_tags"] == ["liquid_equity"]
    assert all(e.get("rationale") for e in raw["entries"])
    assert all(e["tags"] == ["liquid_equity"] for e in raw["entries"])
    assert len({e["symbol"] for e in raw["entries"]}) == len(raw["entries"])
    # 宣言ハッシュは実内容と一致している(承認時にこの版が固定される — 審査 C-25)。
    assert raw["content_sha256"] == curated_content_digest(
        str(raw["criterion"]), list(raw["entries"])
    )
    universe = load_curated_universe(path)
    assert len(universe.entries) == 35
    assert str(raw["approved_by"]) == "representative"
    assert str(raw["approved_at"]) == "2026-08-04"


def test_universe_config_is_a_protected_area():
    """**審査 C-24**: config/universe/** が保護領域に登録されている(A-18-1 の突合対象)。

    承認の正はファイル内の approved_by ではなく `Approved:` トレーラ側にある。登録が
    外れると「銘柄を足すこと」と「承認済みと書くこと」を無トレーラで同時に行える。
    """
    governance = yaml.safe_load(
        (_REPO_ROOT / "config" / "governance.yaml").read_text(encoding="utf-8")
    )
    entries = {p["path"]: p["area"] for p in governance["protected_areas"]}
    assert entries.get("config/universe/**") == "mandates"


def test_curated_tag_is_granted_and_recorded_in_history(conn, run_id, tmp_path):
    """タグ付与は現在値と履歴の両方に載る(PIT — いつから liquid_equity かが再現できる)。"""
    inst = _insert_instrument(conn, symbol="CU1.T", asset_class="equity", venue="TSE")
    # 土台のルール分類は1時間前に確定していたことにする(curated 付与はその後)。
    upsert_classification(
        conn, inst, _tagged("jp_equity_cash"),
        run_id=run_id, as_of=datetime.now(UTC) - timedelta(hours=1),
    )
    before = datetime.now(UTC) - timedelta(minutes=30)

    universe = load_curated_universe(_write_universe(tmp_path, [_entry("CU1.T")]))
    result = apply_curated_universe(conn, universe, run_id=run_id)
    assert result["granted"] == 1 and result["unresolved"] == []

    current = load_classification(conn, inst)
    assert current is not None
    assert set(current.universe_tags) == {"jp_equity_cash", "liquid_equity"}
    assert current.asset_class == "equity_jp"  # 土台の分類は失われない

    # 付与前の as_of では付いていない(今日のタグが過去に漏れない — 不変原則4)。
    past = load_classification_at(conn, inst, as_of=before)
    assert past is not None and "liquid_equity" not in past.universe_tags
    assert any(
        "liquid_equity" in row[0] and row[2].startswith("curated:test-liquid")
        for row in _history(conn, inst)
    )


def test_curated_apply_is_idempotent(conn, run_id, tmp_path):
    """再実行で履歴を膨らませない(タグ集合が変わらなければ unchanged)。"""
    inst = _insert_instrument(conn, symbol="CU2.T", asset_class="equity", venue="TSE")
    classify_current_instruments(conn, run_id=run_id)
    universe = load_curated_universe(_write_universe(tmp_path, [_entry("CU2.T")]))
    apply_curated_universe(conn, universe, run_id=run_id)
    n = len(_history(conn, inst))
    second = apply_curated_universe(conn, universe, run_id=run_id)
    assert second["granted"] == 0 and second["unchanged"] == 1
    assert len(_history(conn, inst)) == n


def test_curated_tag_is_revoked_when_dropped_from_config(conn, run_id, tmp_path):
    """config から外れた銘柄はタグを失う(付与だけでは「config が正」にならない)。"""
    inst = _insert_instrument(conn, symbol="CU3.T", asset_class="equity", venue="TSE")
    classify_current_instruments(conn, run_id=run_id)
    granted = load_curated_universe(_write_universe(tmp_path, [_entry("CU3.T")]))
    apply_curated_universe(conn, granted, run_id=run_id)
    assert "liquid_equity" in load_classification(conn, inst).universe_tags

    dropped = load_curated_universe(_write_universe(tmp_path, [_entry("CU9.T")]))
    result = apply_curated_universe(conn, dropped, run_id=run_id)
    assert result["revoked"] == 1
    remaining = load_classification(conn, inst)
    assert "liquid_equity" not in remaining.universe_tags
    assert "jp_equity_cash" in remaining.universe_tags  # 土台は残る


def test_curated_unresolved_symbols_are_reported_not_raised(conn, run_id, tmp_path):
    """銘柄マスタに無い symbol はエラーにせず件数で返す(綴り間違いを黙って飲まない)。"""
    universe = load_curated_universe(_write_universe(tmp_path, [_entry("NOPE.T")]))
    result = apply_curated_universe(conn, universe, run_id=run_id)
    assert result["unresolved"] == ["NOPE.T"] and result["granted"] == 0


def test_curated_skips_instruments_without_a_base_classification(conn, run_id, tmp_path):
    """ルール分類も既存分類も無い銘柄はタグだけの行を作らない(fail-closed)。"""
    _insert_instrument(conn, symbol="CU4", asset_class="etf", venue="TSE")
    universe = load_curated_universe(_write_universe(tmp_path, [_entry("CU4")]))
    result = apply_curated_universe(conn, universe, run_id=run_id)
    assert result["unclassifiable"] == ["CU4"] and result["granted"] == 0
