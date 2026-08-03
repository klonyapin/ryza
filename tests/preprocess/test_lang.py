"""lang（言語判定）の単体テスト（DB 非依存）。"""

from __future__ import annotations

import pytest

from ryza.preprocess.lang import detect_lang


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("トヨタ自動車は通期業績予想を上方修正した。", "ja"),
        ("The Fed held rates steady on Wednesday.", "en"),
        ("トヨタ（TOYOTA）7203 業績予想を修正", "ja"),  # 英数混在でも CJK が一定量→ja
        ("", "und"),
        ("1234 5678 %%%", "und"),
        (None, "und"),
    ],
)
def test_detect_lang(text, expected):
    assert detect_lang(text) == expected


def test_english_with_few_kana_still_english():
    # ほぼ英語で、ごく僅かな記号のみ。ラテン主体なら en。
    assert detect_lang("Bank of Japan policy meeting minutes released today.") == "en"
