"""dashboard/app.py のヘッドレス描画テスト(T-018)。

Streamlit の ``AppTest`` で全ページを実際に描画し、**例外ゼロ**を確認する。従来
「UI はテスト対象外」としていたが、可視化再設計でページ実装が ``viz.py`` ヘルパと
新規クエリに依存するようになり、描画時にしか出ない型・キー名の不整合(``metrics``
jsonb のキー、``st.progress`` の値域など)を CI で捕まえる必要が生じた。

**DB 隔離**: ``queries.connect_readonly`` / ``connect_boardroom`` をテストの
トランザクション内接続に差し替える(``st.cache_resource`` はテストごとにクリア)。
これによりアプリは commit された実データを見ず、投入したテストデータは rollback で
消える。**GitHub REST** は「計画」ページが叩くためスタブ化する(CI をネットワークに
依存させない)。

**ページの指名方法**(2026-08-03 の ``st.navigation`` 移行に伴う変更): 旧実装は
サイドバーの ``st.radio`` を ``set_value`` して切り替えていたが、``st.navigation`` の
ページリンクはウィジェットではなく専用の ForwardMsg なので AppTest から操作できない。
``AppTest.switch_page`` もファイルベースのページ専用で、callable ページには使えない。
そこで Streamlit 内部と同じ経路 —— ページのハッシュは ``calc_hash(url_path)`` であり、
``AppTest._page_hash`` がリラン要求の ``page_script_hash`` になる —— を使って指名する。
``_page_hash`` は private だが ``AppTest.switch_page`` 自身が同じ属性を書き換えており、
これ以外にページを選ぶ手段が無い。
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

pytest.importorskip("streamlit", reason="streamlit 未導入(.[dashboard] を入れると走る)")

import dads  # noqa: E402
import github_api  # noqa: E402
import queries  # noqa: E402
import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402
from streamlit.util import calc_hash  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
_APP = _ROOT / "dashboard" / "app.py"

#: ナビゲーションの宣言(セクション → [(タイトル, url_path, ページ見出し)])。
#: ``dashboard/app.py`` の ``NAV_SECTIONS`` と一致していることを
#: :func:`test_navigation_sections_match_the_declared_structure` が突き合わせる
#: (app.py は import すると ``main()`` が走ってしまうため AST で読む)。
#: ``url_path`` はページの同一性そのもの(ハッシュの導出元)なので、タイトルを
#: 変えても url_path は変えないこと。
NAV_SECTIONS: dict[str, list[tuple[str, str, str]]] = {
    "監視": [
        ("概況", "overview", "概況"),
        ("ジョブ", "jobs", "ジョブ"),
        ("取込", "ingest", "取込"),
        ("報道", "press", "報道(press.outbox)"),
        ("市場観", "market-view", "市場観(docs.market_view)"),
    ],
    "成績・リスク": [
        ("成績", "performance", "成績"),
        ("リスク", "risk", "リスク"),
        ("コスト", "cost", "コスト(meta.runs.cost)"),
    ],
    "組織・統治": [
        ("組織", "org", "組織"),
        ("規則", "rules", "規則(定款の機械可読版 config/governance.yaml)"),
        ("承認・通知", "approvals", "承認・通知"),
        ("役員室", "boardroom", "役員室"),
    ],
    "開発": [
        ("計画", "plan", "計画"),
        ("開発ステータス", "dev-status", "開発・運用ステータス"),
    ],
}

#: 全ページの ``url_path``(パラメータ化テスト用)。
ALL_PAGES = [url_path for items in NAV_SECTIONS.values() for _, url_path, _ in items]

#: url_path → 期待するページ見出し。
PAGE_HEADERS = {
    url_path: header for items in NAV_SECTIONS.values() for _, url_path, header in items
}

_NOW = datetime.now(UTC)


# ── テストデータ(すべて呼び出し側のトランザクション内。rollback で消える)──────
def _metrics(*, sufficient: bool) -> dict:
    """リスクエンジンの測定値スナップショット(ryza.risk.state.state_metrics 相当)。"""
    return {
        "drawdown": "0.25",
        "n_returns": 20 if sufficient else 3,
        "sufficient": sufficient,
        "ewma_vol_annual": 0.11,
        "es95_adopted": 0.012,
        "nav": "900000",
        "peak_nav": "1200000",
        "notes": ["価格欠測: 9999"],
    }


def _seed(conn, run, *, sufficient: bool = True) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.trading_state (state, reason, updated_by)
            VALUES ('normal', NULL, 'test:app')
            ON CONFLICT (singleton) DO UPDATE
                SET state = EXCLUDED.state, reason = EXCLUDED.reason,
                    updated_by = EXCLUDED.updated_by, updated_at = now()
            """
        )
        # NAV 系列: 100万 → 120万 → 90万(設定来ピーク比 -25% の水没を作る)。
        for i, nav in enumerate((1_000_000, 1_200_000, 900_000)):
            cur.execute(
                """
                INSERT INTO ledger.nav_snapshots (book_id, snap_date, nav, status, detail)
                VALUES ('DEMO_FUND', %s, %s, 'confirmed', '{}'::jsonb)
                ON CONFLICT (book_id, snap_date) DO UPDATE SET nav = EXCLUDED.nav
                """,
                (date(2026, 8, 1) + timedelta(days=i), nav),
            )
        cur.execute(
            """
            INSERT INTO risk.limits_state
                (book_id, dd_soft, dd_hard, vol_exceeded, es_exceeded, as_of, run_id)
            VALUES ('DEMO_FUND', true, false, false, false, %s, %s)
            ON CONFLICT (book_id) DO UPDATE
                SET dd_soft = EXCLUDED.dd_soft, as_of = EXCLUDED.as_of
            """,
            (_NOW, run.run_id),
        )
        cur.execute(
            """
            INSERT INTO risk.limits_state_events
                (book_id, event, dd_soft, dd_hard, vol_exceeded, es_exceeded,
                 metrics, actor, as_of, run_id)
            VALUES ('DEMO_FUND', 'engine_update', true, false, false, false,
                    %s, 'risk.daily', %s, %s)
            """,
            (Jsonb(_metrics(sufficient=sufficient)), _NOW, run.run_id),
        )
        cur.execute(
            """
            INSERT INTO meta.runs
                (job_name, code_version, started_at, finished_at, status, params, cost)
            VALUES ('jobs.daily', 'test', %s, %s, 'success', '{}'::jsonb, %s)
            """,
            (
                _NOW - timedelta(minutes=12),
                _NOW,
                Jsonb(
                    {
                        "total_tokens": 1500,
                        "total_cost_estimate": 0.9,
                        "by_tier": {"mid": {"calls": 2, "tokens": 1500, "cost_estimate": 0.9}},
                    }
                ),
            ),
        )
        cur.execute(
            """
            INSERT INTO press.outbox (channel, embed_json, urgent, run_id)
            VALUES ('ops', %s, false, %s)
            """,
            (
                Jsonb(
                    {
                        "title": "日次サイクル 2026-08-03 07:00 JST",
                        "fields": [
                            {"name": "ingest", "value": "✅ ok=2", "inline": False},
                            {"name": "morning", "value": "⚠️ 失敗: RuntimeError", "inline": False},
                        ],
                    }
                ),
                run.run_id,
            ),
        )
        cur.execute(
            """
            INSERT INTO press.outbox (channel, embed_json, urgent, run_id)
            VALUES ('approval', %s, false, %s)
            """,
            (Jsonb({"title": "承認依頼: T-018"}), run.run_id),
        )


