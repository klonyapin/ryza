"""tagger（銘柄タグ）の単体・DB テスト。"""

from __future__ import annotations

from ryza.preprocess.tagger import InstrumentDict, build_dictionary, tag


def test_tag_empty_on_empty_dict():
    assert tag("トヨタ 7203", InstrumentDict()).instrument_ids == []


def test_tag_by_code_boundary():
    d = InstrumentDict(by_code={"7203": 42})
    r = tag("トヨタ自動車（7203）は業績予想を修正", d)
    assert r.instrument_ids == [42]
    assert r.matched[0]["kind"] == "code"
    # 5 桁以上の数字は 4 桁コードとして誤検出しない。
    assert tag("受付番号 72031 の件", d).instrument_ids == []


def test_tag_by_symbol_and_name():
    d = InstrumentDict(by_symbol={"AAPL": 7}, by_name={"アップル": 7})
    assert tag("AAPL decline", d).instrument_ids == [7]
    assert tag("アップルの新製品", d).instrument_ids == [7]


def test_tag_multiple_instruments_deduped():
    d = InstrumentDict(by_code={"7203": 1, "6758": 2})
    r = tag("7203 と 6758 と再び 7203 に言及", d)
    assert r.instrument_ids == [1, 2]  # 昇順・重複排除


def test_build_dictionary_from_instruments(conn, make_instrument):
    iid_toyota = make_instrument("7203.T")
    iid_apple = make_instrument("AAPL", venue="NASDAQ", currency="USD")
    d = build_dictionary(conn)
    assert d.by_code.get("7203") == iid_toyota
    assert d.by_symbol.get("7203.T") == iid_toyota
    assert d.by_symbol.get("AAPL") == iid_apple
    # 米株は数値コードを持たない。
    assert "AAPL" not in d.by_code


def test_build_dictionary_with_name_map(conn, make_instrument):
    iid = make_instrument("7203.T")
    d = build_dictionary(conn, name_map={"トヨタ自動車": "7203.T", "存在しない": "9999.T"})
    assert d.by_name.get("トヨタ自動車") == iid
    # instruments に無い symbol の社名は無視される。
    assert "存在しない" not in d.by_name
    r = tag("トヨタ自動車が上方修正", d)
    assert iid in r.instrument_ids
