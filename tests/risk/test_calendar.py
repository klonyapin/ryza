"""営業日カレンダーの単体検査(``src/ryza/risk/calendar.py``)。

G-10 の限度状態鮮度検査が依存するため、境界(週末・祝日・年始・振替)を明示的に固定する。
テーブル外(範囲外の日)は「祝日でない = 営業日」に倒れる — fail-closed の方向。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from ryza.risk.calendar import business_days_between, is_business_day, to_jst_date


# ── is_business_day: 週末・祝日 ─────────────────────────────────────────────────
def test_weekday_is_business_day():
    assert is_business_day(date(2026, 8, 4))  # Tue
    assert is_business_day(date(2026, 8, 5))  # Wed


def test_weekend_is_not_business_day():
    assert not is_business_day(date(2026, 8, 1))  # Sat
    assert not is_business_day(date(2026, 8, 2))  # Sun


def test_jp_holidays_are_not_business_days():
    assert not is_business_day(date(2026, 8, 11))  # 山の日 (Tue)
    assert not is_business_day(date(2026, 1, 1))   # 元日
    assert not is_business_day(date(2026, 5, 6))   # 振替休日
    assert not is_business_day(date(2026, 9, 22))  # 国民の休日


def test_out_of_range_falls_back_to_weekday_only():
    """テーブル外の年(例: 2028)は「祝日でない=営業日」に倒れる — fail-closed の方向。

    2028-01-01 は土曜(週末)なので False、2028-01-03 は月曜だが祝日テーブル外なので
    True(実際は元日振替で祝日だが、把握していない祝日を営業日として数え、G-10 の
    経過日数を過大評価する = より早く block する。ゲートの fail-closed 原則に沿う)。
    """
    assert not is_business_day(date(2028, 1, 1))  # Sat
    assert is_business_day(date(2028, 1, 3))       # Mon — 表外は営業日扱い


# ── business_days_between: 半開区間 (start, end] ───────────────────────────────
def test_same_day_is_zero():
    d = date(2026, 8, 4)
    assert business_days_between(d, d) == 0


def test_end_before_start_is_zero():
    """未来 start(= end < start)は 0 に倒す — G-10 側で別途 fail-closed 判定。"""
    assert business_days_between(date(2026, 8, 5), date(2026, 8, 4)) == 0


def test_single_weekday_step_is_one():
    """Mon→Tue の 1 日進みは 1 営業日。"""
    assert business_days_between(date(2026, 8, 3), date(2026, 8, 4)) == 1


def test_weekend_crossing_counts_only_weekdays():
    """Fri Jul 31 → Tue Aug 4: 週末を挟むが営業日は Mon Aug 3・Tue Aug 4 の 2 日。"""
    assert business_days_between(date(2026, 7, 31), date(2026, 8, 4)) == 2


def test_holiday_crossing_skips_holiday():
    """Fri Aug 7 → Wed Aug 12: 山の日(Tue Aug 11)を挟む → Mon 10・Wed 12 の 2 日。"""
    assert business_days_between(date(2026, 8, 7), date(2026, 8, 12)) == 2


def test_holiday_crossing_one_day_more_hits_three():
    """Fri Aug 7 → Thu Aug 13: Mon 10・Wed 12・Thu 13 → 3 日(境界の直上)。"""
    assert business_days_between(date(2026, 8, 7), date(2026, 8, 13)) == 3


def test_year_end_new_year_block_is_all_holiday():
    """年末年始(2026-12-31 〜 2027-01-04)を跨いで 1 営業日ぶんも進まないケース。

    Wed Dec 30 → Mon Jan 4 2027(2027-01-04 は表で祝日): Thu 12/31 (祝) → Fri 1/1 (祝)
    → Sat/Sun (週末) → Mon 1/4 (祝)。合計 0 営業日。
    """
    assert business_days_between(date(2026, 12, 30), date(2027, 1, 4)) == 0


def test_year_end_new_year_first_business_day_2027():
    """Wed Dec 30 2026 → Tue Jan 5 2027(初営業日)= 1 営業日。"""
    assert business_days_between(date(2026, 12, 30), date(2027, 1, 5)) == 1


# ── to_jst_date: timestamptz → JST 日付 ────────────────────────────────────────
def test_to_jst_date_converts_utc_to_jst():
    """22:00 UTC は翌日 07:00 JST。"""
    dt = datetime(2026, 8, 3, 22, 0, tzinfo=UTC)
    assert to_jst_date(dt) == date(2026, 8, 4)


def test_to_jst_date_before_utc_midnight_is_same_jst_day():
    """10:00 UTC = 19:00 JST は当日。"""
    dt = datetime(2026, 8, 4, 10, 0, tzinfo=UTC)
    assert to_jst_date(dt) == date(2026, 8, 4)