def _app_factory(conn, monkeypatch):
    monkeypatch.setattr(queries, "connect_readonly", lambda: conn)
    monkeypatch.setattr(queries, "connect_boardroom", lambda: conn)
    for name in (
        "fetch_open_pulls",
        "fetch_open_issues",
        "fetch_merged_pulls",
        "fetch_closed_issues",
    ):
        monkeypatch.setattr(github_api, name, lambda: [])
    monkeypatch.setattr(github_api, "fetch_ci_state", lambda sha: "none")
    st.cache_data.clear()
    st.cache_resource.clear()

    def _open(page: str | None = None) -> AppTest:
        """``url_path`` でページを指名して描画する(``page=None`` は既定ページ)。"""
        # cache_data(ttl=60) はページ間で持ち越さない — テストは毎回実データを見る。
        st.cache_data.clear()
        at = AppTest.from_file(str(_APP), default_timeout=120)
        if page is not None:
            # ページのハッシュは url_path から導出される(StreamlitPage._script_hash)。
            at._page_hash = calc_hash(page)
        at.run()
        assert not at.exception, f"{page or '(既定)'} の描画で例外: {at.exception}"
        return at

    return _open


@pytest.fixture
def app(conn, run, monkeypatch):
    """テストトランザクションに束縛した AppTest ファクトリ(観測数は十分)。"""
    _seed(conn, run, sufficient=True)
    yield _app_factory(conn, monkeypatch)
    st.cache_data.clear()
    st.cache_resource.clear()


