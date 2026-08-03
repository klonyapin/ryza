"""classify（開示種別・ニュースカテゴリ）の単体テスト（DB 非依存）。"""

from __future__ import annotations

import pytest

from ryza.preprocess.classify import RuleClassifier, classify


@pytest.mark.parametrize(
    ("title", "source_type", "expected_category"),
    [
        ("2025年3月期 通期業績予想の修正に関するお知らせ", "filing",
         "filing_guidance_revision"),
        ("株式会社◯◯に対する公開買付け（TOB）の開始について", "filing", "filing_mna"),
        ("2025年3月期 第2四半期決算短信〔日本基準〕", "filing", "filing_earnings"),
        ("自己株式の取得に係る事項の決定に関するお知らせ", "filing", "filing_buyback"),
        ("配当予想の修正（増配）に関するお知らせ", "filing", "filing_dividend"),
        ("株式分割および定款の一部変更に関するお知らせ", "filing", "filing_stock_split"),
        ("変更報告書（大量保有）の提出", "filing", "filing_large_holding"),
    ],
)
def test_filing_classification(title, source_type, expected_category):
    result = classify(title, None, source_type)
    assert result.category == expected_category
    assert result.label is not None
    # 根拠（どのパターンでマッチしたか）が残る（監査 A-13 対象）。
    assert result.rationale and result.rationale[0]["category"] == expected_category


@pytest.mark.parametrize(
    ("title", "source_type", "expected_category"),
    [
        ("日銀、政策金利を据え置き 金融緩和を維持", "news", "news_monetary_policy"),
        ("FOMC、利上げを決定", "central_bank", "news_monetary_policy"),
        ("円安加速、一時1ドル160円台に 為替介入警戒", "news", "news_fx"),
        ("A社が最終利益過去最高を更新、通期業績を上方修正", "news", "news_earnings"),
    ],
)
def test_news_classification(title, source_type, expected_category):
    result = classify(title, None, source_type)
    assert result.category == expected_category


def test_unknown_when_no_rule_matches():
    result = classify("本日は晴天なり", None, "news")
    assert result.category == "unknown"
    assert result.label is None
    assert result.labels == []


def test_priority_first_rule_wins():
    # 「業績予想の修正」（高優先）と「決算」の両方に触れる文面 → 予想修正が採用される。
    title = "通期業績予想の修正および決算短信に関するお知らせ"
    result = classify(title, None, "filing")
    assert result.category == "filing_guidance_revision"
    # 複数ラベルが labels に載る。
    assert "業績予想の修正" in result.labels


def test_classifier_is_swappable_protocol():
    # RuleClassifier は Classifier プロトコルを満たし差し替え可能（学習分類器の器）。
    clf = RuleClassifier()
    assert clf.classify("決算短信", None, "filing").category == "filing_earnings"
