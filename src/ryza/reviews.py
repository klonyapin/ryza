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

**塞いだ迂回路(2026-08-04 独立役員審査 C-1〜C-3・C-6・C-7)**: 参照表記を変えるだけの無音の
迂回(``..`` 表記・自リポジトリの blob URL・先頭空行での front matter 無効化)、判定が
``reject`` の意見書での発効、由来の事後製造(決定より後に意見書を足す)、front matter の
重複キー、symlink 経由のリポジトリ外読み取り —— これらはいずれも**中止**または**別カウント
での開示**になる。上の「残る限界」は、これらを除いた**書き換え・削除・別ファイル指定**に
限られる(いずれも意見書そのものの改変であり、diff に残る)。
"""

from __future__ import annotations

import os
import re
import subprocess
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

#: ``verdict`` の語彙。
#:
#: - ``approve`` … 承認
#: - ``conditional_approve`` … 条件付き承認(条件を反映済みで発効可)
#: - ``request_changes`` … 要修正(**未是正** — 発効不可)
#: - ``reject`` … 否認(発効不可)
#:
#: **語彙外**の値は警告にとどめる —— 判定名の揺れで発効が止まると様式そのものが忌避される。
#: 一方 :data:`BLOCKING_VERDICTS` は語彙内の一意な値であり、揺れの問題ではない
#: (独立役員審査 2026-08-04 C-1)。
VERDICTS: tuple[str, ...] = ("approve", "conditional_approve", "request_changes", "reject")

#: 発効を許さない判定。CLI はこれを見たら **DB へ何も書かずに中止**する。
#:
#: **なぜ中止か**: 審査記録を読み、否認を見て、それを捨てたうえで「独立役員審査: <その
#: 意見書>」と ``#承認`` に掲示するのは、本実装が新たに作った**偽の保証**である
#: (審査 C-1)。48h 異議期間に代表が見る唯一の成果物が通知である以上、否認された審査を
#: 裏付けとして掲示することは定款第3条・第5条の「審査を前置する」手続の逆転にあたる。
#: **強行経路は作らない** —— 是正して意見書を更新するのが正規の道である。
BLOCKING_VERDICTS: frozenset[str] = frozenset({"request_changes", "reject"})

#: 新様式が持つべきキー。欠けても発効は止めない(下の :class:`ReviewArtifact` 参照)。
EXPECTED_KEYS: tuple[str, ...] = ("reviewed_sha", "review_date", "verdict")

#: 自リポジトリの GitHub blob URL(審査 C-2(b): 参照の書式を変えるだけの迂回を塞ぐ)。
_BLOB_URL_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/blob/[^/]+/(.+?)(?:[#?].*)?$", re.IGNORECASE
)

#: ``git remote get-url origin`` から owner/repo を取り出す(https / ssh の両形式)。
_ORIGIN_SLUG_RE = re.compile(r"github\.com[:/]+([^/]+)/(.+?)(?:\.git)?/?$", re.IGNORECASE)

#: front matter 内のトップレベルキー(重複検出用 — 審査 C-6)。
_TOP_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:")


class ReviewArtifactError(ValueError):
    """意見書の front matter・参照が様式に反する(呼び出し側は発効を止める)。

    **壊れているものを「旧様式」に読み替えない**のが本例外の存在理由である。YAML を壊せば
    front matter が無いことにできる、という抜け道を残すと、審査側の記録を優先する統制は
    「壊し方を知っている者には効かない」ものになる。同じ理由で、リポジトリ外へ出る参照・
    symlink 参照も「読めないから審査記録なし」ではなく**エラー**にする(審査 C-2)。
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

    **開始フェンスの前の空行・空白は読み飛ばす**(審査 C-2): 旧実装は 0 行目だけを開始
    フェンスと見なしたため、**先頭に空行を1行入れるだけ**で front matter 全体が無効化でき、
    しかも生ファイルを読む人間には front matter が見えたままだった。フェンス行の前後の空白は
    既に許容していたのに空行だけが無効化する、という非対称も様式として説明できない。
    """
    stripped = text.lstrip("﻿")
    lines = stripped.splitlines()
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines) or lines[start].strip() != FENCE:
        return None, text
    for i in range(start + 1, len(lines)):
        if lines[i].strip() in _CLOSING:
            return "\n".join(lines[start + 1:i]), "\n".join(lines[i + 1:])
    raise ReviewArtifactError(
        "front matter の開始フェンス(---)はあるが閉じフェンスが無い"
        "(様式不備 — 途中で切れた front matter を『旧様式』とは読まない)"
    )


def _raise_on_duplicate_keys(raw: str) -> None:
    """front matter のトップレベルキー重複を拒否する(審査 C-6)。

    ``yaml.safe_load`` は重複キーを**後勝ちで無警告**に解決するため、正直な SHA を先に、
    偽の SHA を後に置く 2 行だけで採用値を差し替えられる。統制の入力である以上、
    「どちらが効いているか読んで分からない」書き方は受け付けない。ネストした構造は
    現様式に無いので、インデントの無い行だけを見る安価な検査で足りる。
    """
    seen: set[str] = set()
    for line in raw.splitlines():
        if line[:1].isspace() or not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _TOP_KEY_RE.match(line)
        if m is None:
            continue
        key = m.group(1)
        if key in seen:
            raise ReviewArtifactError(
                f"front matter にキー '{key}' が複数ある(後勝ちで黙って採用値が変わるため拒否)"
            )
        seen.add(key)


def parse_review_artifact(text: str, *, path: Path | None = None) -> ReviewArtifact | None:
    """意見書本文から front matter を読む。旧様式(front matter 無し)は ``None``。

    Raises:
        ReviewArtifactError: フェンスが閉じない / YAML が壊れている / マッピングでない /
            ``reviewed_sha`` が 40 桁 hex の完全 SHA でない
    """
    raw, _ = split_front_matter(text)
    if raw is None:
        return None
    _raise_on_duplicate_keys(raw)
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
    """``--review`` / ``review_ref`` の値が**リポジトリ内パス形式に見える**か。

    URL(スキーム付き)・``#`` 始まり・絶対パスを除いた残りが対象である。``..`` を含む
    参照はここでは弾かない —— 旧実装は弾いていたが、その結果
    ``docs/reviews/../reviews/x.md`` が「審査記録なし」として**無音で**起票者の申告に
    落ちる迂回路になっていた(審査 C-2)。正規化と範囲検査は
    :func:`resolve_review_path` の担当であり、そこでリポジトリ外へ出るものは
    ``None`` ではなく**エラー**になる。

    自リポジトリの GitHub blob URL は :func:`resolve_review_path` がパスへ変換するため、
    本関数が ``False`` を返してもリポジトリ内参照でありうる。判定は本関数ではなく
    :func:`resolve_review_path` の戻り値で行うこと。
    """
    if not ref:
        return False
    text = ref.strip()
    return bool(text) and "://" not in text and not text.startswith(("#", "/"))


def origin_slug(repo_root: Path | str) -> str | None:
    """``origin`` remote の ``owner/repo``(取れなければ ``None``)。

    自リポジトリの blob URL だけをパスへ変換するために使う。他リポジトリの URL を
    ローカルパスとして読むと、同名ファイルを「審査記録」と誤認しうる。
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    m = _ORIGIN_SLUG_RE.search(out.stdout.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def resolve_review_path(
    ref: str | None, *, repo_root: Path | str | None, repo_slug: str | None = None
) -> Path | None:
    """``ref`` をリポジトリ内の実パスへ解決する。リポジトリ内参照でなければ ``None``。

    **無音の迂回を作らないことが本関数の目的である**(審査 C-2)。参照の書式を変えるだけで
    「審査記録なし」に落とせるなら、fail-safe は書式を知っている者には効かない。したがって:

    - ``..`` や ``//``、``./`` は :func:`os.path.normpath` で正規化してから解決する
      (同じファイルを指す表記は同じ扱いになる)
    - 正規化後に**リポジトリ外へ出る**参照は :class:`ReviewArtifactError`(中止)。
      「読めないので審査記録なし」に落とすと、脱出表記が迂回路になる
    - 経路上に **symlink** があれば :class:`ReviewArtifactError`(審査 C-7)。旧 docstring は
      「リポジトリ外を読みに行かない」と書いていたが symlink で破れており、**持っていない
      保証を主張していた**。読まずに落とすのではなく、明示的に拒否して保証を実体化する
    - 自リポジトリの **GitHub blob URL** はリポジトリ内パスへ変換する(他リポジトリの URL は
      従来どおり ``None`` = リポジトリ外の審査参照)

    ``repo_root`` が ``None`` の実行(パッケージ設置)では検査そのものを行わず ``None``。
    """
    if repo_root is None or not ref:
        return None
    text = ref.strip()
    if not text:
        return None
    if "://" in text:
        m = _BLOB_URL_RE.match(text)
        if m is None:
            return None
        slug = repo_slug if repo_slug is not None else origin_slug(repo_root)
        if not slug or f"{m.group(1)}/{m.group(2)}".lower() != slug.lower():
            return None  # 他リポジトリの意見書 URL はローカルパスとして読まない
        text = m.group(3)
    if text.startswith("#"):
        return None
    root = Path(repo_root)
    if Path(text).is_absolute():
        raise ReviewArtifactError(f"審査参照は絶対パスにできない(リポジトリ相対で書くこと): {ref}")
    normalized = os.path.normpath(text)
    if normalized == os.pardir or normalized.startswith(os.pardir + os.sep):
        raise ReviewArtifactError(
            f"審査参照 '{ref}' は正規化するとリポジトリ外を指す(審査記録はリポジトリ内に置く)"
        )
    path = root / normalized
    root_real = os.path.realpath(root)
    path_real = os.path.realpath(path)
    if os.path.commonpath([root_real, path_real]) != root_real:
        raise ReviewArtifactError(
            f"審査参照 '{ref}' はリポジトリ外(symlink 先を含む)を指す: {path_real}"
        )
    if path.exists() and path_real != os.path.realpath(str(path.absolute())):  # pragma: no cover
        raise ReviewArtifactError(f"審査参照 '{ref}' の解決結果が一意でない")
    if _has_symlink(root, path):
        raise ReviewArtifactError(
            f"審査参照 '{ref}' の経路に symlink がある(審査記録の所在を一意にするため拒否)"
        )
    return path


def _has_symlink(root: Path, path: Path) -> bool:
    """``root`` から ``path`` までの経路(自身を含む)に symlink があるか。"""
    current = path
    root_resolved = root.absolute()
    while True:
        if current.is_symlink():
            return True
        if current.absolute() == root_resolved or current.parent == current:
            return False
        current = current.parent


def load_review_artifact(
    ref: str | None, *, repo_root: Path | str | None, repo_slug: str | None = None
) -> ReviewArtifact | None:
    """``ref`` が指す意見書の front matter を読む。リポジトリ外参照/旧様式なら ``None``。

    ``None`` を返すのは「審査側の記録が無い」場合であり、呼び出し側は 0029 以前と同じ動作
    (起票者の申告をそのまま使う)へ落ちる。**壊れた front matter・脱出する参照は ``None``
    ではなく :class:`ReviewArtifactError`** —— 区別しないと様式や表記を壊すことが回避策になる。

    Args:
        ref: ``--review`` の値(リポジトリ相対パス・自リポジトリの blob URL)
        repo_root: リポジトリルート。``None`` なら検査そのものを行わない
            (パッケージ設置環境で全参照が読めないと誤判定するのを避ける)
        repo_slug: 自リポジトリの ``owner/repo``。省略時は ``origin`` remote から取る
    """
    path = resolve_review_path(ref, repo_root=repo_root, repo_slug=repo_slug)
    if path is None or not path.is_file():
        return None
    return parse_review_artifact(path.read_text(encoding="utf-8"), path=path)


def first_commit_date(repo_root: Path | str, path: Path | str) -> str | None:
    """意見書がリポジトリに**初めて現れた**コミットの committer date(ISO)。

    由来 (``from_review_artifact``) の事後製造を検出するための材料である(審査 C-3):
    決定と ``Approved:`` トレーラを先に作り、その後で同じ SHA を宣言する意見書を commit
    するだけで、作業ツリーだけを読む監査は由来率を 100% にできてしまう。「意見書が決定より
    前から在ったか」は git にしか無い事実なので、監査時点のツリーではなく履歴に問う。

    追跡されていない(commit されていない)ファイルは ``None``。改名は ``--follow`` で辿る。
    """
    rel = os.path.relpath(str(path), str(repo_root))
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "log", "--follow", "--format=%cI", "--reverse",
             "--", rel],
            capture_output=True, text=True, check=False, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        if line.strip():
            return line.strip()
    return None


__all__ = [
    "BLOCKING_VERDICTS",
    "EXPECTED_KEYS",
    "FENCE",
    "VERDICTS",
    "ReviewArtifact",
    "ReviewArtifactError",
    "first_commit_date",
    "is_repo_path_ref",
    "load_review_artifact",
    "origin_slug",
    "parse_review_artifact",
    "resolve_review_path",
    "split_front_matter",
]