@pytest.fixture
def app_insufficient(conn, run, monkeypatch):
    """観測不足(engine の sufficient=False)の系統。vol/ES 判定が無効な経路。"""
    _seed(conn, run, sufficient=False)
    yield _app_factory(conn, monkeypatch)
    st.cache_data.clear()
    st.cache_resource.clear()




def _texts(at: AppTest) -> str:
    """描画された markdown/caption/info/warning の全文(存在アサーション用)。"""
    parts = [el.value for el in at.markdown]
    parts += [el.value for el in at.info] + [el.value for el in at.warning]
    parts += [el.value for el in at.error] + [el.value for el in at.success]
    parts += [el.value for el in at.header] + [el.value for el in at.subheader]
    return "\n".join(str(p) for p in parts)


# ── 全ページのヘッドレス描画(例外ゼロ)─────────────────────────────────────────
@pytest.mark.parametrize("page", ALL_PAGES)
def test_every_page_renders_without_exception(app, page):
    at = app(page)
    assert not at.exception


@pytest.mark.parametrize("page", ALL_PAGES)
def test_every_page_is_reachable_by_its_url_path(app, page):
    """ナビ再構成後も全 14 ページに到達できること(受け入れ基準)。

    ``st.navigation`` は指名されたハッシュが未登録なら**黙って既定ページへ落とす**。
    「例外が出ない」だけでは url_path のタイプミスを検出できないため、そのページ
    固有の見出しが出ていることまで確認する。
    """
    assert [h.value for h in app(page).header] == [PAGE_HEADERS[page]]


@pytest.mark.parametrize("page", ALL_PAGES)
def test_every_page_receives_the_dads_css_block(app, page):
    """DADS の CSS 層(44px タップターゲット・行間・フォーカスリング)が全ページに届く。

    **限界を明示する**: 確認できるのは「CSS ブロックが送出されている」ことだけで、
    実際に 44px で描画されているかは検査できない(AppTest は DOM もレンダラも持た
    ない)。Streamlit の更新でセレクタが一致しなくなってもこのテストは通る —
    実寸は人間が実ブラウザで見るしかない。根拠と割り切りは dashboard/dads.py。

    **この限界は机上の話ではなく実際に踏んだ**: 2026-08-03 の実ブラウザ検証で、
    このテストが緑のまま CSS がブラウザに 1 バイトも届いていないことが判明した
    (原因は下の ``test_dads_css_is_injected_inside_the_page_not_the_entrypoint``
    と ``dads.inject`` の docstring)。「送出されている」と「効いている」の差を、
    テストの緑では埋められない。
    """
    blocks = [str(el.value) for el in app(page).markdown]
    assert sum(dads.CSS_MARKER in b for b in blocks) == 1, page
    assert any(f"min-height: {dads.TARGET_SIZE_PX}px" in b for b in blocks), page


def test_dads_css_is_injected_inside_the_page_not_the_entrypoint():
    """CSS 注入が**ページ側**で呼ばれていること(``main()`` では効かない)。

    ``st.navigation`` の ``page.run()`` はメインコンテナをリセットするため、
    ``main()`` が ``page.run()`` より前に書いた要素はブラウザに届かない。当初の実装は
    まさにそれで、AppTest は緑・実ブラウザではタップターゲット 28px のままだった。

    実効性そのものは AppTest で検査できないので、**その原因になった構造**を固定する。
    ソースを AST で読み、``dads.inject()`` の呼び出しが ``_with_dads_css``(全ページに
    被せるラッパ)の中だけにあり、``main()`` の中には無いことを確認する。
    """
    tree = ast.parse(_APP.read_text(encoding="utf-8"))
    callers = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "inject"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "dads"
            ):
                callers.setdefault(node.name, 0)
                callers[node.name] += 1
    # ``ast.walk`` は入れ子の関数も辿るので、``_with_dads_css`` とその内側の ``_run``
    # の両方が挙がる(同じ1つの呼び出し)。数えたいのは「どの関数の中にあるか」。
    assert "main" not in callers, "main() で注入しても page.run() のリセットで消える"
    assert "_with_dads_css" in callers, callers
    assert set(callers) == {"_with_dads_css", "_run"}, callers


