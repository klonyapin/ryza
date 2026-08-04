"""独立役員審査の意見書(``docs/reviews/*.md``)の front matter を読む。

**なぜ審査側に機械可読な記録を置くか**: 0029 と A-18-8 は「``Approved:`` トレーラの
``reviewed=<sha40>``」と「``governance.decisions.reviewed_sha``」を突合するが、**どちらも
発効を起票した設計リードが書く申告**であり、同じ値を両方に書けば一致で通る
(``docs/reviews/g-a18-protect-independent-review.md`` 重要-3、``ops/reminders.yaml``
``reviewed-sha-from-review-agent``)。統制として成立していたのは「片側だけの改変・取り違えの
検出」までで、「独立審査が実際にその SHA を見たこと」の証明ではなかった。本モジュールは
**審査成果物そのもの**(意見書)を第三の記録として読み、審査側が書いた ``reviewed_sha`` を
起票者の申告より優先させるための入り口である。

様式(v1 = 新様式。front matter を持たない既存の意見書は「旧様式」として扱う)::

    ---
    reviewed_sha: 0123456789abcdef0123456789abcdef01234567
    review_date: 2026-08-04
    verdict: conditional_approve
    ---

    # 独立役員意見書 — ...

**旧様式を遡及改変しない**(``ops/reminders.yaml`` の本タスク条件)。front matter の無い
意見書は :func:`load_review_artifact` が ``None`` を返し、呼び出し側は 0029 以前と同じ動作に
落ちる —— 過去の審査に後から front matter を足せてしまうと「審査側の記録」という主張が
起票者の申告と区別できなくなる。

**残る限界(重要・honest disclosure)**: 意見書はリポジトリ内の平文であり、審査エージェント
自身の署名は無い。起票者が front matter を書き換える・front matter ごと消す・そもそも
front matter の無いファイルを ``--review`` に指す、のいずれも技術的には可能である。本様式が
足すのは (1) 起票者の申告と審査側の記録が**食い違えば発効が止まる**こと、(2) 突合済みの
``reviewed_sha`` のうち審査記録に由来する割合が A-18-8 で毎週開示され、由来のない申告が
埋もれないこと、の2点にとどまる。署名(審査エージェントの鍵)は将来課題である。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

#: front matter の開始・終了フェンス(行全体がこれ)。
FENCE = "---"

#: YAML ドキュメント終端としても閉じを認める(``...``)。
_CLOSING = (FENCE, "...")

#: 審査対象コミットの様式。``governance.decisions.reviewed_sha``(0029 の CHECK)・
#: 監査 A-18 の ``reviewed=`` と同じく **40 桁 hex の完全 SHA のみ**。短縮 SHA を許すと
#: 突合が「一致とも不一致とも言えない」第三の状態を作る。
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)

#: ``verdict`` の語彙。判定は統制の分岐に使わない(現状は開示のみ)ので、語彙外は
#: 警告にとどめる —— 判定名の揺れで発効が止まると、様式そのものが忌避される。
VERDICTS: tuple[str, ...] = ("approve", "conditional_approve", "reject")

#: 新様式が持つべきキー。欠けても発効は止めない(下の :class:`ReviewArtifact` 参照)。
EXPECTED_KEYS: tuple[str, ...] = ("reviewed_sha", "review_date", "verdict")


class ReviewArtifactError(ValueError):
    """意見書の front matter が壊れている(呼び出し側は発効を止める)。

    **壊れているものを「旧様式」に読み替えない**のが本例外の存在理由である。YAML を壊せば
    front matter が無いことにできる、という抜け道を残すと、審査側の記録を優先する統制は
    「壊し方を知っている者には効かない」ものになる。
    """


@dataclass(frozen=True)
class ReviewArtifact:
    """意見書 1 件の front matter(新様式)。

    ``reviewed_sha`` が ``None`` になるのは front matter はあるがキーが無い場合である。
    **これは失敗ではなく警告**にする: 欠落は情報量として旧様式と同じであり、ここで発効を
    止めると「front matter ごと消せば通る」という逆インセンティブになる。食い違い
    (審査側が書いた SHA と起票者の申告が別)だけが fail-safe の対象である。
    """

    path: Path | None
    reviewed_sha: str | None
    review_date: str | None
    verdict: str | None
    data: dict[str, Any]
    #: 様式の不備(発効は止めないが、呼び出し側が開示する)
    warnings: tuple[str, ...] = ()


def split_front_matter(text: str) -> tuple[str | None, str]:
    """先頭の front matter ブロックを ``(YAML 本文, 残りの本文)`` に分ける。

    front matter が無ければ ``(None, text)``。**先頭行がフェンスなのに閉じが無い**場合は
    :class:`ReviewArtifactError` —— 途中まで書いた front matter を「無し」と読むと、
    閉じフェンスを消すだけで審査側の記録を消せてしまう。
    """
    stripped = text.lstrip("﻿")
    lines = stripped.splitlines()
    if not lines or lines[0].strip() != FENCE:
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() in _CLOSING:
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    raise ReviewArtifactError(
        "front matter の開始フェンス(---)はあるが閉じフェンスが無い"
        "(様式不備 — 途中で切れた front matter を『旧様式』とは読まない)"
    )


def parse_review_artifact(text: str, *, path: Path | None = None) -> ReviewArtifact | None:
    """意見書本文から front matter を読む。旧様式(front matter 無し)は ``None``。

    Raises:
        ReviewArtifactError: フェンスが閉じない / YAML が壊れている / マッピングでない /
            ``reviewed_sha`` が 40 桁 hex の完全 SHA でない
    """
    raw, _ = split_front_matter(text)
    if raw is None:
        return None
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ReviewArtifactError(f"front matter の YAML を解釈できない: {exc}") from exc
    if data is None:
        raise ReviewArtifactError("front matter が空(様式不備)")
    if not isinstance(data, dict):
        raise ReviewArtifactError(
            f"front matter はマッピングである必要がある(実際: {type(data).__name__})"
        )

    warnings: list[str] = []
    reviewed_sha = _read_reviewed_sha(data)
    verdict = _read_verdict(data, warnings)
    review_date = _read_review_date(data, warnings)
    for key in EXPECTED_KEYS:
        if data.get(key) in (None, ""):
            warnings.append(f"front matter に {key} が無い(新様式は {', '.join(EXPECTED_KEYS)})")
    return ReviewArtifact(
        path=path,
        reviewed_sha=reviewed_sha,
        review_date=review_date,
        verdict=verdict,
        data=data,
        warnings=tuple(warnings),
    )


def _read_reviewed_sha(data: dict[str, Any]) -> str | None:
    """``reviewed_sha`` を検証して小文字へ正規化する(欠落は ``None``)。

    YAML は 40 桁の数字だけの値を整数として読むため、文字列でない値もいったん受けてから
    様式検査に掛ける(先頭 0 が落ちた値は 40 桁にならず、引用を促すエラーになる)。
    """
    value = data.get("reviewed_sha")
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not _FULL_SHA_RE.match(text):
        raise ReviewArtifactError(
            f"reviewed_sha は 40 桁 hex の完全 SHA である必要がある: {value!r}"
            "(YAML が数値として解釈した可能性がある場合は引用符で囲むこと)"
        )
    return text.lower()


def _read_verdict(data: dict[str, Any], warnings: list[str]) -> str | None:
    value = data.get("verdict")
    if value in (None, ""):
        return None
    verdict = str(value).strip()
    if verdict not in VERDICTS:
        warnings.append(
            f"verdict='{verdict}' は語彙外({'/'.join(VERDICTS)})— 判定は統制の分岐に"
            "使っていないため発効は妨げない"
        )
    return verdict


def _read_review_date(data: dict[str, Any], warnings: list[str]) -> str | None:
    value = data.get("review_date")
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    try:
        date.fromisoformat(text)
    except ValueError:
        warnings.append(f"review_date='{text}' が ISO 日付(YYYY-MM-DD)として読めない")
    return text


def is_repo_path_ref(ref: str | None) -> bool:
    """``--review`` / ``review_ref`` の値がリポジトリ内パス形式か。

    URL(``https://`` 等のスキーム付き)や ``#`` 始まりの参照、絶対パス、``..`` を含む
    参照は対象外にする。**リポジトリ外を読みに行かない**のは、審査記録の所在をリポジトリに
    限定するためであり、``../../etc`` のような参照でリポジトリ外のファイルを「審査記録」と
    主張させないためでもある。
    """
    if not ref:
        return False
    text = ref.strip()
    if not text or "://" in text or text.startswith(("#", "/")):
        return False
    return ".." not in Path(text).parts


def load_review_artifact(
    ref: str | None, *, repo_root: Path | str | None
) -> ReviewArtifact | None:
    """``ref`` が指す意見書の front matter を読む。読めない/旧様式なら ``None``。

    ``None`` を返すのは「審査側の記録が無い」場合であり、呼び出し側は 0029 以前と同じ動作
    (起票者の申告をそのまま使う)へ落ちる。**壊れた front matter は ``None`` ではなく
    :class:`ReviewArtifactError`** —— 区別しないと様式を壊すことが回避策になる。

    Args:
        ref: ``--review`` の値(リポジトリ相対パスのときだけ読む)
        repo_root: リポジトリルート。``None`` なら検査そのものを行わない
            (パッケージ設置環境で全参照が読めないと誤判定するのを避ける)
    """
    if repo_root is None or not is_repo_path_ref(ref):
        return None
    path = Path(repo_root) / str(ref).strip()
    if not path.is_file():
        return None
    return parse_review_artifact(path.read_text(encoding="utf-8"), path=path)


__all__ = [
    "EXPECTED_KEYS",
    "FENCE",
    "VERDICTS",
    "ReviewArtifact",
    "ReviewArtifactError",
    "is_repo_path_ref",
    "load_review_artifact",
    "parse_review_artifact",
    "split_front_matter",
]
