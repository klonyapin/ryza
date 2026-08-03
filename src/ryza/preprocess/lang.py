"""lang — 言語判定（階層0・LLM 非依存・依存ゼロ）。

設計 20-research §3 ②「言語判定」。重い言語判定ライブラリを持ち込まず、文字種の割合で
日本語（``ja``）/ 英語（``en``）/ 判定不能（``und``）を軽量に判定する。日本語（CJK・かな）
と英語の 2 言語を主対象とする初期スコープに十分な精度。

純関数（DB 非依存）。
"""

from __future__ import annotations


def _is_cjk_or_kana(ch: str) -> bool:
    o = ord(ch)
    return (
        0x3040 <= o <= 0x30FF   # ひらがな・カタカナ
        or 0x3400 <= o <= 0x4DBF  # CJK 拡張 A
        or 0x4E00 <= o <= 0x9FFF  # CJK 統合漢字
        or 0xFF66 <= o <= 0xFF9D  # 半角カナ
    )


def _is_latin_alpha(ch: str) -> bool:
    return ("a" <= ch.lower() <= "z")


def detect_lang(text: str | None) -> str:
    """テキストの言語を ``'ja'`` | ``'en'`` | ``'und'`` で返す。

    かな・CJK 文字が 1 つでも一定割合あれば ``ja``。無く、ラテン文字が主なら ``en``。
    判定材料が乏しければ ``und``。
    """
    if not text:
        return "und"
    cjk = sum(1 for ch in text if _is_cjk_or_kana(ch))
    latin = sum(1 for ch in text if _is_latin_alpha(ch))
    if cjk == 0 and latin == 0:
        return "und"
    # 日本語文はラテン文字（英数・企業名）を多く含むが、CJK/かなが一定量あれば ja とみなす。
    if cjk > 0 and cjk >= latin * 0.15:
        return "ja"
    if latin > 0:
        return "en"
    return "und"