def test_dads_css_is_not_sent_through_st_html():
    """style だけの ``st.html`` は**イベントコンテナ**へ送られ、本アプリでは表示されない。

    Streamlit の仕様(``st.html`` の docstring): 内容が style タグだけの場合、場所を
    取らないようイベントコンテナへ送る。``st.navigation`` 構成ではそのコンテナが DOM に
    現れず、CSS が一切適用されなかった。``st.markdown(unsafe_allow_html=True)`` は
    通常のコンテナへ出すため実測で効くことを確認済み。
    """
    source = (_ROOT / "dashboard" / "dads.py").read_text(encoding="utf-8")
    body = source.split("def inject", 1)[1].split("def ", 1)[0]
    assert "st.markdown(_CSS, unsafe_allow_html=True)" in body
    assert "st.html(" not in body


def _rendered_css(at) -> str:
    """描画結果に含まれる CSS 全文(st.html + unsafe_allow_html の markdown)。"""
    parts = [str(el.body) for el in at.get("html")]
    parts += [str(el.value) for el in at.markdown]
    return "\n".join(p for p in parts if "font-size" in p or "<style" in p)


@pytest.mark.parametrize("page", ALL_PAGES)
def test_rendered_css_has_no_font_size_below_the_dads_minimum(app, page):
    """重要-2: **実際に描画された** CSS の font-size が全て 14px 以上であること。

    ソース走査(``test_dads_theme``)は ``font-size:{dads.MIN_FONT_REM}rem`` のような
    補間を見られない —— 数字がソースに現れないため、是正した 9 箇所がまさに検査から
    漏れる。宣言と実装の食い違いを潰すのが目的なのに、是正後の値だけ素通りするのでは
    本末転倒なので、ブラウザへ送られる文字列そのものを見る。

    ``rem`` は ``baseFontSize = 16`` に対する倍率、``em`` は親要素に対する倍率で、
    親が既定サイズなら同じく 16px 基準になる。
    """
    sizes = re.findall(r"font-size:\s*([0-9.]+)(rem|em|px)", _rendered_css(app(page)))
    for value, unit in sizes:
        px = float(value) * (1 if unit == "px" else 16)
        assert px >= 14, f"{page}: {value}{unit} = {px}px"


def test_rendered_css_scan_actually_sees_the_org_chart_declarations():
    """走査が空振りしていないこと。組織ページは font-size 宣言が最も多い画面。"""
    assert dads.MIN_FONT_REM == 0.875  # 0.875rem = 14px


@pytest.mark.parametrize("page", ["org", "overview"])
def test_rendered_css_is_not_vacuous(app, page):
    css = _rendered_css(app(page))
    sizes = re.findall(r"font-size:\s*([0-9.]+)(rem|em|px)", css)
    assert sizes, f"{page}: font-size 宣言を1つも拾えていない(走査が壊れている)"
    # 是正した値(0.875rem)が実際に出ていること。
    assert any(v == "0.875" for v, _ in sizes), sizes


def test_navigation_sections_match_the_declared_structure():
    """``app.py`` の ``NAV_SECTIONS`` と本ファイルの宣言が一致していること。

    app.py は import すると ``main()`` が走る(Streamlit のエントリポイント)ため、
    AST で ``NAV_SECTIONS`` の定義だけを読み取って突き合わせる。ページを増減した
    ときにテスト側の宣言を直し忘れると、上の到達性テストが素通りしてしまう。
    """
    tree = ast.parse(_APP.read_text(encoding="utf-8"))
    node = next(
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.AnnAssign)
        and isinstance(n.target, ast.Name)
        and n.target.id == "NAV_SECTIONS"
    )
    actual = {
        ast.literal_eval(key): [
            (ast.literal_eval(item.elts[0]), ast.literal_eval(item.elts[1]))
            for item in value.elts
        ]
        for key, value in zip(node.keys, node.values, strict=True)
    }
    expected = {
        section: [(title, url_path) for title, url_path, _ in items]
        for section, items in NAV_SECTIONS.items()
    }
    assert actual == expected


def test_navigation_url_paths_are_unique():
    """url_path はページのハッシュそのもの。重複すると片方が到達不能になる。"""
    assert len(ALL_PAGES) == len(set(ALL_PAGES)) == 14


def test_every_page_declares_the_question_it_answers(app):
    """Say 原則: 各ページ冒頭に「このページで答えられる問い」が 1 行ある。"""
    for page in ALL_PAGES:
        at = app(page)
        captions = [str(c.value) for c in at.caption]
        assert any(c.startswith("このページで答えられる問い:") for c in captions), page


# ── 概況: 6 ブロック固定の監視面 ───────────────────────────────────────────────
def test_overview_has_six_fixed_blocks(app):
    text = _texts(app("overview"))
    for block in (
        "① 取引状態",
        "② NAV",
        "③ DD 使用率",
        "④ 直近の日次サイクル",
        "⑤ 未処理の承認・通知",
        "⑥ LLM コスト予算消化",
    ):
        assert block in text, block


def test_overview_nav_carries_comparison_context(app):
    text = _texts(app("overview"))
    assert "前日比" in text and "設定来" in text


def test_overview_dd_bullet_shows_usage_against_limit(app):
    at = app("overview")
    # DD 0.25 / dd_hard 0.25 → 使用率 100%・リミット到達で赤。
    bars = [b.proto.text for b in at.get("progress")]
    assert any("使用率 100%" in b and b.startswith(":red[") for b in bars), bars


def test_overview_names_the_active_latches(app):
    """重要-4: DD 使用率だけでなく、いま発注を止めているラッチを概況で名指しする。"""
    text = _texts(app("overview"))
    assert "リスクフラグ" in text
    assert "作動中: dd_soft" in text  # fixture は dd_soft のみ true


def test_overview_cost_uses_calendar_month_and_shows_recorded_share(app):
    """中-9: 予算(月次)と分子(当月)の期間を揃え、コスト記録率を添える。"""
    text = _texts(app("overview"))
    assert "⑥ LLM コスト予算消化(当月)" in text
    assert "コスト記録のある実行" in text


def test_overview_has_no_raw_runs_table(app):
    """meta.runs の 30 行テーブルは「ジョブ」へ移した(概況は一画面に収める)。"""
    assert len(app("overview").dataframe) == 0
    assert len(app("jobs").dataframe) > 0


# ── 成績 ──────────────────────────────────────────────────────────────────────
def test_performance_shows_nav_line_and_underwater(app):
    at = app("performance")
    text = _texts(at)
    assert "NAV 推移" in text
    assert "アンダーウォーター(設定来ピーク比の下落率)" in text
    # ライン(NAV)の直下にエリア(underwater)。どちらも vega-lite で出るため
    # spec の mark 種別で見分ける。
    marks = [c.proto.spec for c in at.get("vega_lite_chart")]
    assert len(marks) == 2
    assert '"line"' in marks[0]
    assert '"area"' in marks[1]


def test_performance_declares_missing_benchmark_instead_of_faking_it(app):
    """無い比較(等配分 BH 対照)を推定値で埋めず、明示行として出す。"""
    frame = app("performance").dataframe[0].value
    column = list(frame["対照(等配分 buy-and-hold)"])
    assert set(column[:3]) == {"未実装"}
    assert column[-1] == "対照系列は未実装(T-019 候補)"


def test_performance_period_return_table_has_all_periods(app):
    frame = app("performance").dataframe[0].value
    assert list(frame["期間"])[:3] == ["1W(7日)", "1M(30日)", "設定来"]


def test_performance_table_declares_window_base_day(app):
    """重大-1/2: 起点日を併記し、窓を満たさない期間は値を出さない。

    fixture の NAV は 8/1〜8/3 の 3 日分。1W(7日)・1M(30日)は cutoff 以前の
    スナップショットが無いため「期間未充足」で値なし、設定来のみ起点 8/1 で有効。
    """
    frame = app("performance").dataframe[0].value
    rows = frame.set_index("期間")
    assert rows.loc["1W(7日)", "リターン"] == "—"
    assert rows.loc["1W(7日)", "起点日"] == "—"
    assert "期間未充足" in rows.loc["1M(30日)", "注記"]
    assert rows.loc["設定来", "起点日"] == "2026-08-01"
    assert rows.loc["設定来", "リターン"].startswith("-")  # 100万 → 90万


def test_performance_flags_external_flow_days_under_the_chart(app):
    """重要-6: 出資・払戻の日を underwater 図の直下で名指しする。

    フローは fixture ではなくマイグレーション由来(0006 の初期出資 8/2・0011 の
    増資 8/3)。NAV スナップショットの日付をそれに合わせてある。
    """
    captions = [str(c.value) for c in app("performance").caption]
    flow_note = next((c for c in captions if "外部フロー発生日" in c), None)
    assert flow_note is not None, captions
    assert "2026-08-02 出資" in flow_note and "2026-08-03 出資" in flow_note
    assert "損益ではない" in flow_note


# ── リスク ────────────────────────────────────────────────────────────────────
def test_risk_lists_bullets_sorted_by_usage(app):
    at = app("risk")
    usages = [b.proto.value for b in at.get("progress")]
    assert len(usages) == 4  # dd_soft / dd_hard / 実現ボラ / ES95
    assert usages == sorted(usages, reverse=True)


def test_risk_shows_latch_state_as_text(app):
    text = _texts(app("risk"))
    assert "DD ソフト(新規建て枠半減)" in text and "作動中" in text
    assert "DD ハード(全新規発注停止・復帰は委員会のみ)" in text and "未作動" in text


def test_risk_bullets_carry_limits_from_ips(app):
    bars = [b.proto.text for b in app("risk").get("progress")]
    joined = "\n".join(bars)
    assert "上限 25.0%" in joined  # dd_hard_limit
    assert "上限 15.0%" in joined  # dd_soft_limit / realized_vol_limit
    assert "上限 3.0%" in joined  # daily_es95_nav_max


def test_risk_vol_and_es_are_unknown_when_observations_insufficient(app_insufficient):
    """重大-3: エンジンが判定を保留している間、vol/ES に赤 breach を出さない。

    ラッチが「未作動」なのに使用率が超過を示す、同一画面での矛盾を潰す。
    """
    bars = [b.proto.text for b in app_insufficient("risk").get("progress")]
    unknown = [b for b in bars if b.startswith(":gray[")]
    assert len(unknown) == 2, bars  # 実現ボラ / ES95
    assert all("観測不足で判定無効(n=3/20)" in b for b in unknown)
    assert not any("実現ボラ" in b and b.startswith(":red[") for b in bars)
    # DD は 1 日目から有効なので観測不足でも出す。
    assert any("DD(対 dd_hard)" in b and b.startswith(":red[") for b in bars)


def test_risk_unknown_bullets_are_listed_first(app_insufficient):
    """低-12: 測れていないリミットは「安全」ではないので最下段に沈めない。"""
    bars = [b.proto.text for b in app_insufficient("risk").get("progress")]
    assert bars[0].startswith(":gray[") and bars[1].startswith(":gray[")


def test_risk_vol_and_es_are_measured_when_sufficient(app):
    bars = [b.proto.text for b in app("risk").get("progress")]
    assert not any(b.startswith(":gray[") for b in bars)
    assert any("実現ボラ(EWMA 年率)" in b and "11.0%" in b for b in bars), bars


# ── コスト: 累計 vanity を出さず比率で見せる ────────────────────────────────────
def test_cost_page_shows_budget_ratio_and_per_run(app):
    at = app("cost")
    text = _texts(at)
    assert "月次予算の消化(当月・" in text  # 暦月起点(中-9)
    assert "1 実行あたり" in text
    assert "トークン合計" not in text  # 累計 vanity は廃止
    assert len(at.get("progress")) == 1


# ── 禁止記法が構造的に混入しないこと(中-8)──────────────────────────────────
def test_app_source_has_no_raw_progress_or_json():
    """ページ実装は表示形を viz ヘルパ経由でのみ作る(CI の grep と同じ規約)。"""
    source = _APP.read_text(encoding="utf-8")
    assert not re.search(r"(^|[^.\w])st\.(progress|json)\(", source)


def test_plan_page_uses_helper_progress_with_denominator(app):
    """生 st.progress を置き換えた進捗バーが分母つきで出ている。"""
    bars = [b.proto.text for b in app("plan").get("progress")]
    assert bars, "ロードマップの進捗バーが無い"
    assert all("/" in b for b in bars)
    assert any("全体進捗(マイルストーン完了)" in b for b in bars)


# ── 承認・通知 ────────────────────────────────────────────────────────────────
def test_approvals_summary_row_counts_and_oldest_age(app):
    text = _texts(app("approvals"))
    assert "未配送の通知" in text
    assert "最古" in text
