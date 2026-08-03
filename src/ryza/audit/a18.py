"""A-18 規則⇔実装トレーサビリティ監査(定款第6条・config/governance.yaml controls)。

8つの検査を実行し、構造化 dict を返す:

  A-18-1 保護領域突合   … `protected_areas` の glob に触れた発効日以後のコミットを列挙し、
                          (a) ``Approved:`` トレーラ (b) GitHub マージ PR 経由(Merge pull request
                          マージコミットの配下) (c) PR 承継(有効なトレーラを持つ PR マージが
                          持ち込んだコミット群)のいずれも無いものを違反として列挙する。
                          DB 接続がある実行ではトレーラの参照先を ``current_decisions`` と
                          突合し、否認済み・却下・不在は承認と見なさない(承継の起点判定も
                          同じ照合を通る)
  A-18-2 文書⇔config    … 80-ips.md ⇔ config/ips.yaml、06-constitution.md ⇔ config/governance.yaml
                          のバージョン文字列一致を検査する
  A-18-3 宣言棚卸し     … controls のうち ``enforcement: declaration`` を列挙する(検査ではなく
                          可視化 — 四半期ごとの執行点実装可否の再評価対象)
  A-18-4 全変更 PR 化   … 基準コミット(``PR_RULE_BASELINE_COMMIT``)以降の first-parent 履歴で、
                          (a) マージコミットでないコミット(= main への直 push)
                          (b) 件名が PR マージ形式(``Merge pull request #N``)でない、または
                          PR #N が GitHub 上に実在しない/未マージのマージコミット
                          を保護領域か否かにかかわらず違反として列挙する。例外なし
                          (``Approved:`` トレーラ付き直 push も違反 — 2026-08-03 代表指示)
  A-18-5 通知なき発効   … ``decision='deemed'`` の通知参照(``outbox:<id>``)が指す
                          ``press.outbox`` の行が ``UNNOTIFIED_DEEMED_MINUTES`` を超えて
                          未配送なら違反として列挙する。定款第3条はみなし承認を「通知と同時に
                          発効」と定めるが、**outbox への投入は配送ではない** — 配送が止まれば
                          「発効したが誰も知らない」状態が続く(独立役員審査 重要-3)。
                          DB 接続がある実行でのみ動く
  A-18-6 決議の批判経由   … 「批判を経ない決議」(``governance.minute_resolutions`` の
                          ``confirmed_without_critic`` が true=鮮度なしを確認して通した、
                          または NULL=鮮度の判定不能)の直近件数・連続数を集計し、
                          ``boardroom.CONFIRMATION_STREAK_ALERT`` 件連続または走査窓内
                          ``CONFIRMATION_COUNT_ALERT`` 件で警告する。DB 接続がある実行でのみ動く
  A-18-7 承認記録漏れ    … ``DEEMED_RECORD_BASELINE_COMMIT`` 以降の first-parent 上の PR マージ
                          (親2・件名が ``Merge pull request``)で保護領域に触れたもののうち、
                          **その PR に帰属する**承認記録(``proposal_ref`` が
                          ``https://github.com/<slug>/pull/<N>`` に完全一致)が無いものを
                          列挙する。トレーラの参照は、指す決定の ``proposal_ref`` がこの PR
                          である場合にのみ帰属と認める(別 PR の記録の複写で緑にしない)。
                          みなし承認の発効通知は ``python -m ryza.governance.decisions
                          --deemed`` を人が叩くことでしか出ず(自動起票は未実装 — 独立役員審査
                          中-7)、叩き忘れは「通知なき発効」になる。A-18-5 が「記録はあるが
                          未配送」を見るのに対し、本検査は「記録そのものが無い」を見る。
                          緑には必ず分母(検査対象 PR 数)を出す。DB 接続がある実行でのみ動く
  A-18-8 審査対象 SHA    … ``Approved:`` トレーラの ``reviewed=<sha40>`` と、その参照が指す
                          承認記録の ``governance.decisions.reviewed_sha``(0029)が**両方ある**
                          決定について一致を検査する。両者は別経路で書かれる同じ主張
                          (トレーラ = PR 作成者 / 記録 = 発効 CLI が gh から取った head SHA)
                          なので、片側だけの改変・取り違えは不一致として出る。**証明ではない**
                          — どちらも起票者の申告であり審査エージェント自身の署名は無い
                          (同じ嘘を両方に書けば一致する)。緑には必ず分母(突合できた件数)を
                          出す。DB 接続がある実行でのみ動く

**A-18-6 をここに置く理由**(ops-weekly VM 移設審査 2026-08-04 の代替案(d)・設計リード裁定):
この指標は決議精緻化審査(2026-08-03)が新設した統制で、当初は週次ジョブ ops-weekly の
ダイジェストに載せる設計だった。しかし ops-weekly は Cloud Run Job で VM 内 PostgreSQL に
届かず、配線には実行基盤の移設が要る。移設案は「監査が可変の稼働コード ``/opt/ryza`` から
走る」「env の1行削除が『未配線』= 移設前と同一表示に化ける」という2つの重大な穴を生んだ。
A-18 は既に(1)監査専用 clone ``/opt/ryza-audit`` から走り、(2)``--always-report`` の
ハートビートを持ち、(3)``press.outbox`` 経由で #運営 に届く。同じ統制目的(**確認を外す当人
から独立した検出点**)を、これらの性質を作り直さずに達成できるため A-18 側に載せる。

なお本検査は ``config/governance.yaml`` の statement に対応を持たない(05-governance §6-5 は
「懸念ゼロ回答の連続」「付議なし期間」を挙げるが本指標そのものは挙げていない — その趣旨に
連なる**同型の指標**として新設されたものである)。条文に書かれていない統制を書かれているかの
ように引くと版と実装の対応を追う A-18-1/2 の突合が狂うため、根拠条文としては引用しない。

**read-only 原則**: 本モジュールは検査と警告(``press.outbox`` の ops チャンネルへの embed 投入)
のみを行い、修正・巻き戻し・コミットは一切行わない。

**対象範囲**: 発効日(2026-08-03 の定款批准コミット ``RATIFICATION_COMMIT``)より後のコミットのみ。
``git rev-list <批准>..HEAD`` は批准コミット自身とその祖先を除外する。

**既知の限界(独立役員審査 2026-08-03 指摘により報告 notes へ毎回開示する)**:

- PR 件名(``Merge pull request #N``)は GitHub API(``repos/<slug>/pulls/N``)で
  **実在+マージ済み+``merge_commit_sha`` が当該マージと一致**することまで照合する
  (:class:`PRVerifier`)。SHA 帰属を欠く照合は偽造コストを「番号を捏造する」から
  「実在番号をコピーする」へ下げるだけで有害である(独立役員審査 2026-08-04 重大-1)。
  トレーラの PR URL は**自リポジトリのもののみ**実在+マージ済みまで照合する
  (コミット単位の帰属は主張しない。他リポジトリ URL は権限外のため照合せず件数を開示)。
  **API に到達できない実行では従来どおり件名を信用する(fail-open)**— 週次監査を API
  障害で止めないため。ただし縮退した週は :func:`has_findings` で**所見あつかい**にし、
  embed に「照合不能 N 件(要手動確認)」を出す(緑は全照合が成立した週に限る — 重要-4)。
  私有リポジトリに未認証でアクセスすると存在する PR も 404 になるため、``repos/<slug>`` の
  到達性を先に確認し、到達できない場合は 404 を「不在」と解釈しない
- ``Approved:`` トレーラの参照は、DB 接続がある実行に限り ``governance.current_decisions`` と
  突合する(``decision:<id>`` は ID 一致、それ以外は ``proposal_ref`` 一致 — PR URL の承認記録が
  この経路で解決される)。否認済み(``effective_decision='vetoed'``)・却下・不在は承認として
  受理しない(独立役員審査 0021 C-5・重大-1)。**裸の数字は照合しない**(Issue 番号と区別
  できず偶然一致が fail-open になる — 重要-2)。DB に対応行の無い参照(Issue 決議など)は
  従来どおり存在検査までで、照合できなかったことを notes に載せる
- GitHub の squash マージ(``... (#N)`` 形式の単独コミット)は「マージ PR」と判定しない。
  本リポジトリの承認手続はマージコミット(``Merge pull request``)で行われている(批准 PR #32 が
  実例)。squash 併用を始める場合は判定の拡張が必要

**Approved トレーラ様式 v2(2026-08-04 独立役員審査 重大-1/重大-2 の恒久対策)**::

    Approved: <GitHub Issue/PR URL または decision:<id>> [reviewed=<40桁 SHA>]

``reviewed=`` は任意拡張であり、**付いていれば承継の範囲を「``reviewed`` の祖先」に限定**する
(:func:`reviewed_shas`)。独立審査・``#承認`` 通知の後にブランチへ積んだコミットは
``reviewed`` の祖先にならないため承継されず、従来どおり違反として列挙される(重大-2 の
「審査後 push の吸収」の封鎖)。以下はいずれも**様式不備として承継の起点にしない**
(fail-safe — 不備を「制限なし」に読み替えない): 40 桁 hex でない / リポジトリに実在しない /
**当該マージの第2親(= その PR のブランチ)の祖先でない**(他ブランチの SHA を書いて
「reviewed 限定」の表示だけを得る偽装の排除 — 独立役員審査 2026-08-04 重要-3)/
同一行に ``reviewed`` が複数ある(低-7)。
v1(``reviewed`` 無し)のトレーラは移行期の経過措置として従来どおり有効だが、それによる
承継の件数を notes に「reviewed 無しの承継 N 件」として開示する。

**v2 の限界(一部は 0029 + A-18-8 で縮小・残りは継続課題)**: ``reviewed`` はトレーラの
書き手の申告である。0029 が承認記録側に ``reviewed_sha`` を作り A-18-8 が両者を突合するように
なったため、**片側だけの改変**(マージ後にトレーラの ``reviewed`` を差し替える・別 PR の SHA を
複写する)は不一致として検出できる。**残る限界**: どちらの値も発効を起票した側が書くもので、
審査エージェント自身の署名は無い —— 同じ嘘を両方に書けば一致し、``reviewed=<マージ直前の
ブランチ head>`` と書いて記録側も同じ値にすれば被覆は v1 と同じになる。到達点は審査
エージェントの出力から機械的に埋まる経路であり、``ops/reminders.yaml`` の後続課題として残る。
v1 経過措置の打ち切りは ``trailer-v1-sunset``。

**PR 承継(2026-08-04 設計リード裁定)**: first-parent 上のマージ M が有効な ``Approved:``
トレーラを持つとき、M が main に持ち込んだコミット群(M の配下でまだ main に無かったもの)は
M の承認を承継し違反としない。PR 単位のみなし承認は独立審査を経た PR の内容全体に及ぶためで、
ブランチ内コミット(worktree の統合マージ含む)に個別トレーラを要求すると統合フローが構造的に
違反を量産し監査が形骸化する。承継は ``inherited`` として集計し報告に必ず出す(黙って消さない)。
承継の起点は「first-parent 上・件名がマージ形式・親2・**GitHub 上に実在しマージ済み**・
トレーラ有効・``reviewed`` があればその祖先」を満たすコミットのみで、トレーラの無い
PR マージ配下は従来どおり検査する。**起点のトレーラ有効性は必ず
:func:`trailer_approves` を通す** — 素の :func:`has_approval_trailer` で分岐すると、否認済みの
承認がブランチ全体へ承継され、否認照合が承継経路から迂回される。``conn`` が無い(照合不能な)
実行では形式的有効性のみで承継し、その件数を notes に開示する(黙って通さない)。

**既知違反の受容(acknowledged_findings)**: 是正不能な過去の違反(git 履歴の書き換えなしには
消せない evil merge 等)は ``config/governance.yaml`` の ``acknowledged_findings`` に
commit(完全 SHA)・paths(触れた保護パス集合)・理由・承認記録の参照とともに登録できる。
A-18-1 は一致した違反を ``violations`` から外す代わりに ``acknowledged`` へ移し、報告 embed の
別フィールドとして **必ず表示** する(黙って消さない)。一致は commit と paths の完全一致を要求し、
一致しない受容エントリは notes に「陳腐化」として開示する。

**evil merge 対策**: マージコミット自身のコンフリクト解消差分は ``git diff-tree --cc``
(全親と異なるファイルのみ列挙)で検査する。保護パスに触れる場合は **マージコミット自身の**
``Approved:`` トレーラを必須とし、PR マージ件名だけでは承認と見なさない(レビュー承認は
ブランチ内容に対するもので、マージ時に持ち込まれた差分をカバーしないため)。クリーンな
マージは ``--cc`` に現れないので誤検知しない。

git 操作は subprocess で行い、リポジトリパスは引数化してテスト可能にしている。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ryza import org
from ryza.bot import COLOR_FLASH, COLOR_NORMAL, DISCLAIMER
from ryza.bot.outbox import enqueue

log = logging.getLogger("ryza.audit.a18")

# 定款批准コミット(2026-08-03 発効・Merge pull request #32)。これ以前は監査対象外。
RATIFICATION_COMMIT = "c7af81ef85cc9f45bb7881ffc45769abfbc771dc"

# 全変更 PR 化ルール(2026-08-03 代表指示: 保護領域に限らずリポジトリへの全変更を PR 経由と
# する)の基準コミット = ルール採用日(A-18-4 実装時点)の origin/main HEAD。
#   4c7f6e9 "docs(tasks): T-017 FM エージェント第一陣(Ben・Jim)の実装指示書"
# これ以前の直 push は対象外(遡及しない)。GitHub 無料プラン(私有リポ)ではブランチ保護が
# 使えないため、本監査(A-18-4)がこのルールの執行点になる。
PR_RULE_BASELINE_COMMIT = "4c7f6e9daded18a3e9e903a80c87feba3576b52c"

GOVERNANCE_PATH = "config/governance.yaml"

# 既知の限界の常時開示(独立役員審査条件)。報告 embed の notes に毎回載せる。
STANDARD_DISCLOSURES: tuple[str, ...] = (
    "PR 件名(Merge pull request #N)は GitHub API で実在+マージ済み+merge_commit_sha が"
    "当該マージと一致することまで照合する。トレーラの自リポジトリ PR URL は実在+マージ済みまで"
    "(コミット単位の帰属は主張しない)。API 不達は fail-open し、件数を所見として報告する",
    "Approved トレーラは current_decisions と突合(decision:<id> は ID 一致・それ以外は "
    "proposal_ref 一致。否認済み・却下・不在は受理しない)。裸の数字と DB 外の承認記録"
    "(Issue 決議)は照合対象外",
    "マージのコンフリクト解消差分(evil merge)は --cc で検査し、保護パスに触れる場合は"
    "マージ自身の Approved トレーラを要求",
    "A-18-4 のマージ判定は親2限定+PR 件名+PR 実在照合(A-18-1 と同一の検査)— "
    "octopus マージは PR マージと見なさず違反にする",
    "Approved トレーラの reviewed=<sha40> は任意拡張。付いていれば承継は reviewed の祖先に"
    "限定され、無ければ PR マージ時点のブランチ全体に及ぶ(v1 経過措置 — 件数を開示する)",
    "A-18-8 の一致は「トレーラと承認記録という2つの申告が食い違っていない」ことのみを意味する。"
    "どちらも発効を起票した側が書く値であり、審査エージェント自身の署名は無い"
    "(同じ値を両方に書けば一致する)",
)

# 文書⇔config のバージョン突合ペア(A-18-2)。(文書, config, config 内の version キー)
VERSION_PAIRS: tuple[tuple[str, str], ...] = (
    ("docs/design/80-ips.md", "config/ips.yaml"),
    ("docs/design/06-constitution.md", "config/governance.yaml"),
)

# GitHub マージ PR のマージコミット件名(PR 番号を捕捉して実在照合に回す)。
_PR_MERGE_RE = re.compile(r"^Merge pull request #(\d+)")

# トレーラの任意拡張(``reviewed=<sha40>`` 等の key=value)。参照の後ろに空白区切りで並ぶ。
_TRAILER_ATTR_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)=(\S+)$")

# トレーラ様式 v2 の審査対象コミット(承継の上限)。
_REVIEWED_KEY = "reviewed"

# 解釈するキー / 「書いてもよいが解釈しない」キー(記入者の誤認を notes で正す — 低-10)。
_KNOWN_TRAILER_KEYS: frozenset[str] = frozenset({_REVIEWED_KEY})
_IGNORED_TRAILER_KEYS: frozenset[str] = frozenset({"mode", "notified"})

# GitHub PR の URL(トレーラ参照の実在照合に使う)。
_PR_URL_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)(?:[/#?].*)?$", re.IGNORECASE
)

# origin remote から owner/repo を取り出す(https / ssh の両形式)。
_ORIGIN_SLUG_RE = re.compile(r"github\.com[:/]+([^/]+)/(.+?)(?:\.git)?/?$", re.IGNORECASE)

# 見出し行のバージョン表記(例: 「# Ryza 投資方針書(IPS)v1.3」)。
_DOC_VERSION_RE = re.compile(r"v(\d+(?:\.\d+)+)")

# 受容記録の commit は 40 桁 hex の完全 SHA のみ(短縮 SHA は曖昧で誤一致・永久不一致を招く)。
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)

# Approved トレーラの参照が governance.decisions の ID を指す表記。接頭辞は必須
# (裸の数字は Issue 番号と区別できない — 独立役員審査 重要-2)。
_DECISION_REF_RE = re.compile(r"^decision:(\d+)$")

# 裸の数字(照合不能として開示する参照)。
_BARE_NUMBER_RE = re.compile(r"^\d+$")

# みなし承認の通知参照(governance/notices.py の NOTICE_REF_PREFIX と同値)。
# audit は governance を import せず定数を持つ(監査が被監査モジュールに依存しない)。
_NOTICE_REF_PREFIX = "outbox:"

# 通知が未配送のまま許容する時間。これを超えた deemed は「通知なき発効」として違反にする
# (独立役員審査 重要-3)。Bot の配送ループは 5 秒間隔なので、60 分は配送系の一時障害を
# 誤検知しない十分な余裕がありつつ、代表が気づかないまま1営業日が過ぎることを防ぐ。
UNNOTIFIED_DEEMED_MINUTES = 60

# A-18-7(保護領域 PR の承認記録漏れ)の基準コミット = 検査の採用日(2026-08-04)の
# origin/main HEAD。``--deemed`` CLI が入る前の PR マージは記録が無くて当然なので遡及しない
# (A-18-4 が PR_RULE_BASELINE_COMMIT を置いたのと同じ理由)。
DEEMED_RECORD_BASELINE_COMMIT = "649c4e292ef2ba78d931749e782ae1d3c42c3ada"

# 現決定 view の effective_decision のうち「発効している承認」。'vetoed' は含めない
# (否認された承認をトレーラの参照先として受理しない — 独立役員審査 0021 C-5)。
APPROVED_DECISIONS: frozenset[str] = frozenset({"approve", "deemed"})


# ────────────────────────────────────────────────────────────────────────────
# git ヘルパ(subprocess・リポジトリパス引数化)
# ────────────────────────────────────────────────────────────────────────────
def _git(repo: str | Path, *args: str) -> str:
    """git コマンドを実行し stdout を返す(失敗は CalledProcessError)。"""
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def _git_ok(repo: str | Path, *args: str) -> bool:
    """git コマンドの成否のみ返す(``merge-base --is-ancestor`` 用)。"""
    res = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    return res.returncode == 0


def _rev_list(repo: str | Path, since: str | None, *flags: str) -> list[str]:
    """``since..HEAD``(since=None なら全履歴)のコミット列を古い順に返す。"""
    rng = f"{since}..HEAD" if since else "HEAD"
    out = _git(repo, "rev-list", "--reverse", *flags, rng)
    return [ln for ln in out.splitlines() if ln]


# ────────────────────────────────────────────────────────────────────────────
# GitHub PR 実在照合(独立役員審査 2026-08-04 重大-1 の恒久対策)
#
# ``Merge pull request #N`` の件名も ``Approved: <PR URL>`` のトレーラも自己申告であり、
# 実在しない PR 番号を書くだけで承認を装える(審査 PoC: 存在しない #999999 の件名+架空 URL で
# Kill Switch 改変が violations=0 を通った)。PR 承継はこの偽造1件の爆風半径をブランチ全体へ
# 拡大するため、起点の実在照合が承継の前提条件になる。
#
# **fail-open の設計**: API 不達(gh 未導入・トークン無し・ネットワーク障害・レート制限)で
# 週次監査を止めると、監査が動かない週が「所見なし」と区別できなくなる(沈黙の多義化)。
# 到達できない場合は従来挙動(件名を信用)へ縮退し、**縮退した件数と理由を必ず notes に出す**。
# ────────────────────────────────────────────────────────────────────────────
#: GitHub API のトークンを探す環境変数(監査 VM のランナーは GIT_TOKEN を export する)。
_TOKEN_ENV_VARS: tuple[str, ...] = ("GH_TOKEN", "GITHUB_TOKEN", "GIT_TOKEN")

#: API 呼び出し1回あたりの上限秒数(週次バッチなので長すぎない値)。
GITHUB_API_TIMEOUT = 15.0


def origin_slug(repo_path: str | Path) -> str | None:
    """``origin`` remote から ``owner/repo`` を返す(GitHub でなければ None)。"""
    try:
        url = _git(repo_path, "remote", "get-url", "origin").strip()
    except (subprocess.CalledProcessError, OSError):
        return None
    m = _ORIGIN_SLUG_RE.search(url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _github_api_get(path: str, *, timeout: float = GITHUB_API_TIMEOUT) -> tuple[str, Any]:
    """GitHub API を GET する。戻り値は ``("ok", payload)`` / ``("not_found", None)`` /
    ``("error", 理由)``。

    ``gh`` があれば ``gh api``(認証を CLI に委ねられる)、無ければ ``urllib`` +
    環境変数のトークンを使う。監査 VM(ops/deploy-a18.sh)は ``gh`` を持たず
    ``GIT_TOKEN`` を export するため、両経路を用意しないと本番で常に fail-open になる。
    """
    if shutil.which("gh"):
        try:
            res = subprocess.run(
                ["gh", "api", path], capture_output=True, text=True, check=False, timeout=timeout
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            # 監査自身が API の遅延で落ちない(fail-open して縮退を開示する)。
            return "error", f"gh api 実行不能: {type(exc).__name__}"
        if res.returncode == 0:
            try:
                return "ok", json.loads(res.stdout or "{}")
            except json.JSONDecodeError:
                return "error", "gh api の応答が JSON でない"

        err = " ".join((res.stderr or "").split())
        if "404" in err or "Not Found" in err:
            return "not_found", None
        return "error", f"gh api 失敗: {err[:160] or f'exit={res.returncode}'}"

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ryza-a18-audit",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = next((os.environ[v] for v in _TOKEN_ENV_VARS if os.environ.get(v)), None)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"https://api.github.com/{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https 固定)
            return "ok", json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "not_found", None
        return "error", f"GitHub API HTTP {exc.code}"
    except Exception as exc:  # ネットワーク・DNS・タイムアウト・JSON 破損
        return "error", f"GitHub API 不達: {type(exc).__name__}"


@dataclass
class PRVerifier:
    """PR 番号の実在+マージ済みを GitHub API で照合する(結果はプロセス内でキャッシュ)。

    判定は ``ok`` / ``bad`` / ``unverifiable`` の3値。``bad`` のみが承認の否定であり、
    ``unverifiable`` は**従来挙動へ縮退**する(fail-open)。縮退の理由と件数は
    :meth:`disclosures` が返し、報告 notes に必ず載る。

    私有リポジトリに未認証でアクセスすると実在する PR も 404 になるため、初回に
    ``repos/<slug>`` の到達性を確認し、到達できないときは 404 を「不在」と読まない。
    この防御が無いと、トークンを失った週に全 PR が「実在しない」と判定され、
    監査が違反を大量生成して信用を失う。

    **SHA 帰属(独立役員審査 2026-08-04 重大-1)**: 「PR が実在する」は「この変更が承認された」
    ではない。番号だけを見る照合は偽造コストを「番号を捏造する」から「実在番号をコピーする」へ
    下げるだけで、緑をより信頼できるものに見せる分むしろ有害である。マージコミットを検査する
    経路では ``expected_merge_sha`` を渡し、API の ``merge_commit_sha`` との一致を必須にする
    (実在 PR 番号を件名に流用した自作マージを封じる)。
    """

    repo_path: str | Path | None = None
    slug: str | None = None
    #: API 呼び出し(テストは差し替えてネットワークに触れない)。None なら実 API。
    api_get: Callable[[str], tuple[str, Any]] | None = None
    enabled: bool = True
    #: 番号 → API 取得結果(``("ok", payload)`` / ``("not_found", None)`` / ``("error", 理由)``)。
    #: エラーもキャッシュする(レート制限時に同一番号へ再問い合わせしない — 審査 低-9)。
    _cache: dict[int, tuple[str, Any]] = field(default_factory=dict, repr=False)
    _unavailable: dict[str, int] = field(default_factory=dict, repr=False)
    _foreign: dict[str, int] = field(default_factory=dict, repr=False)
    _verified: int = field(default=0, repr=False)
    _reachable: bool | None = field(default=None, repr=False)
    _reach_reason: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.api_get is None:
            self.api_get = _github_api_get
        if self.slug is None and self.repo_path is not None:
            self.slug = origin_slug(self.repo_path)

    # ── 内部 ────────────────────────────────────────────────────────────────
    def _degrade(self, reason: str) -> tuple[str, str | None]:
        self._unavailable[reason] = self._unavailable.get(reason, 0) + 1
        return "unverifiable", reason

    def _unreachable_reason(self) -> str | None:
        """リポジトリ自体に到達できるか(初回のみ問い合わせ)。到達不能なら理由を返す。"""
        if self._reachable is None:
            status, detail = self.api_get(f"repos/{self.slug}")
            self._reachable = status == "ok"
            self._reach_reason = (
                None
                if self._reachable
                else (
                    f"リポジトリ {self.slug} に API でアクセスできない"
                    f"(認証不備・不達の可能性: {detail if status == 'error' else 'HTTP 404'})"
                )
            )
        return self._reach_reason

    def _fetch(self, number: int) -> tuple[str, Any]:
        """PR 1件の API 取得(成否ともキャッシュ)。"""
        if number not in self._cache:
            self._cache[number] = self.api_get(f"repos/{self.slug}/pulls/{number}")
        return self._cache[number]

    # ── 照合 ────────────────────────────────────────────────────────────────
    def check(self, number: int, expected_merge_sha: str | None = None) -> tuple[str, str | None]:
        """PR 番号1件を照合する。``("ok"|"bad"|"unverifiable", 理由)``。

        ``expected_merge_sha`` を渡すと、API の ``merge_commit_sha`` がそれと一致することを
        **必須**にする(実在 PR 番号の流用偽装の封鎖 — 独立役員審査 2026-08-04 重大-1)。
        """
        if not self.enabled:
            return self._degrade("GitHub PR 実在照合が無効化されている")
        if not self.slug:
            return self._degrade("origin が GitHub リポジトリでないため PR 番号を照合できない")
        unreachable = self._unreachable_reason()
        if unreachable:
            return self._degrade(unreachable)
        status, payload = self._fetch(number)
        if status == "not_found":
            return "bad", f"PR #{number} が GitHub に存在しない"
        if status != "ok":
            return self._degrade(f"PR #{number} の照合に失敗({payload})")
        payload = payload or {}
        if not payload.get("merged_at"):
            return "bad", f"PR #{number} は GitHub 上でマージされていない"
        if expected_merge_sha is not None:
            actual = str(payload.get("merge_commit_sha") or "").lower()
            if not actual:
                # SHA を確認できない応答では帰属を主張しない(fail-open して開示)。
                return self._degrade(f"PR #{number} の merge_commit_sha が API 応答に無い")
            if actual != expected_merge_sha.lower():
                return "bad", (
                    f"PR #{number} のマージコミットは {actual[:12]} であり本コミットではない"
                    "(実在 PR 番号の流用)"
                )
        self._verified += 1
        return "ok", None

    def check_ref(self, ref: str) -> tuple[str, str | None]:
        """トレーラ参照が**自リポジトリの** PR URL なら照合する。対象外なら ``("skip", None)``。

        他リポジトリの PR URL は照合できない(こちらの権限外)。黙って通すと
        「架空 URL は違反」という開示が実態と食い違うため、件数を数えて notes に出す
        (独立役員審査 2026-08-04 中-6)。
        """
        m = _PR_URL_RE.match(ref)
        if not m:
            return "skip", None
        if not self.slug or f"{m.group(1)}/{m.group(2)}".lower() != self.slug.lower():
            key = f"{m.group(1)}/{m.group(2)}"
            self._foreign[key] = self._foreign.get(key, 0) + 1
            return "skip", None
        return self.check(int(m.group(3)))

    # ── 集計 ────────────────────────────────────────────────────────────────
    @property
    def verified_count(self) -> int:
        """実在+マージ済み(帰属確認済みを含む)として通した参照の数。"""
        return self._verified

    @property
    def failed_open_count(self) -> int:
        """照合できず従来挙動へ縮退した参照の数(> 0 なら緑にしない — 重要-4)。"""
        return sum(self._unavailable.values())

    @property
    def failed_open_reasons(self) -> dict[str, int]:
        """縮退の理由 → 件数(報告 embed 用)。"""
        return dict(self._unavailable)

    def disclosures(self) -> list[str]:
        """照合できた件数と fail-open した件数・理由(報告 notes へ)。"""
        out = [
            f"GitHub PR 実在照合を実施できず fail-open した参照 {n} 件: {reason}"
            for reason, n in sorted(self._unavailable.items())
        ]
        out += [
            f"自リポジトリ外の PR URL {n} 件は照合対象外(権限外のため実在を確認していない): {slug}"
            for slug, n in sorted(self._foreign.items())
        ]
        if self._verified:
            out.append(
                f"GitHub PR 実在照合: {self._verified} 件を実在+マージ済み"
                "(マージ SHA 帰属を含む)として確認"
            )
        return out


def pr_number_from_subject(subject: str) -> int | None:
    """``Merge pull request #N`` 件名から PR 番号を返す(形式が違えば None)。"""
    m = _PR_MERGE_RE.match(subject)
    return int(m.group(1)) if m else None


def verified_pr_merge(
    subject: str, pr_verifier: PRVerifier | None, merge_sha: str | None = None
) -> tuple[bool, str | None]:
    """件名が PR マージ形式で、かつ(照合できるなら)**その PR のマージコミット**か。

    ``merge_sha`` を渡すと GitHub の ``merge_commit_sha`` と一致することまで要求する
    (実在 PR 番号を件名に流用した自作マージの封鎖 — 独立役員審査 2026-08-04 重大-1)。

    Returns:
        ``(PR マージとして扱うか, 扱わない理由)``。照合不能(API 不達)は従来どおり
        件名を信用する(fail-open。縮退件数は :meth:`PRVerifier.failed_open_count`)。
    """
    number = pr_number_from_subject(subject)
    if number is None:
        return False, None
    if pr_verifier is None:
        return True, None
    state, detail = pr_verifier.check(number, merge_sha)
    if state == "bad":
        return False, detail
    return True, None


# ────────────────────────────────────────────────────────────────────────────
# glob マッチ(protected_areas のパターン)
# ────────────────────────────────────────────────────────────────────────────
def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """protected_areas の glob を正規表現へ変換する。

    ``**`` は任意(``/`` を含む)、``*``/``?`` はパス区切りを跨がない。fnmatch は ``*`` が
    ``/`` を跨いでしまい ``migrations/*.sql`` が過剰マッチするため自前で変換する。
    """
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern[i : i + 2] == "**":
            parts.append(".*")
            i += 2
            if i < len(pattern) and pattern[i] == "/":
                i += 1  # "**/" は "**" と同義に丸める
        elif c == "*":
            parts.append("[^/]*")
            i += 1
        elif c == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def match_protected(files: list[str], patterns: list[re.Pattern[str]]) -> list[str]:
    """protected glob のいずれかに一致するファイルだけ返す。"""
    return [f for f in files if any(p.match(f) for p in patterns)]


# ────────────────────────────────────────────────────────────────────────────
# governance.yaml の読取
# ────────────────────────────────────────────────────────────────────────────
def load_governance(
    repo_path: str | Path, governance_path: str = GOVERNANCE_PATH
) -> dict[str, Any]:
    """governance.yaml を読み込む(A-18 の検査仕様はこのファイルが定義する)。"""
    text = (Path(repo_path) / governance_path).read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}


def protected_patterns(gov: dict[str, Any]) -> list[re.Pattern[str]]:
    return [glob_to_regex(str(e["path"])) for e in gov.get("protected_areas", [])]


# ────────────────────────────────────────────────────────────────────────────
# A-18-1 保護領域突合
# ────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TrailerLine:
    """``Approved:`` トレーラ1行の構造(様式 v2)。"""

    #: 承認記録の参照(GitHub Issue/PR URL または ``decision:<id>``)
    ref: str
    #: 任意拡張の ``key=value``(現在は ``reviewed`` のみ意味を持つ)
    attrs: dict[str, str]
    #: 解釈できなかったトークン(``key=value`` 形式でない余剰語)
    extras: tuple[str, ...] = ()
    #: 同一行で2回以上現れたキー(後勝ちで不備を握り潰さないため様式不備にする — 低-7)
    duplicates: tuple[str, ...] = ()


def approval_trailers(message: str, trailer: str = "Approved:") -> list[TrailerLine]:
    """``Approved: <参照> [key=value ...]`` トレーラ行を構造化して返す(様式 v2)。

    v1(参照のみ)は先頭トークンだけが埋まった :class:`TrailerLine` になり、従来と同じに
    扱われる。v2 の拡張(``reviewed=<sha40>``)は :func:`reviewed_shas` が読む。
    """
    pat = re.compile(rf"^{re.escape(trailer)}\s*(\S.*)$", re.MULTILINE)
    lines: list[TrailerLine] = []
    for m in pat.finditer(message):
        tokens = m.group(1).split()
        if not tokens:
            continue
        attrs: dict[str, str] = {}
        extras: list[str] = []
        duplicates: list[str] = []
        for tok in tokens[1:]:
            attr = _TRAILER_ATTR_RE.match(tok)
            if attr:
                key = attr.group(1).lower()
                if key in attrs:
                    duplicates.append(key)
                attrs[key] = attr.group(2)
            else:
                extras.append(tok)
        lines.append(
            TrailerLine(
                ref=tokens[0],
                attrs=attrs,
                extras=tuple(extras),
                duplicates=tuple(duplicates),
            )
        )
    return lines


def approval_trailer_refs(message: str, trailer: str = "Approved:") -> list[str]:
    """``Approved: <参照>`` トレーラ行の参照値を全て返す(定款第5条 C-5 様式)。

    参照は「GitHub Issue/PR URL または ``governance.decisions`` の ID」
    (config/governance.yaml の様式コメント)。1コミットに複数のトレーラを許すのは、
    複数の承認記録にまたがる変更(例: 独立役員審査 + 代表の明示承認)を表現するため。
    """
    return [line.ref for line in approval_trailers(message, trailer)]


def reviewed_shas(message: str, trailer: str = "Approved:") -> tuple[tuple[str, ...], str | None]:
    """トレーラの ``reviewed=<sha40>`` を全て返す。``(SHA 群, 様式不備の理由)``。

    様式 v2 の ``reviewed`` は「独立審査が実際に見たコミット」を固定する。承継はこの
    祖先に限定され、審査後にブランチへ積んだコミットは承継されない(重大-2)。
    **40 桁 hex でない値は様式不備**として理由を返し、呼び出し側は承継の起点に
    しない(fail-safe — 不備を「制限なし」に読み替えると v2 が抜け道になる)。
    """
    shas: list[str] = []
    for line in approval_trailers(message, trailer):
        if _REVIEWED_KEY in line.duplicates:
            # 後勝ちで解釈すると `reviewed=zzz reviewed=<valid>` が不備検出を無言で回避する
            # (独立役員審査 2026-08-04 低-7)。
            return (), "同一トレーラ行に reviewed が複数ある(どれが審査対象か確定できない)"
        value = line.attrs.get(_REVIEWED_KEY)
        if value is None:
            continue
        if not _FULL_SHA_RE.match(value):
            return (), f"reviewed={value} が 40 桁 hex の完全 SHA でない"
        shas.append(value.lower())
    return tuple(shas), None


def trailer_format_warnings(message: str, trailer: str = "Approved:") -> list[str]:
    """解釈されないキー・語を警告文にする(綴り誤りを黙って v1 扱いにしない — 低-10)。"""
    warnings: list[str] = []
    for line in approval_trailers(message, trailer):
        for key in line.attrs:
            if key in _IGNORED_TRAILER_KEYS:
                warnings.append(f"トレーラのキー '{key}=' は A-18 では解釈されない(記載は自由)")
            elif key not in _KNOWN_TRAILER_KEYS:
                warnings.append(
                    f"トレーラの未知キー '{key}='(綴り誤りの可能性 — 解釈されない)"
                )
        warnings += [f"トレーラの解釈できない語 '{tok}'" for tok in line.extras]
    return warnings


def has_approval_trailer(message: str, trailer: str = "Approved:") -> bool:
    """コミット本文に ``Approved: <参照>`` トレーラ行があるか(存在検査のみ)。"""
    return bool(approval_trailer_refs(message, trailer))


def decision_ref_id(ref: str) -> int | None:
    """トレーラ参照が ``decision:<id>`` 形式なら ``governance.decisions.id``、違えば None。

    **裸の数字は受理しない**(独立役員審査 重要-2)。``Approved: 42`` は GitHub Issue #42 の
    つもりで書かれうる表記であり、たまたま同じ ID の決定が存在すると**無関係な承認記録で
    照合が通る**(不在なら fail-closed だが、偶然一致は fail-open)。接頭辞を必須にすると
    偶然一致は起こらず、裸の数字は「照合できない参照」として notes に開示される。
    """
    m = _DECISION_REF_RE.match(ref)
    return int(m.group(1)) if m else None


@dataclass(frozen=True)
class TrailerVerdict:
    """``Approved:`` トレーラ参照の突合結果。"""

    accepted: bool
    #: 承認として受理できない参照の理由(``accepted=True`` でも空とは限らない — 軽微-10)
    problems: list[str]
    #: 照合できなかった参照(裸の数字・DB に対応行の無い URL)
    unverifiable: list[str]


def _verdict_for_ref(
    conn: Any, ref: str, pr_verifier: PRVerifier | None = None
) -> tuple[str, str | None]:
    """参照1件を突合し ``(判定, 理由)`` を返す。判定は ok / bad / unverifiable。

    突合は2段: (1) 参照が自リポジトリの PR URL なら GitHub API で実在+マージ済みを確認する
    (架空 URL の空手形を封じる — 重大-1)。(2) ``governance.current_decisions`` と突合し
    否認済み・却下・不在を弾く。``conn`` が無い実行では (2) を行わない。
    """
    from ryza.governance.decisions import current_decision, current_decision_by_id

    if pr_verifier is not None:
        pr_state, pr_detail = pr_verifier.check_ref(ref)
        if pr_state == "bad":
            return "bad", f"承認記録 '{ref}': {pr_detail}"

    if conn is None:
        # DB 照合はできない。PR URL の実在照合(上)だけが効く従来経路。
        return "unverifiable", None

    decision_id = decision_ref_id(ref)
    if decision_id is not None:
        row = current_decision_by_id(conn, decision_id)
        label = f"承認記録 id={decision_id}"
        if row is None:
            return "bad", f"{label} が governance.decisions に存在しない"
    elif _BARE_NUMBER_RE.match(ref):
        # 裸の数字は Issue 番号とも読めるため、決定 ID として解釈しない(重要-2)。
        return "unverifiable", f"参照 '{ref}' は照合不能(決定 ID なら decision:{ref} と書く)"
    else:
        # PR URL 等。deemed 記録の proposal_ref は PR URL そのものなので、ID 形式でなくても
        # proposal_ref 一致で解決できる(独立役員審査 重大-1: 本リポジトリの履歴は全件 URL で
        # あり、ID 形式だけを見る照合では 0021 C-5 の穴が実運用上ふさがらない)。
        row = current_decision(conn, ref)
        label = f"承認記録 '{ref}'"
        if row is None:
            # 承認記録が Issue 決議など DB 外にある場合はここに来る。従来どおり存在検査まで。
            return "unverifiable", None
    effective = str(row["effective_decision"])
    if effective in APPROVED_DECISIONS:
        return "ok", None
    if effective == "vetoed":
        return "bad", (
            f"{label} は代表により否認済み"
            f"(recorded={row['recorded_decision']} / 取消義務が発生している)"
        )
    return "bad", f"{label} は decision='{effective}' で承認ではない"


def trailer_approves(
    conn: Any | None,
    message: str,
    trailer: str = "Approved:",
    *,
    pr_verifier: PRVerifier | None = None,
) -> TrailerVerdict | None:
    """コミットメッセージのトレーラを検証する。トレーラが無ければ ``None``。

    「トレーラがあるか」(:func:`has_approval_trailer`)と「その承認が今も有効か」を1つに
    まとめた読み口。**承認の有効性を判定する箇所は必ずここを通す** — 素の
    ``has_approval_trailer`` で分岐すると、その経路だけ否認済み承認を受理する穴になる。

    ``conn`` が ``None`` なら承認記録との突合はできないので、従来どおりトレーラの存在を
    もって受理する(ただし ``pr_verifier`` があれば PR URL の実在照合だけは効く)。
    """
    refs = approval_trailer_refs(message, trailer)
    if not refs:
        return None
    return verify_decision_refs(conn, refs, pr_verifier=pr_verifier)


def verify_decision_refs(
    conn: Any | None, refs: list[str], *, pr_verifier: PRVerifier | None = None
) -> TrailerVerdict:
    """トレーラ参照を ``governance.current_decisions`` と突合する。

    受理の規則:

    - 解決できた参照のうち **1つでも有効な承認**(``approve`` / ``deemed``)があれば受理する。
      1コミットが複数の承認記録を挙げる様式(独立役員審査+代表承認など)を許すため
    - 解決できた参照が**全て無効**(否認済み・却下・不在)なら受理しない
    - 解決できた参照が**1つも無い**(裸の数字・DB 外の Issue 決議)なら、従来どおり
      トレーラの存在をもって受理し、照合できなかったことを ``unverifiable`` に残す

    受理した場合でも無効な参照は ``problems`` に残す(軽微-10)。「有効な承認と否認済みの
    承認を両方挙げているコミット」は、違反ではないが取消義務の検討対象であり、監査報告から
    消してよい事実ではない。

    **否認済みを受理しない**のが本関数の存在理由である(独立役員審査 0021 C-5)。
    ``governance.decisions`` を直読すると、代表が否認した承認を A-18 が承認として受理し、
    否認された変更(= 取消義務が発生している変更)が無承認変更として検出されない。
    現決定 view は否認を反映して ``vetoed`` を返すため、view 経由でのみ突合する。
    """
    problems: list[str] = []
    unverifiable: list[str] = []
    resolved = 0
    accepted = False
    for ref in refs:
        verdict, detail = _verdict_for_ref(conn, ref, pr_verifier)
        if verdict == "unverifiable":
            if detail:
                unverifiable.append(detail)
            continue
        resolved += 1
        if verdict == "ok":
            accepted = True
        elif detail:
            problems.append(detail)
    if resolved == 0:
        accepted = True  # 照合対象が無い = 従来どおり存在検査で受理
    return TrailerVerdict(accepted=accepted, problems=problems, unverifiable=unverifiable)


def _find_introducing_merge(
    repo: str | Path, sha: str, first_parent_merges: list[str]
) -> str | None:
    """コミット ``sha`` を main に持ち込んだ first-parent マージコミットを返す(古い順走査)。"""
    for m in first_parent_merges:
        if _git_ok(repo, "merge-base", "--is-ancestor", sha, m):
            return m
    return None


@dataclass(frozen=True)
class MergeOrigin:
    """承継の起点候補(main に取り込んだ first-parent マージ)の判定結果。"""

    sha: str
    subject: str
    #: 件名が PR マージ形式・親2・(照合できるなら)GitHub 上で実在しマージ済み
    is_pr: bool
    #: PR として扱わない理由(実在しない・未マージ)。件名が PR 形式でない場合は None
    not_pr_detail: str | None
    #: ``Approved:`` トレーラが有効(否認済み・却下・不在でない)
    approved: bool
    #: 様式 v2 の ``reviewed=<sha40>``(空 = v1 様式で範囲制限なし)
    reviewed: tuple[str, ...]
    #: 様式不備(承継の起点にしない fail-safe の理由)
    problem: str | None


def _merge_origin(
    repo: str | Path,
    merge: str,
    trailer: str,
    conn: Any | None,
    pr_verifier: PRVerifier | None,
) -> MergeOrigin:
    """承継の起点候補となるマージコミットを1件評価する(呼び出し側でキャッシュする)。"""
    subject = _git(repo, "log", "-1", "--format=%s", merge).strip()
    message = _git(repo, "log", "-1", "--format=%B", merge)
    parents = _git(repo, "log", "-1", "--format=%P", merge).split()
    two_parent = len(parents) == 2
    # octopus マージ(親3以上)は起点にしない。GitHub の PR マージは常に親2であり、octopus に
    # PR 件名を付けると複数ブランチの内容を1つの承認で通せてしまう(審査 2026-08-04 中-3)。
    # PR の実在に加え **merge_commit_sha == この マージ** まで要求する(番号流用の封鎖 — 重大-1)。
    pr_ok, not_pr_detail = verified_pr_merge(subject, pr_verifier, merge)
    verdict = trailer_approves(conn, message, trailer, pr_verifier=pr_verifier)
    reviewed, problem = reviewed_shas(message, trailer)
    if problem is None:
        missing = [r for r in reviewed if not _git_ok(repo, "cat-file", "-e", f"{r}^{{commit}}")]
        if missing:
            # 実在しない reviewed は祖先判定ができない。「制限なし」に読み替えず起点から外す。
            problem = f"reviewed={missing[0][:12]} がリポジトリに存在せず審査範囲を確定できない"
        elif reviewed and two_parent:
            # reviewed は **この PR のブランチ(第2親)の祖先**でなければならない。他ブランチの
            # SHA を書けば「reviewed 限定」と表示したまま実際には何も限定しない偽装ができる
            # (独立役員審査 2026-08-04 重要-3)。
            outside = [
                r for r in reviewed
                if not _git_ok(repo, "merge-base", "--is-ancestor", r, parents[1])
            ]
            if outside:
                problem = (
                    f"reviewed={outside[0][:12]} が当該 PR のブランチ(第2親 "
                    f"{parents[1][:12]})の祖先でない"
                )
        elif reviewed and not two_parent:
            problem = "reviewed 付きだが親2のマージでないため審査範囲を確定できない"
    return MergeOrigin(
        sha=merge,
        subject=subject,
        is_pr=pr_ok and two_parent,
        not_pr_detail=not_pr_detail,
        approved=verdict is not None and verdict.accepted,
        reviewed=reviewed,
        problem=problem,
    )


def _within_reviewed(repo: str | Path, sha: str, reviewed: tuple[str, ...]) -> bool:
    """``sha`` が ``reviewed`` のいずれかの祖先(自身を含む)か。"""
    return any(_git_ok(repo, "merge-base", "--is-ancestor", sha, r) for r in reviewed)


def check_protected_commits(
    repo_path: str | Path,
    gov: dict[str, Any],
    *,
    since_commit: str | None = RATIFICATION_COMMIT,
    conn: Any | None = None,
    pr_verifier: PRVerifier | None = None,
    format_notes: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, list[dict[str, Any]]]:
    """A-18-1: ``(違反, PR 承継で承認, 検査コミット数, トレーラ所見)`` を返す。

    承認とみなす条件(定款附則):
      (a) コミット本文の ``Approved:`` トレーラ。``conn`` が与えられれば
          :func:`verify_decision_refs` で参照(``decision:<id>`` / ``proposal_ref`` 一致)を
          実在照合し、**否認済み・却下・不在は承認と見なさない**
      (b) GitHub マージ PR 経由 = ``Merge pull request #N`` マージコミット(**親2**・
          ``pr_verifier`` があれば **PR #N が実在しマージ済み**)の配下で main に到達
      (c) **PR 承継**: 有効な ``Approved:`` トレーラを持つ first-parent 上の PR マージ M
          (**親2**・実在)が main に持ち込んだコミット群は、M の承認を承継する(下記)。
          M のトレーラが様式 v2(``reviewed=<sha40>``)なら、承継は **reviewed の祖先**に
          限られ、審査後に積まれたコミットは (b)・(c) のいずれでも救済されない
    ``since_commit``(批准コミット)以前のコミットは ``rev-list since..HEAD`` により対象外。

    **トレーラが無効なら (b)・(c) では救済しない**: 「この承認記録で承認された」と明示的に
    主張しているコミットが、その記録の否認によって主張を失った場合、PR 経由であることを
    理由に承認扱いへ戻すと否認が監査から見えなくなる。否認は取消義務(定款第3条)を
    生じさせるので、取消されるまでは無承認変更として列挙されるのが正しい。承継の起点判定も
    同じ理由で :func:`trailer_approves` を通す(素の存在検査で分岐すると、否認済みの承認が
    ブランチ全体へ承継され照合が迂回される)。

    4つ目の戻り値は「受理はしたが問題のある参照」(有効な承認と否認済みの承認を併記した
    コミット等)。違反ではないが取消義務の検討対象なので報告から落とさない(軽微-10)。
    """
    repo = str(repo_path)
    if since_commit and not _git_ok(repo, "cat-file", "-e", f"{since_commit}^{{commit}}"):
        raise ValueError(f"発効基準コミットがリポジトリに存在しない: {since_commit}")

    patterns = protected_patterns(gov)
    trailer = str(gov.get("approval_trailer") or "Approved:")
    commits = _rev_list(repo, since_commit)
    first_parent = set(_rev_list(repo, since_commit, "--first-parent"))
    fp_merges = _rev_list(repo, since_commit, "--first-parent", "--merges")

    violations: list[dict[str, Any]] = []
    inherited: list[dict[str, Any]] = []
    trailer_findings: list[dict[str, Any]] = []
    origins: dict[str, MergeOrigin] = {}  # 起点マージの評価キャッシュ(API 呼び出しを減らす)
    for sha in commits:
        parents = _git(repo, "log", "-1", "--format=%P", sha).split()
        is_merge = len(parents) > 1
        if is_merge:
            # evil merge 対策: マージ自身のコンフリクト解消差分(全親と異なるファイルのみ)。
            # クリーンなマージは --cc に現れない。
            diff_args = ("diff-tree", "--cc", "--no-commit-id", "--name-only", sha)
        else:
            diff_args = ("diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha)
        files = [ln for ln in _git(repo, *diff_args).splitlines() if ln]
        touched = match_protected(files, patterns)
        if not touched:
            continue

        message = _git(repo, "log", "-1", "--format=%B", sha)
        if format_notes is not None:
            # 解釈されないキー・語(綴り誤り)は黙って v1 扱いに落とさず注記に出す(低-10)。
            format_notes += [
                f"{w}: `{sha[:12]}`" for w in trailer_format_warnings(message, trailer)
            ]
        # 承認の有効性判定は trailer_approves に集約する。PR 承継のように「別のコミットの
        # トレーラで承認する」規則も必ずこの関数を通す(素の has_approval_trailer で分岐すると
        # その経路だけ否認済み承認を受理する穴になる)。
        verdict = trailer_approves(conn, message, trailer, pr_verifier=pr_verifier)
        trailer_reason: str | None = None
        if verdict is not None:
            if verdict.accepted:
                # 受理はしたが否認済みの参照を併記している(軽微-10)、または照合できない
                # 参照(裸の数字・DB 外の Issue 決議)を含む(重要-2)。どちらも違反では
                # ないが、報告から落とすと「照合済み」と「照合できていない」が混ざる。
                if verdict.problems or verdict.unverifiable:
                    trailer_findings.append(
                        {
                            "commit": sha[:12],
                            "subject": _git(repo, "log", "-1", "--format=%s", sha).strip(),
                            "problems": verdict.problems,
                            "unverifiable": verdict.unverifiable,
                        }
                    )
                continue
            trailer_reason = (
                "Approved トレーラの承認記録が有効でない: " + "; ".join(verdict.problems)
            )

        # 承継の起点候補: sha を main に持ち込んだ first-parent 上のマージ M。
        # sha 自身が first-parent 上にある場合(= main への直接の到達点)は承継しない。
        # 自分のトレーラが無効(否認済み等)なコミットは (b)・(c) で救済しない。
        merge = (
            None
            if trailer_reason is not None or sha in first_parent
            else _find_introducing_merge(repo, sha, fp_merges)
        )
        origin_reason: str | None = None
        if merge is not None:
            # 起点候補の探索自体は全 first-parent マージに対して行う — octopus や偽 PR を
            # 探索対象から外すと「持ち込んだマージ」が後続の別 PR に誤帰属するため。
            # 起点のトレーラは必ず trailer_approves を通す(否認済みの PR トレーラで
            # ブランチ全体を承継させると、否認照合が承継経路から迂回される)。
            if merge not in origins:
                origins[merge] = _merge_origin(repo, merge, trailer, conn, pr_verifier)
            origin = origins[merge]
            if origin.is_pr and origin.problem is not None:
                # 様式不備は「制限なし」ではなく「起点にしない」— fail-safe(重大-2 の恒久対策)。
                origin_reason = (
                    f"承継の起点 PR `{merge[:12]}` の Approved トレーラが様式不備: "
                    f"{origin.problem}"
                )
            elif origin.is_pr and origin.reviewed and not _within_reviewed(
                repo, sha, origin.reviewed
            ):
                # 様式 v2: 承継は reviewed の祖先に限る。独立審査・#承認 通知の**後**に
                # 積まれたコミットは同じトレーラでは承認されない(重大-2「審査後 push の吸収」)。
                origin_reason = (
                    f"PR `{merge[:12]}` の Approved トレーラは reviewed="
                    f"{origin.reviewed[0][:12]} を審査対象としており、本コミットはその祖先でない"
                    "(審査後に積まれた変更は承継しない)"
                )
            elif origin.not_pr_detail is not None:
                # 件名は PR マージだが GitHub 上に実在しない/未マージ(重大-1 の偽トレーラ)。
                origin_reason = (
                    f"持ち込んだマージ `{merge[:12]}` の PR 件名が GitHub と一致しない: "
                    f"{origin.not_pr_detail}"
                )
            elif origin.is_pr and not is_merge:
                continue  # (b) マージ PR 経由のブランチ内コミット = 代表承認(附則・従来どおり)
            elif origin.is_pr and origin.approved:
                # (c) PR 承継。PR 単位のみなし承認(定款第3条 v0.4)は独立審査を経た PR の
                # 内容全体に及ぶ。ブランチ内コミット(worktree の統合マージ含む)に個別
                # トレーラを要求すると、統合フローが構造的に違反を量産し監査が形骸化する
                # (2026-08-04 設計リード裁定・g-a18 審査 C-3 の恒久対策)。
                # 承継の起点は「first-parent 上・件名がマージ形式・親2・PR 実在・トレーラ有効・
                # reviewed があればその祖先」に限る。トレーラの無い PR マージ(#56 等の初期)は
                # 承継させない。conn が無い実行では形式的有効性のみで承継する。その事実は
                # decision_verified=False として持ち、報告 notes で件数を開示する。
                inherited.append(
                    {
                        "commit": sha[:12],
                        "commit_full": sha,
                        "subject": _git(repo, "log", "-1", "--format=%s", sha).strip(),
                        "files": touched,
                        "merge": merge[:12],
                        "merge_subject": origin.subject,
                        "decision_verified": conn is not None,
                        # v1(reviewed 無し)の承継は移行期の経過措置。件数を notes で開示する。
                        "reviewed_scoped": bool(origin.reviewed),
                        "reviewed": origin.reviewed[0] if origin.reviewed else None,
                    }
                )
                continue

        if trailer_reason is not None:
            reason = trailer_reason
        elif origin_reason is not None:
            reason = origin_reason
        elif is_merge:
            # マージ自身の差分は PR 件名では承認と見なさない(レビューはブランチ内容に対する
            # もので、マージ時に持ち込まれた差分をカバーしない)。トレーラ必須。
            # 承継が効くのは「トレーラ有効な PR マージが持ち込んだ」ブランチ内マージのみで、
            # first-parent 上のマージ自身(件名偽装の余地がある経路)は従来どおり検査する。
            reason = "マージ自身のコンフリクト解消差分(evil merge)で Approved トレーラなし"
        elif sha not in first_parent:
            reason = "マージ経由だが PR マージコミット(親2)が確認できない"
        else:
            reason = "main への直接コミットで Approved トレーラなし"
        violations.append(
            {
                "commit": sha[:12],
                # 受容記録(acknowledged_findings)の突合は完全 SHA で行う(短縮形は曖昧)。
                "commit_full": sha,
                "subject": _git(repo, "log", "-1", "--format=%s", sha).strip(),
                "files": touched,
                "reason": reason,
            }
        )
    return violations, inherited, len(commits), trailer_findings


# ────────────────────────────────────────────────────────────────────────────
# A-18-1 既知違反の受容(acknowledged_findings — 独立役員審査 C-3)
# ────────────────────────────────────────────────────────────────────────────
def _ack_key(commit: str, files: list[str] | tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    """受容記録の一致キー: (完全 SHA, 保護パスの正規化集合)。順序差では外れない。"""
    return commit.strip().lower(), tuple(sorted({str(f).strip() for f in files}))


def acknowledged_index(
    gov: dict[str, Any],
) -> tuple[dict[tuple[str, tuple[str, ...]], dict[str, Any]], list[str]]:
    """``acknowledged_findings`` を(一致キー → エントリ の索引, 無効エントリの注記)に変換する。

    無効(commit / paths 欠落・40 桁 hex でない SHA)なエントリは索引に入れず、注記で開示する。
    黙って落とすと運用者が「受容できた」と誤認する(独立役員審査 2026-08-04 低-7)。
    """
    index: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    notes: list[str] = []
    for entry in gov.get("acknowledged_findings") or []:
        commit = str(entry.get("commit", "")).strip()
        paths = entry.get("paths") or []
        if not commit or not paths:
            # 不完全なエントリは受容として効かせない(fail-safe = 違反のまま出す)。
            notes.append(
                f"acknowledged_findings のエントリが無効(commit / paths のいずれかが欠落): "
                f"{commit or '(commit なし)'}"
            )
            continue
        if not _FULL_SHA_RE.match(commit):
            notes.append(
                f"acknowledged_findings のエントリが無効(40 桁 hex の完全 SHA が必要 — "
                f"短縮 SHA は曖昧なため受け付けない): {commit}"
            )
            continue
        index[_ack_key(commit, paths)] = entry
    return index, notes


def partition_acknowledged(
    violations: list[dict[str, Any]], gov: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """違反を(未受容, 受容済み, 陳腐化した受容エントリの注記)に分割する。

    一致条件は **完全 SHA と保護パス集合の完全一致**。片方でも違えば受容は効かず、違反として
    残る(将来の別の違反や、保護領域追加でパス集合が変わったケースを巻き込まない)。
    受容済みは捨てずに返し、報告側で必ず可視化する(黙って消さない)。
    """
    index, notes = acknowledged_index(gov)
    matched: set[tuple[str, tuple[str, ...]]] = set()
    unacknowledged: list[dict[str, Any]] = []
    acknowledged: list[dict[str, Any]] = []
    for v in violations:
        key = _ack_key(str(v.get("commit_full") or v["commit"]), v["files"])
        entry = index.get(key)
        if entry is None:
            unacknowledged.append(v)
            continue
        matched.add(key)
        acknowledged.append(
            {
                **v,
                "acknowledged_on": entry.get("acknowledged_on"),
                "approval_ref": entry.get("approval_ref"),
                "ack_reason": entry.get("reason"),
            }
        )
    notes += [
        f"acknowledged_findings のエントリが一致する違反を持たない(陳腐化・SHA/パスの誤り"
        f"の可能性): {key[0][:12]}({', '.join(key[1])})"
        for key in index
        if key not in matched
    ]
    return unacknowledged, acknowledged, notes


# ────────────────────────────────────────────────────────────────────────────
# A-18-2 文書⇔config 整合
# ────────────────────────────────────────────────────────────────────────────
def doc_version(path: Path) -> str | None:
    """文書先頭の見出し行から ``vX.Y`` を抽出する(無ければ None)。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("#"):
            m = _DOC_VERSION_RE.search(line)
            return m.group(1) if m else None
    return None


def config_version(path: Path) -> str | None:
    """機械可読 config の ``version`` キーを返す(無ければ None)。"""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return None
    v = doc.get("version")
    return None if v is None else str(v).lstrip("v")


def check_versions(
    repo_path: str | Path,
    pairs: tuple[tuple[str, str], ...] = VERSION_PAIRS,
) -> list[dict[str, Any]]:
    """A-18-2: 発効文書と機械可読 config のバージョン不一致を列挙する。"""
    root = Path(repo_path)
    mismatches: list[dict[str, Any]] = []
    for doc_rel, cfg_rel in pairs:
        dv = doc_version(root / doc_rel)
        cv = config_version(root / cfg_rel)
        if dv is None or cv is None or dv != cv:
            reason = "バージョン表記が取得できない" if None in (dv, cv) else "バージョン不一致"
            mismatches.append(
                {
                    "doc": doc_rel,
                    "config": cfg_rel,
                    "doc_version": dv,
                    "config_version": cv,
                    "reason": reason,
                }
            )
    return mismatches


# ────────────────────────────────────────────────────────────────────────────
# A-18-3 宣言棚卸し
# ────────────────────────────────────────────────────────────────────────────
def list_declarations(gov: dict[str, Any]) -> list[dict[str, Any]]:
    """controls のうち enforcement: declaration の項目(執行点なし)を列挙する。"""
    return [
        {"rule": c.get("rule"), "verification": c.get("verification")}
        for c in gov.get("controls", [])
        if c.get("enforcement") == "declaration"
    ]


def _coverage_notes(gov: dict[str, Any]) -> list[str]:
    """protected_areas の登録漏れ(governance.yaml のコメントで予告された項目)を注記する。"""
    notes: list[str] = []
    paths = [str(e.get("path", "")) for e in gov.get("protected_areas", [])]
    if not any(p.startswith("src/ryza/audit") for p in paths):
        notes.append(
            "protected_areas に監査部門コード(src/ryza/audit)が未登録(定款第5条。統合時に追記)"
        )
    return notes


def _staleness_note(repo_path: str | Path) -> list[str]:
    """検査対象 checkout の鮮度検査(read-only: fetch はしない)。

    ``origin/main`` の追跡 ref が存在し、HEAD がそれを含まない(= 手元の追跡情報より古い
    履歴を監査している)場合に警告する。追跡 ref 自体が古い可能性は検出できないことも含めて
    注記する。追跡 ref が無い環境(一時リポジトリ等)は注記なし。
    """
    if not _git_ok(repo_path, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main"):
        return []
    if _git_ok(repo_path, "merge-base", "--is-ancestor", "origin/main", "HEAD"):
        return []
    return [
        "stale checkout: HEAD が origin/main を含まない — 最新でない履歴を監査している可能性"
        "(read-only 原則により fetch はしない。checkout の更新は運用側で)"
    ]


# ────────────────────────────────────────────────────────────────────────────
# A-18-4 全変更 PR 化(直 push 検査)
# ────────────────────────────────────────────────────────────────────────────
def check_direct_pushes(
    repo_path: str | Path,
    *,
    since_commit: str | None = PR_RULE_BASELINE_COMMIT,
    pr_verifier: PRVerifier | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """A-18-4: main への直 push・非 PR マージの一覧と、検査した first-parent コミット数を返す。

    基準コミット(全変更 PR 化ルール採用日の main HEAD)以降の first-parent 履歴で、
    (a) マージコミットでないコミット = 直 push、(b) 件名が PR マージ形式でない、または
    ``pr_verifier`` の照合で **PR が実在しない/未マージ**のマージコミット = 非 PR マージ、
    を違反とする。保護領域か否かは問わず、例外も設けない(``Approved:`` トレーラ付き
    直 push も違反 — 全 PR 化ルールに例外なし)。基準コミット以前は
    ``rev-list since..HEAD`` により対象外。
    """
    repo = str(repo_path)
    if since_commit and not _git_ok(repo, "cat-file", "-e", f"{since_commit}^{{commit}}"):
        raise ValueError(f"全変更 PR 化の基準コミットがリポジトリに存在しない: {since_commit}")

    fp_commits = _rev_list(repo, since_commit, "--first-parent")
    violations: list[dict[str, Any]] = []
    for sha in fp_commits:
        parents = _git(repo, "log", "-1", "--format=%P", sha).split()
        if len(parents) > 1:
            subject = _git(repo, "log", "-1", "--format=%s", sha)
            # 親3以上(octopus)は GitHub の PR マージではありえない。件名だけで通すと
            # 実在 PR 件名を付けた octopus が A-18-4 を素通りする(審査 2026-08-04 中-5)。
            is_two_parent = len(parents) == 2
            is_pr, not_pr_detail = verified_pr_merge(subject, pr_verifier, sha)
            if is_pr and is_two_parent:
                continue  # PR マージコミット(API 不達なら件名を信用する fail-open)
            reason = "main への非 PR マージ(全変更 PR 化ルール違反 — 例外なし)"
            if not is_two_parent:
                reason = (
                    f"octopus マージ(親{len(parents)})は PR マージではない"
                    "(全変更 PR 化ルール違反)"
                )
            elif not_pr_detail is not None:
                reason = f"PR マージ件名だが GitHub と一致しない: {not_pr_detail}"
            # マージが main に持ち込んだ内容 = first parent との差分を列挙する。
            diff_args = (
                "diff-tree", "--no-commit-id", "--name-only", "-r", "-m", "--first-parent", sha
            )
        else:
            reason = "main への直 push(全変更 PR 化ルール違反 — 例外なし)"
            diff_args = ("diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha)
        files = [ln for ln in _git(repo, *diff_args).splitlines() if ln]
        violations.append(
            {
                "commit": sha[:12],
                "subject": _git(repo, "log", "-1", "--format=%s", sha).strip(),
                "files": files,
                "reason": reason,
            }
        )
    return violations, len(fp_commits)


# ────────────────────────────────────────────────────────────────────────────
# A-18-5 通知なき発効(未配送のみなし承認)
# ────────────────────────────────────────────────────────────────────────────
def check_unnotified_deemed(
    conn: Any, *, max_delay_minutes: int = UNNOTIFIED_DEEMED_MINUTES
) -> tuple[list[dict[str, Any]], int]:
    """A-18-5: 発効済みなのに通知が届いていないみなし承認を列挙する。

    定款第3条はみなし承認を「``#承認`` への通知と同時に発効」と定め、
    ``config/governance.yaml`` の ``deemed_approval.unnotified_change: violation`` は
    通知なき発効を無承認変更として扱う。``governance/notices.py`` は記録と
    **outbox への投入**を同一トランザクションに置くが、投入は配送ではない
    (独立役員審査 重要-3)。配送が止まっていれば「発効したが誰も知らない」状態が続く。
    したがって監査側で滞留を検出する: ``notice_ref``(``outbox:<id>``)の指す行が
    ``max_delay_minutes`` を超えて ``sent_at IS NULL`` なら違反として報告する。

    Returns:
        ``(所見, 通知参照の形式が outbox: でない deemed 行の数)``。後者は本検査で
        追跡できない記録(手作業で ``discord://`` 等を入れたもの)であり、notes に開示する。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, proposal_ref, decided_at, channel_msg_id
            FROM governance.decisions
            WHERE decision = 'deemed'
            ORDER BY id
            """
        )
        deemed_rows = cur.fetchall()

    by_outbox: dict[int, tuple[int, str, Any]] = {}
    untracked = 0
    for decision_id, proposal_ref, decided_at, notice_ref in deemed_rows:
        raw = (notice_ref or "")[len(_NOTICE_REF_PREFIX):] if notice_ref else ""
        if not (notice_ref or "").startswith(_NOTICE_REF_PREFIX) or not raw.isdigit():
            untracked += 1
            continue
        by_outbox[int(raw)] = (decision_id, proposal_ref, decided_at)
    if not by_outbox:
        return [], untracked

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, sent_at,
                   EXTRACT(EPOCH FROM (now() - created_at)) / 60 AS waiting_minutes
            FROM press.outbox
            WHERE id = ANY(%s)
            """,
            (list(by_outbox),),
        )
        outbox_rows = {r[0]: (r[1], float(r[2])) for r in cur.fetchall()}

    findings: list[dict[str, Any]] = []
    for outbox_id, (decision_id, proposal_ref, _decided_at) in sorted(by_outbox.items()):
        row = outbox_rows.get(outbox_id)
        if row is None:
            # 記録は残っているのに通知行が消えている = 通知の証跡が無い。
            findings.append(
                {
                    "decision_id": decision_id,
                    "proposal_ref": proposal_ref,
                    "notice_ref": f"{_NOTICE_REF_PREFIX}{outbox_id}",
                    "waiting_minutes": None,
                    "reason": "通知(press.outbox)の行が存在しない",
                }
            )
            continue
        sent_at, waiting_minutes = row
        if sent_at is not None or waiting_minutes <= max_delay_minutes:
            continue
        findings.append(
            {
                "decision_id": decision_id,
                "proposal_ref": proposal_ref,
                "notice_ref": f"{_NOTICE_REF_PREFIX}{outbox_id}",
                "waiting_minutes": round(waiting_minutes, 1),
                "reason": f"通知が未配送のまま {max_delay_minutes} 分を超過(通知なき発効)",
            }
        )
    return findings, untracked


# ────────────────────────────────────────────────────────────────────────────
# A-18-6 決議の批判経由(形骸化の監査)
# ────────────────────────────────────────────────────────────────────────────
def check_resolution_bypass(conn: Any) -> dict[str, Any]:
    """A-18-6: 「批判を経ない決議」の直近件数・連続数を集計する(read-only)。

    集計そのものは ``governance.boardroom`` の統制ロジック(走査窓・閾値・表示行)を
    再利用する — 監査側で閾値を再定義すると、UI・監査の二重定義が静かにずれる。
    ``boardroom`` は psycopg を直接使うため遅延インポートする(本モジュールは git と
    設定ファイルだけで動く実行経路を持つ)。

    Returns:
        ``line``(運用レポート1行)と内訳・``alert``(⚠ 条件に達したか)を持つ dict。
    """
    from ryza.governance.boardroom import (
        confirmation_status_line,
        resolution_confirmation_stats,
    )

    stats = resolution_confirmation_stats(conn)
    return {
        "scanned": stats.scanned,
        "confirmed": stats.confirmed,
        "undetermined": stats.undetermined,
        "bypassed": stats.bypassed,
        "streak": stats.streak,
        "alert": stats.alert,
        "line": confirmation_status_line(stats),
    }


# ────────────────────────────────────────────────────────────────────────────
# A-18-7 保護領域 PR の承認記録漏れ(みなし承認の起票忘れ)
#
# みなし承認の発効通知は ``python -m ryza.governance.decisions --deemed`` を**人が叩く**
# ことでしか出ない(GitHub イベント受信基盤が無く、自動起票は未実装 — 独立役員審査 中-7)。
# 叩き忘れれば、保護領域の変更が #承認 への通知なしにマージされる = 定款第3条の
# 「通知と同時に発効」が満たされないまま変更が入る。A-18-5 は「記録はあるが配送されて
# いない」を検出するが、**そもそも記録が無い**ケースはどの検査にも掛からなかった。
# 本検査はその穴を塞ぐ: 保護領域に触れた PR マージに対応する承認記録が DB に無ければ列挙する。
# ────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class UnrecordedPRScan:
    """A-18-7 の走査結果。

    ``checked`` は**緑の分母**(検査した保護領域 PR マージ数)である。件数を持たない緑は
    「漏れが無い」と「そもそも1件も見ていない」を同じ表示にする —— squash マージへ移行して
    ``Merge pull request`` 件名が消えれば、検査は静かに 0 件走査の ✅ を出し続ける
    (後続配線審査 後-4。PR 実在照合が縮退件数を必ず出す流儀と同じ)。

    ``repo_slug`` が None の実行は ``proposal_ref`` のリポジトリ部分を照合できず、
    PR 番号の末尾一致までしか見ていない。黙って緑にせず notes に開示する。
    """

    findings: list[dict[str, Any]]
    checked: int
    repo_slug: str | None


def pr_proposal_ref(slug: str, pr_number: int) -> str:
    """``--deemed`` が記録する PR の ``proposal_ref``(= GitHub の ``html_url``)。"""
    return f"https://github.com/{slug}/pull/{pr_number}"


def _same_ref(a: Any, b: Any) -> bool:
    """``proposal_ref`` の同一性(末尾スラッシュと大小文字の揺れだけ吸収する)。"""
    return str(a or "").strip().rstrip("/").lower() == str(b or "").strip().rstrip("/").lower()


def _resolve_trailer_decision(conn: Any, ref: str) -> dict[str, Any] | None:
    """トレーラ参照から決定行を引く(引けなければ None)。

    否認済みでも行は返す。本検査が見るのは**記録の有無と帰属**であって有効性ではない
    —— 否認済みの承認を参照するコミットは A-18-1 が既に無承認変更として列挙しており、
    ここで二重に鳴らすと「CLI の叩き忘れ」という本検査の信号が別種の違反に埋もれる。
    """
    from ryza.governance.decisions import current_decision, current_decision_by_id

    decision_id = decision_ref_id(ref)
    if decision_id is not None:
        return current_decision_by_id(conn, decision_id)
    if _BARE_NUMBER_RE.match(ref):
        return None  # 裸の数字は決定 ID として解釈しない(重要-2)
    return current_decision(conn, ref)


def _attributed_to_pr(row: dict[str, Any], expected: str | None, pr_number: int) -> bool:
    """決定行がこの PR に帰属するか。

    ``expected``(自リポの PR URL)が分かる実行では**完全一致のみ**を帰属とする。
    ``origin`` を解決できない実行(remote の無い一時リポジトリ等)は末尾一致まで落とし、
    リポジトリ部分を照合できていないことを報告の notes に開示する。
    """
    if expected is not None:
        return _same_ref(row["proposal_ref"], expected)
    return str(row["proposal_ref"] or "").rstrip("/").endswith(f"/pull/{pr_number}")


def decisions_for_pr_number(conn: Any, pr_number: int) -> list[dict[str, Any]]:
    """``proposal_ref`` の末尾が ``/pull/<N>`` の決定を全て返す(リポジトリ部分は問わない)。

    **末尾一致は帰属の判定ではなく所見の材料である**(後続配線審査 後-5)。他リポジトリの
    ``/pull/610`` の記録で自リポ #610 を緑にすると、fail-closed の検査に fail-open が
    1箇所入る。帰属は :func:`pr_proposal_ref` との完全一致で判定し、末尾だけ一致する行は
    「別リポジトリの記録」として所見の理由に出す。``LIKE '%%/pull/<N>'`` は末尾固定なので
    ``/pull/1`` が ``/pull/12`` に誤一致しない。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT decision_id, proposal_ref, recorded_decision, effective_decision "
            "FROM governance.current_decisions "
            "WHERE proposal_ref LIKE %s ORDER BY decision_id",
            (f"%/pull/{pr_number}",),
        )
        rows = cur.fetchall()
        columns = [d.name for d in cur.description]
    return [dict(zip(columns, row, strict=True)) for row in rows]


def check_unrecorded_protected_prs(
    repo_path: str | Path,
    gov: dict[str, Any],
    conn: Any,
    *,
    since_commit: str | None = DEEMED_RECORD_BASELINE_COMMIT,
    repo_slug: str | None = None,
) -> UnrecordedPRScan:
    """A-18-7: 保護領域 PR のうち、**その PR に帰属する**承認記録が DB に無いものを列挙する。

    帰属の判定は ``proposal_ref == https://github.com/<slug>/pull/<N>`` の完全一致1本である。
    トレーラの参照は、そこから引いた決定の ``proposal_ref`` がこの PR を指すときだけ帰属と
    認める —— 「参照先の決定が実在するか」だけを見ると、**別 PR の承認記録で緑になる**
    (後続配線審査 後-3: PR #601 だけ ``--deemed`` して #601/#602 に同じトレーラを複写すると
    #602 が所見ゼロで通る)。追い PR へのトレーラ複写は事故で起きやすい経路であり、
    「承認記録がある」ではなく「**この変更の**承認記録がある」を検査の意味にする。

    検査対象は first-parent 上の PR マージ(件名が ``Merge pull request``・**親2**)で、
    ``-m --first-parent`` の差分(= main に持ち込まれた内容)が保護領域に触れるもの。
    非 PR マージ・直 push は A-18-4 の担当なのでここでは扱わない。

    Args:
        repo_slug: ``owner/name``。省略時は ``origin`` remote から解決する
            (:func:`origin_slug`)。解決できない実行は PR 番号の末尾一致までしか
            照合できず、``UnrecordedPRScan.repo_slug=None`` として報告に開示する

    **件名偽装は本検査では封じられない**(後続配線審査 後-6): ``--deemed`` は
    ``proposal_ref`` を無検証で受けるため、架空の PR 番号を指す記録は作れる。したがって
    「件名が偽なら記録も引けない」という関係は成立せず、偽装の封鎖は A-18-1 の
    :class:`PRVerifier`(GitHub API による実在+マージ照合。**API 不達時は fail-open** で
    縮退し、その件数は報告に出る)が担う。A-18-7 はその照合に依存する。

    **限界**: 承認記録が DB の外(Issue 決議など)にある PR は記録なしと判定される。
    定款第3条の発効要件は ``#承認`` への通知であり、その通知は ``governance.decisions``
    への記録と同一トランザクションでしか作られない(``governance/notices.py``)ため、
    「DB に無いが通知は出ている」状態は設計上存在しない。それでも例外を認める必要が
    出た場合は ``acknowledged_findings`` と同型の受容記録を足すこと(黙って除外しない)。
    """
    repo = str(repo_path)
    if since_commit and not _git_ok(repo, "cat-file", "-e", f"{since_commit}^{{commit}}"):
        raise ValueError(f"承認記録照合の基準コミットがリポジトリに存在しない: {since_commit}")

    slug = repo_slug or origin_slug(repo)
    patterns = protected_patterns(gov)
    trailer = str(gov.get("approval_trailer") or "Approved:")
    findings: list[dict[str, Any]] = []
    checked = 0
    for sha in _rev_list(repo, since_commit, "--first-parent", "--merges"):
        subject = _git(repo, "log", "-1", "--format=%s", sha).strip()
        # 件名からの PR 番号抽出は A-18-1/4 と同じ読み口を使う(件名は自己申告という限界も
        # 共有する。偽装の封鎖は A-18-1 の PRVerifier — 上記 docstring)。
        pr_number = pr_number_from_subject(subject)
        if pr_number is None:
            continue
        if len(_git(repo, "log", "-1", "--format=%P", sha).split()) != 2:
            continue  # octopus は PR マージと見なさない(A-18-1 と同じ判定)
        files = [
            ln
            for ln in _git(
                repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-m",
                "--first-parent", sha,
            ).splitlines()
            if ln
        ]
        touched = match_protected(files, patterns)
        if not touched:
            continue
        checked += 1

        expected = pr_proposal_ref(slug, pr_number) if slug else None
        refs = approval_trailer_refs(_git(repo, "log", "-1", "--format=%B", sha), trailer)
        trailer_rows = [(ref, _resolve_trailer_decision(conn, ref)) for ref in refs]
        by_number = decisions_for_pr_number(conn, pr_number)

        candidates = by_number + [row for _ref, row in trailer_rows if row is not None]
        if any(_attributed_to_pr(row, expected, pr_number) for row in candidates):
            continue

        # ここから先は所見。「なぜ帰属する記録が無いのか」を切り分けられる形で残す。
        other_proposals = sorted(
            {f"{ref} → {row['proposal_ref']}" for ref, row in trailer_rows if row is not None}
        )
        other_repos = sorted({str(row["proposal_ref"]) for row in by_number})
        unresolved = sorted({ref for ref, row in trailer_rows if row is None})
        parts: list[str] = []
        if other_proposals:
            parts.append(
                "Approved トレーラが別提案の承認記録を指している"
                f"({', '.join(other_proposals)})"
            )
        if other_repos:
            parts.append(f"PR 番号は一致するが別リポジトリの記録({', '.join(other_repos)})")
        if unresolved:
            parts.append(
                f"Approved トレーラの参照({', '.join(unresolved)})に対応する承認記録が無い"
            )
        if not parts:
            parts.append("Approved トレーラも当該 PR を指す承認記録も無い")
        findings.append(
            {
                "merge": sha[:12],
                "merge_full": sha,
                "subject": subject,
                "pr_number": pr_number,
                "files": touched,
                "trailer_refs": refs,
                "expected_ref": expected,
                "reason": (
                    f"{'; '.join(parts)} — この PR に帰属する承認記録が無い"
                    "(python -m ryza.governance.decisions --deemed-for-pr が未実行の疑い)"
                ),
            }
        )
    return UnrecordedPRScan(findings=findings, checked=checked, repo_slug=slug)


# ────────────────────────────────────────────────────────────────────────────
# A-18-8 審査対象 SHA の突合(トレーラの reviewed= ⇔ 承認記録の reviewed_sha)
#
# 様式 v2 の ``reviewed=<sha40>`` は**トレーラの書き手の申告**であり、A-18 はそれを
# 「独立審査が実際に見た SHA」として検証できなかった —— 照合先が存在しなかったからである
# (独立役員審査 2026-08-04 重要-3)。migration 0029 が承認記録の側に ``reviewed_sha`` /
# ``review_ref`` を作ったことで、初めて**2つの独立した書込経路**が同じ主張を持つ:
#   (1) コミットメッセージのトレーラ(git 履歴・PR 作成者が書く)
#   (2) governance.decisions の列(発効 CLI が gh から取った head SHA を書く)
# 本検査は両方ある決定について一致を要求する。片方だけを後から書き換えた偽装
# (トレーラの reviewed を別 SHA に差し替える・別 PR の記録にトレーラを向ける)は不一致で出る。
#
# **これは証明ではない**(黙って強い保証に見せない): どちらの値も最終的には発効を起票した
# 側の申告であり、審査エージェント自身の署名は無い。起票者が両方に同じ嘘を書けば一致する。
# 「独立審査が実際に見た SHA」への到達点は審査エージェントの出力から機械的に埋まる経路であり、
# 本検査はその**一歩**(突合先の新設と、片側改変の検出)にとどまる。
# ────────────────────────────────────────────────────────────────────────────
#: ``git log`` を1回で読むための区切り(コミットメッセージ本文に現れない制御文字)。
_LOG_FIELD_SEP = "\x1f"
_LOG_RECORD_SEP = "\x1e"


@dataclass(frozen=True)
class ReviewedShaScan:
    """A-18-8 の走査結果。

    ``compared`` は**緑の分母**(両方の値が揃っていて実際に突合できた件数)である。
    件数の無い緑は「不一致が無い」と「そもそも1件も突合していない」を同じ表示にする
    —— 0029 以前の記録は ``reviewed_sha`` が NULL なので、移行期の緑はほぼ後者である
    (A-18-7 の分母と同じ流儀 — 後続配線審査 後-4)。

    ``trailer_only`` は「トレーラに ``reviewed=`` はあるが承認記録に ``reviewed_sha`` が
    無い」件数。移行期は正常だが、``--deemed-for-pr`` 以外の経路で発効させ続けている
    兆候でもあるため notes に開示する。
    """

    findings: list[dict[str, Any]]
    compared: int
    trailer_only: int


def _log_messages(repo: str | Path, since: str | None) -> list[tuple[str, str]]:
    """``since..HEAD`` の ``(SHA, コミットメッセージ全文)`` を古い順に返す。

    1コミットずつ ``git log -1`` を呼ぶと履歴に比例して subprocess が増えるため、
    制御文字区切りの1回の ``git log`` で読む(A-18-1 が本文を読むのは保護領域に触れた
    コミットだけなので個別呼び出しで足りるが、本検査は全コミットのトレーラを見る)。
    """
    rng = f"{since}..HEAD" if since else "HEAD"
    out = _git(repo, "log", "--reverse", f"--format=%H{_LOG_FIELD_SEP}%B{_LOG_RECORD_SEP}", rng)
    records: list[tuple[str, str]] = []
    for raw in out.split(_LOG_RECORD_SEP):
        if _LOG_FIELD_SEP not in raw:
            continue
        sha, message = raw.split(_LOG_FIELD_SEP, 1)
        records.append((sha.strip(), message))
    return records


def check_reviewed_sha_agreement(
    repo_path: str | Path,
    gov: dict[str, Any],
    conn: Any,
    *,
    since_commit: str | None = RATIFICATION_COMMIT,
) -> ReviewedShaScan:
    """A-18-8: トレーラの ``reviewed=<sha40>`` と承認記録の ``reviewed_sha`` を突合する。

    突合は**トレーラ行単位**で行う。1行は ``Approved: <参照> reviewed=<sha>`` の形で
    「この参照の決定は、この SHA を審査対象として発効した」と主張しているので、参照から引いた
    決定の ``reviewed_sha`` と比べるのが主張どおりの検査になる(コミット単位でまとめて比べると、
    複数の承認記録を挙げるコミットでどの参照の主張が食い違ったのか特定できない)。

    所見にするのは**両方に値があって食い違う**場合だけである:

    - 承認記録に ``reviewed_sha`` が無い(0029 以前の記録・PR 以外の発効経路)は所見にしない。
      移行期に全件を鳴らすと本物の不一致が埋もれる。件数は ``trailer_only`` として開示する
    - トレーラに ``reviewed=`` が無い(様式 v1)も所見にしない。v1 の承継範囲の問題は
      A-18-1 の担当であり、打ち切りは reminders ``trailer-v1-sunset`` で扱う
    - 否認済みの決定も突合対象に含める。本検査が見るのは「審査対象の申告が一致するか」で
      あって決定の有効性ではない(有効性は A-18-1 が既に見ている)

    **限界の開示**: 一致は「2つの申告が食い違っていない」ことしか意味しない。どちらの値も
    発効を起票した側が書くため、**審査エージェント自身の署名は無く**、同じ値を両方に書けば
    一致する。本検査が捕まえるのは片側だけの改変・取り違え(別 PR の SHA の複写、マージ後に
    トレーラだけ書き換えた履歴、承認記録と無関係な SHA の申告)である。
    """
    repo = str(repo_path)
    if since_commit and not _git_ok(repo, "cat-file", "-e", f"{since_commit}^{{commit}}"):
        raise ValueError(f"審査対象 SHA 突合の基準コミットがリポジトリに存在しない: {since_commit}")

    trailer = str(gov.get("approval_trailer") or "Approved:")
    findings: list[dict[str, Any]] = []
    compared = 0
    trailer_only = 0
    for sha, message in _log_messages(repo, since_commit):
        for line in approval_trailers(message, trailer):
            if _REVIEWED_KEY in line.duplicates:
                continue  # 様式不備は A-18-1 が承継の起点から外して扱う(二重に鳴らさない)
            declared = line.attrs.get(_REVIEWED_KEY)
            if declared is None or not _FULL_SHA_RE.match(declared):
                continue  # v1 様式・様式不備は本検査の対象外(上記 docstring)
            row = _resolve_trailer_decision(conn, line.ref)
            if row is None:
                continue  # 参照が解決できないこと自体は A-18-1/7 の担当
            recorded = str(row.get("reviewed_sha") or "").strip().lower()
            if not recorded:
                trailer_only += 1
                continue
            compared += 1
            if recorded == declared.lower():
                continue
            findings.append(
                {
                    "commit": sha[:12],
                    "commit_full": sha,
                    "subject": _git(repo, "log", "-1", "--format=%s", sha).strip(),
                    "ref": line.ref,
                    "decision_id": row.get("decision_id"),
                    "trailer_reviewed": declared.lower(),
                    "recorded_reviewed": recorded,
                    "reason": (
                        f"Approved トレーラの reviewed={declared.lower()[:12]} と"
                        f"承認記録(id={row.get('decision_id')})の reviewed_sha="
                        f"{recorded[:12]} が一致しない"
                        "(審査対象の申告が2経路で食い違っている — どちらが実際の審査対象か"
                        "確認すること)"
                    ),
                }
            )
    return ReviewedShaScan(findings=findings, compared=compared, trailer_only=trailer_only)


# ────────────────────────────────────────────────────────────────────────────
# 本体・報告
# ────────────────────────────────────────────────────────────────────────────
def run_a18(
    repo_path: str | Path,
    *,
    governance_path: str = GOVERNANCE_PATH,
    since_commit: str | None = RATIFICATION_COMMIT,
    pr_since_commit: str | None = PR_RULE_BASELINE_COMMIT,
    deemed_since_commit: str | None = DEEMED_RECORD_BASELINE_COMMIT,
    version_pairs: tuple[tuple[str, str], ...] = VERSION_PAIRS,
    conn: Any | None = None,
    verify_prs: bool = True,
    pr_verifier: PRVerifier | None = None,
) -> dict[str, Any]:
    """A-18 の8検査を実行して構造化 dict を返す(A-18-5/6/7/8 は ``conn`` のある実行のみ)。

    ``conn`` を渡すと A-18-1 が ``Approved:`` トレーラの参照先(``governance.decisions``
    の ID 形式)を ``governance.current_decisions`` と突合する(read-only)。渡さない
    場合は従来どおりトレーラの存在検査までで、その旨を notes に載せる。

    ``verify_prs``(既定 True)で PR 件名・トレーラ PR URL の実在照合(:class:`PRVerifier`)を
    行う。API に到達できない実行は従来挙動へ縮退し、縮退の件数と理由を notes に開示する。
    ネットワークに触れたくない実行(テスト等)は ``verify_prs=False`` を渡す。
    """
    if pr_verifier is None and verify_prs:
        pr_verifier = PRVerifier(repo_path=repo_path)
    gov = load_governance(repo_path, governance_path)
    format_notes: list[str] = []
    found, inherited, checked, trailer_findings = check_protected_commits(
        repo_path,
        gov,
        since_commit=since_commit,
        conn=conn,
        pr_verifier=pr_verifier,
        format_notes=format_notes,
    )
    # 既知違反の受容: violations からは外すが捨てない(報告で必ず別枠表示する)。
    violations, acknowledged, ack_notes = partition_acknowledged(found, gov)
    direct_pushes, fp_checked = check_direct_pushes(
        repo_path, since_commit=pr_since_commit, pr_verifier=pr_verifier
    )
    unnotified: list[dict[str, Any]] = []
    untracked_deemed = 0
    resolution_bypass: dict[str, Any] | None = None
    unrecorded_prs: list[dict[str, Any]] = []
    deemed_scan: UnrecordedPRScan | None = None
    reviewed_scan: ReviewedShaScan | None = None
    if conn is not None:
        unnotified, untracked_deemed = check_unnotified_deemed(conn)
        resolution_bypass = check_resolution_bypass(conn)
        deemed_scan = check_unrecorded_protected_prs(
            repo_path, gov, conn, since_commit=deemed_since_commit
        )
        unrecorded_prs = deemed_scan.findings
        reviewed_scan = check_reviewed_sha_agreement(
            repo_path, gov, conn, since_commit=since_commit
        )
    return {
        "as_of": datetime.now(UTC).isoformat(),
        "since_commit": since_commit,
        "checked_commits": checked,
        "violations": violations,
        "inherited": inherited,
        "acknowledged": acknowledged,
        "mismatches": check_versions(repo_path, version_pairs),
        "declarations": list_declarations(gov),
        "pr_since_commit": pr_since_commit,
        "checked_first_parent": fp_checked,
        "direct_pushes": direct_pushes,
        "decision_refs_verified": conn is not None,
        "prs_verified": pr_verifier is not None,
        # PR 照合の成立/縮退の件数。縮退 > 0 の週は緑にしない(独立役員審査 重要-4:
        # 攻撃者が GIT_TOKEN を消すだけで偽 PR が「所見なし」で通る経路を塞ぐ)。
        "pr_verification": {
            "verified": pr_verifier.verified_count if pr_verifier else 0,
            "failed_open": pr_verifier.failed_open_count if pr_verifier else 0,
            "reasons": pr_verifier.failed_open_reasons if pr_verifier else {},
        },
        "trailer_findings": trailer_findings,
        "unnotified_deemed": unnotified,
        "resolution_bypass": resolution_bypass,
        "deemed_since_commit": deemed_since_commit,
        "unrecorded_prs": unrecorded_prs,
        # 緑の分母(後-4)。検査した保護領域 PR マージ数と、リポジトリ部分を照合できたか。
        "checked_protected_prs": deemed_scan.checked if deemed_scan else 0,
        "deemed_repo_slug": deemed_scan.repo_slug if deemed_scan else None,
        # A-18-8: トレーラ reviewed= ⇔ 承認記録 reviewed_sha の突合(conn のある実行のみ)。
        "reviewed_sha_mismatches": reviewed_scan.findings if reviewed_scan else [],
        "compared_reviewed_shas": reviewed_scan.compared if reviewed_scan else 0,
        "trailer_only_reviewed": reviewed_scan.trailer_only if reviewed_scan else 0,
        # 既知の限界は毎回開示する(独立役員審査条件)+ 個別の注記(登録漏れ・鮮度)。
        "notes": [
            *_coverage_notes(gov),
            *_staleness_note(repo_path),
            *ack_notes,
            *_unverified_inheritance_notes(inherited),
            *_v1_inheritance_notes(inherited),
            *sorted(set(format_notes)),
            *(pr_verifier.disclosures() if pr_verifier is not None else [
                "GitHub PR 実在照合は無効化されている(verify_prs=False)— 件名は自己申告のまま"
            ]),
            *([] if conn is not None else [
                "DB 接続なしの実行のため Approved トレーラの承認記録(否認済みか)と"
                "みなし承認の通知配送(A-18-5)・決議の批判経由(A-18-6)・"
                "保護領域 PR の承認記録(A-18-7)・審査対象 SHA の突合(A-18-8)は未照合"
            ]),
            *([] if reviewed_scan is None or not reviewed_scan.trailer_only else [
                f"トレーラに reviewed= はあるが承認記録に reviewed_sha が無い決定 "
                f"{reviewed_scan.trailer_only} 件 — A-18-8 の突合が働かない記録"
                "(0029 以前の記録、または --deemed-for-pr 以外の経路での発効)"
            ]),
            *([] if deemed_scan is None or deemed_scan.repo_slug else [
                "A-18-7 は origin remote から owner/repo を解決できず、承認記録の帰属を"
                "PR 番号の末尾一致までしか照合していない(別リポジトリの /pull/<N> の記録が"
                "救済しうる — 後続配線審査 後-5)"
            ]),
            *_trailer_notes(trailer_findings),
            *([] if not untracked_deemed else [
                f"通知参照が outbox: 形式でない deemed 記録が {untracked_deemed} 件"
                "(手作業の記録 — A-18-5 の配送検査で追跡できない)"
            ]),
            *STANDARD_DISCLOSURES,
        ],
    }


def _unverified_inheritance_notes(inherited: list[dict[str, Any]]) -> list[str]:
    """decisions 照合なしで承継した件数を開示する(照合不能でも黙って通さない)。"""
    n = sum(1 for i in inherited if not i.get("decision_verified"))
    if not n:
        return []
    return [
        f"decisions 照合なしの承継 {n} 件(DB 接続が無い実行のため、起点 PR のトレーラは"
        "形式的有効性のみで判定した — 否認済みかどうかは未照合)"
    ]


def _v1_inheritance_notes(inherited: list[dict[str, Any]]) -> list[str]:
    """v1 様式(``reviewed`` 無し)で承継した件数を開示する(移行期の可視化)。

    v1 の承継は「PR マージ時点のブランチ全体」に及ぶため、独立審査の後に積まれたコミットも
    同じトレーラで承認扱いになりうる(重大-2)。v2(``reviewed=<sha40>``)への移行が
    どこまで進んでいるかを毎週の報告で見えるようにする。
    """
    n = sum(1 for i in inherited if not i.get("reviewed_scoped"))
    if not n:
        return []
    return [
        f"reviewed 無し(様式 v1)の承継 {n} 件 — 承継範囲が PR マージ時点のブランチ全体に"
        "及ぶ経過措置。以後の保護領域 PR は Approved: <URL> reviewed=<sha40> を推奨"
    ]


def _trailer_notes(trailer_findings: list[dict[str, Any]]) -> list[str]:
    """照合できなかったトレーラ参照を注記にまとめる(重要-2 の開示)。"""
    unverifiable = sorted({u for f in trailer_findings for u in f.get("unverifiable", [])})
    if not unverifiable:
        return []
    return [f"照合できない Approved 参照: {'; '.join(unverifiable)}"]


def vetoed_trailer_findings(result: dict[str, Any]) -> list[dict[str, Any]]:
    """受理されたが否認済み参照を含むコミット(軽微-10)。照合不能のみの所見は含めない。"""
    return [f for f in result.get("trailer_findings", []) if f.get("problems")]


def pr_verification_degraded(result: dict[str, Any]) -> bool:
    """PR 実在照合が成立しなかった(縮退・無効化)実行か。

    **緑は全照合が成立した週に限る**(独立役員審査 2026-08-04 重要-4・反対意見書③)。
    ``repos/<slug>`` へ到達できなければ全照合が fail-open するため、``GIT_TOKEN`` を消すだけで
    偽 PR 件名が「所見なし」を通る。縮退した週を所見扱いにすると、この経路は静かでなくなる。
    """
    if result.get("prs_verified") is False:
        return True  # 明示的な無効化(--no-pr-verify)も緑にはしない
    return bool((result.get("pr_verification") or {}).get("failed_open"))


def has_findings(result: dict[str, Any]) -> bool:
    """警告(embed 投入)を要する所見があるか。

    照合できない参照(裸の数字)だけの所見は notes への開示にとどめ、報告の要否は
    変えない。様式の不備であって統制違反ではないため、これで ⚠️ を点けると
    「毎回 ⚠️」になり本物の違反が埋もれる。**PR 照合の縮退は別**で、これは
    「検査が働かなかった」ことの表明なので所見にする(重要-4)。

    A-18-6(決議の批判経由)は閾値に達したときだけ数える。件数が 0 でない程度では
    鳴らさないのは同じ理由で、閾値の定義は ``boardroom`` 側(走査窓・連続・累積)に一本化
    してある。
    """
    return bool(
        result["violations"]
        or result["mismatches"]
        or result["direct_pushes"]
        or result.get("unnotified_deemed")
        or result.get("unrecorded_prs")
        or (result.get("resolution_bypass") or {}).get("alert")
        or result.get("reviewed_sha_mismatches")
        or vetoed_trailer_findings(result)
        or pr_verification_degraded(result)
    )


def _chunk_notes(notes: list[str], limit: int = 1024) -> list[str]:
    """注記行を Discord の field 上限に収まる塊へ分割する(切り捨てない)。"""
    chunks: list[str] = []
    current = ""
    for note in notes:
        line = f"- {note}"
        if len(line) > limit:  # 単独で超える行だけは切る(それ以外は必ず全部出す)
            line = line[: limit - 1] + "…"
        if not current:
            current = line
        elif len(current) + 1 + len(line) <= limit:
            current = f"{current}\n{line}"
        else:
            chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks


def build_alert_embed(result: dict[str, Any]) -> dict[str, Any]:
    """#運営 向けの警告/報告 embed(daily の実行サマリと同じ流儀)。"""
    fields: list[dict[str, Any]] = []

    if result["violations"]:
        lines = [
            f"- `{v['commit']}` {v['subject']}({v['reason']}: {', '.join(v['files'])})"
            for v in result["violations"]
        ]
        fields.append(
            {
                "name": "⚠️ A-18-1 保護領域の無承認変更",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            }
        )
    else:
        fields.append(
            {
                "name": "A-18-1 保護領域突合",
                "value": f"✅ 違反なし(検査 {result['checked_commits']} コミット)",
                "inline": False,
            }
        )

    # PR 承継で承認されたコミットは違反にしないが、起点の PR マージごとに集計して可視化する。
    inherited = result.get("inherited") or []
    if inherited:
        by_merge: dict[str, list[dict[str, Any]]] = {}
        for item in inherited:
            by_merge.setdefault(f"`{item['merge']}` {item['merge_subject']}", []).append(item)
        # 免除した保護パスの和集合まで出す(件数だけでは「何が免除されたか」が見えない
        # — 独立役員審査 2026-08-04 中-3)。長くなる場合は先頭数件+残数に丸める。
        inh_lines = []
        for m, items in by_merge.items():
            paths = sorted({f for item in items for f in item["files"]})
            shown = ", ".join(paths[:5])
            if len(paths) > 5:
                shown += f" ほか {len(paths) - 5} 件"
            # 承継範囲(reviewed 限定か v1 のブランチ全体か)も出す。v1 の緑は
            # 「審査後 push を含みうる」意味であり、v2 の緑と同じに見せない(重大-2)。
            scope = (
                "reviewed 限定"
                if all(i.get("reviewed_scoped") for i in items)
                else "様式 v1(範囲=マージ時点のブランチ全体)"
            )
            inh_lines.append(f"- {m}: {len(items)} コミット / {scope}({shown})")
        fields.append(
            {
                "name": f"PR 承継で承認: {len(inherited)} コミット(起点 {len(by_merge)} PR)",
                "value": "\n".join(inh_lines)[:1024],
                "inline": False,
            }
        )

    # 受容済み既知違反は violations から外れるが、必ず別枠で可視化する(黙って消さない)。
    acknowledged = result.get("acknowledged") or []
    if acknowledged:
        ack_lines = [
            f"- `{a['commit']}` {a['subject']}({', '.join(a['files'])}"
            f"{' / 承認記録: ' + a['approval_ref'] if a.get('approval_ref') else ''})"
            for a in acknowledged
        ]
        fields.append(
            {
                "name": f"受容済み既知違反: {len(acknowledged)} 件(A-18-1・是正不能として受容)",
                "value": "\n".join(ack_lines)[:1024],
                "inline": False,
            }
        )

    if result["mismatches"]:
        lines = [
            f"- {m['doc']}(v{m['doc_version']})⇔ {m['config']}"
            f"(v{m['config_version']}): {m['reason']}"
            for m in result["mismatches"]
        ]
        fields.append(
            {
                "name": "⚠️ A-18-2 文書⇔config 不整合",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            }
        )
    else:
        fields.append({"name": "A-18-2 文書⇔config 整合", "value": "✅ 一致", "inline": False})

    decls = result["declarations"]
    decl_lines = [f"- {d['rule']}" for d in decls] or ["なし"]
    fields.append(
        {
            "name": f"A-18-3 宣言のみ条文(執行点なし): {len(decls)} 件",
            "value": "\n".join(decl_lines)[:1024],
            "inline": False,
        }
    )

    if result["direct_pushes"]:
        lines = [
            f"- `{v['commit']}` {v['subject']}({', '.join(v['files'])})"
            for v in result["direct_pushes"]
        ]
        fields.append(
            {
                "name": "⚠️ A-18-4 全変更 PR 化違反(直 push・非 PR マージ)",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            }
        )
    else:
        fields.append(
            {
                "name": "A-18-4 全変更 PR 化",
                "value": (
                    f"✅ 直 push・非 PR マージなし(検査 {result['checked_first_parent']} コミット)"
                ),
                "inline": False,
            }
        )
    unnotified = result.get("unnotified_deemed") or []
    if unnotified:
        lines = [
            f"- decision id={u['decision_id']} {u['proposal_ref']}"
            f"({u['notice_ref']}: {u['reason']})"
            for u in unnotified
        ]
        fields.append(
            {
                "name": "⚠️ A-18-5 通知なき発効(みなし承認の通知が未配送)",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            }
        )
    elif result.get("decision_refs_verified"):
        fields.append(
            {
                "name": "A-18-5 みなし承認の通知配送",
                "value": "✅ 未配送の滞留なし",
                "inline": False,
            }
        )

    # A-18-6: 閾値未満でも**必ず1行載せる**(A-18-5 と同じ流儀 — 「アラートが無い」と
    # 「そもそも見ていない」を沈黙で同一視させない)。ダッシュボードの決議欄は
    # 確認を外す当人しか見ないため、独立した検出点はこの行である。
    bypass = result.get("resolution_bypass")
    if bypass:
        fields.append(
            {
                "name": (
                    "⚠️ A-18-6 決議の批判経由(形骸化の疑い)"
                    if bypass["alert"]
                    else "A-18-6 決議の批判経由"
                ),
                "value": bypass["line"][:1024],
                "inline": False,
            }
        )

    # A-18-7: 記録漏れは「気づかれないこと」自体が問題なので、0 件でも1行載せる
    # (A-18-5・A-18-6 と同じ流儀 — 沈黙を「見ていない」と区別できるようにする)。
    unrecorded = result.get("unrecorded_prs") or []
    checked_prs = result.get("checked_protected_prs") or 0
    if unrecorded:
        lines = [
            f"- `{u['merge']}` PR #{u['pr_number']} {u['subject']}"
            f"({', '.join(u['files'])}: {u['reason']})"
            for u in unrecorded
        ]
        fields.append(
            {
                "name": (
                    f"⚠️ A-18-7 保護領域 PR の承認記録漏れ {len(unrecorded)}/{checked_prs} 件"
                    "(--deemed-for-pr の実行忘れ)"
                ),
                "value": "\n".join(lines)[:1024],
                "inline": False,
            }
        )
    elif result.get("decision_refs_verified"):
        # **緑には必ず分母を書く**(後-4)。件数の無い ✅ は「漏れが無い」と「1件も見て
        # いない」を同じ表示にする —— squash マージへ移行して `Merge pull request` 件名が
        # 消えれば、検査は 0 件走査の ✅ を出し続ける。
        fields.append(
            {
                "name": "A-18-7 保護領域 PR の承認記録",
                "value": (
                    f"✅ 記録漏れなし(検査対象 {checked_prs} 件)"
                    if checked_prs
                    else "対象 PR なし(基準コミット以降に保護領域へ触れた PR マージが 0 件 — "
                         "squash マージ運用へ移行した場合も同じ表示になる)"
                ),
                "inline": False,
            }
        )

    # A-18-8: 不一致は「2つの申告が食い違っている」= 事故か改変の signal。0 件でも1行載せ、
    # **分母(突合できた件数)を必ず書く**(A-18-7 と同じ流儀 — 移行期の緑は「不一致が無い」
    # ではなく「まだ1件も突合していない」であることが多い)。
    reviewed_mismatches = result.get("reviewed_sha_mismatches") or []
    compared_shas = result.get("compared_reviewed_shas") or 0
    if reviewed_mismatches:
        lines = [
            f"- `{m['commit']}` {m['subject']}({m['ref']}: トレーラ "
            f"{m['trailer_reviewed'][:12]} ⇔ 記録 {m['recorded_reviewed'][:12]})"
            for m in reviewed_mismatches
        ]
        fields.append(
            {
                "name": (
                    f"⚠️ A-18-8 審査対象 SHA の不一致 {len(reviewed_mismatches)}/{compared_shas} 件"
                    "(トレーラの reviewed= ⇔ 承認記録の reviewed_sha)"
                ),
                "value": "\n".join(lines)[:1024],
                "inline": False,
            }
        )
    elif result.get("decision_refs_verified"):
        fields.append(
            {
                "name": "A-18-8 審査対象 SHA の突合",
                "value": (
                    f"✅ 不一致なし(突合できた決定 {compared_shas} 件)"
                    if compared_shas
                    else "突合対象なし(トレーラの reviewed= と承認記録の reviewed_sha が"
                         "揃った決定が 0 件 — 一致の確認ではない)"
                ),
                "inline": False,
            }
        )

    # PR 実在照合の成立/縮退は必ず1行出す(緑の意味を「照合が働いた」に限定する — 重要-4)。
    prv = result.get("pr_verification") or {}
    if pr_verification_degraded(result):
        if result.get("prs_verified") is False:
            value = "❗ 照合が無効化された実行(--no-pr-verify)— PR 件名は自己申告のまま"
        else:
            reasons = "\n".join(
                f"  - {r}: {n} 件" for r, n in sorted(prv.get("reasons", {}).items())
            )
            value = (
                f"❗ 照合不能 {prv.get('failed_open', 0)} 件(**要手動確認** — この週の "
                f"PR 件名は自己申告のまま通っている)\n{reasons}"
            )
        fields.append(
            {"name": "⚠️ GitHub PR 実在照合が成立していない", "value": value[:1024], "inline": False}
        )
    elif prv.get("verified"):
        fields.append(
            {
                "name": "GitHub PR 実在照合",
                "value": f"✅ {prv['verified']} 件を実在+マージ済み(SHA 帰属含む)として確認",
                "inline": False,
            }
        )

    vetoed_refs = vetoed_trailer_findings(result)
    if vetoed_refs:
        lines = [
            f"- `{f['commit']}` {f['subject']}({'; '.join(f['problems'])})"
            for f in vetoed_refs
        ]
        fields.append(
            {
                "name": "⚠️ 否認済みの承認記録を参照するコミット(取消義務の検討対象)",
                "value": "\n".join(lines)[:1024],
                "inline": False,
            }
        )

    # 注記は 1024 文字を超えると Discord 側で落ちるため、切り捨てずに複数 field へ分割する。
    # 末尾を黙って捨てると「開示したつもりの限界」が消える(独立役員審査 2026-08-04 低-8)。
    for i, chunk in enumerate(_chunk_notes(result["notes"])):
        fields.append(
            {"name": "注記" if i == 0 else f"注記(続き {i + 1})", "value": chunk, "inline": False}
        )

    alert = has_findings(result)
    return {
        "title": ("⚠️ A-18 監査: 要対応の所見あり" if alert else "A-18 監査: 所見なし"),
        "description": (
            "規則⇔実装トレーサビリティ監査(定款第6条)。監査は read-only であり修正は行わない。"
        ),
        "color": COLOR_FLASH if alert else COLOR_NORMAL,
        # 監査報告の発信者 = 監査部門のキャラクター(台帳 org.yaml から役職キーで解決)。
        "author": org.author_for_role("audit"),
        "fields": fields,
        "footer": {"text": DISCLAIMER},
    }


def enqueue_alert(conn: Any, result: dict[str, Any], run_id: int, *, channel: str = "ops") -> int:
    """検査結果 embed を ``press.outbox`` の ops チャンネルへ投入する(違反時は urgent)。"""
    # 通知なき発効(A-18-5)は governance.yaml が violation と定める statement なので、
    # 保護領域違反・直 push と同じ緊急度で扱う。
    # **A-18-7 を urgent に含めないのは意図的**: A-18-5 は「いま配送が詰まっている」進行中の
    # 障害で、速く出すほど滞留を短くできる。A-18-7 が指すのは既にマージされた PR の記録漏れで、
    # 発見時点で通知なき発効の窓は閉じており、速報にしても短くならない(是正は --deemed の
    # 実行か、記録が本当に無いなら取消の判断)。⚠️ 付きで週次報告に必ず載る。
    urgent = bool(
        result["violations"] or result["direct_pushes"] or result.get("unnotified_deemed")
    )
    return enqueue(conn, channel, build_alert_embed(result), run_id, urgent=urgent)


def run_a18_readonly(repo_path: str | Path, **run_kwargs: Any) -> dict[str, Any]:
    """照合専用の **autocommit・read-only 接続**で :func:`run_a18` を実行し、接続を閉じる。

    **なぜ検査用と報告用で接続を分けるか**(独立役員審査 軽微-11): 検査の大半は git の
    subprocess 走査であり、履歴が伸びるほど時間が延びる。承認記録の照合(A-18-1/5/6/7)を
    報告投入と同じトランザクションで行うと、その走査の間ずっと ``idle in transaction`` の
    セッションが残る —— VACUUM の回収対象を止め、長時間ロックの原因になる。読取は
    autocommit(= 文ごとに完結)にして走査中にトランザクションを開いたままにしない。

    分離しても報告の一貫性は落ちない。各検査は自分が読んだ時点の状態を所見に焼き込むので、
    報告は「検査時点の観測」であり、報告投入時に承認状態が変わっていても所見の意味は変わらない
    (むしろ単一の長いトランザクションは、検査中に発効した承認を最後まで見ないという別の
    ずれを作る)。

    ``default_transaction_read_only`` は**うっかり書込の検出点であって権限境界ではない**
    (後続配線審査 後-8)。これはセッション既定にすぎず、``SET TRANSACTION READ WRITE`` で
    上書きでき、接続ロールの書込権限も失われない。意図せず書込が紛れ込んだときに静かに
    書かれず即座に失敗する、という早期検出のための設定であり、悪意ある書込は止められない。
    """
    from ryza.db.conn import connect

    conn = connect(autocommit=True)
    try:
        conn.execute("SET default_transaction_read_only = on")
        return run_a18(repo_path, conn=conn, **run_kwargs)
    finally:
        conn.close()


def run_and_report(
    repo_path: str | Path,
    *,
    dry_run: bool = False,
    always_report: bool = False,
    since_commit: str | None = RATIFICATION_COMMIT,
    pr_since_commit: str | None = PR_RULE_BASELINE_COMMIT,
    deemed_since_commit: str | None = DEEMED_RECORD_BASELINE_COMMIT,
    verify_prs: bool = True,
) -> dict[str, Any]:
    """A-18 を実行し、所見があれば(または ``always_report``)#運営 へ enqueue する。

    ops-weekly など他ジョブからの呼び出し口。``dry_run`` では DB に接続せずログのみ
    (このとき ``Approved:`` トレーラの承認記録との突合は行われない — notes に開示する)。

    通常実行は**接続を2本に分ける**: 検査(照合)は :func:`run_a18_readonly` の
    autocommit・read-only 接続で行い、閉じてから報告投入用の書込接続を開く。git 走査を
    含む長い検査でトランザクションを開いたままにしないためである(軽微-11。理由は
    :func:`run_a18_readonly` の docstring)。
    """
    if dry_run:
        result = run_a18(
            repo_path,
            since_commit=since_commit,
            pr_since_commit=pr_since_commit,
            deemed_since_commit=deemed_since_commit,
            verify_prs=verify_prs,
        )
        log.info(
            "[DRY_RUN] A-18 結果: violations=%d inherited=%d acknowledged=%d mismatches=%d "
            "declarations=%d direct_pushes=%d(enqueue %s)",
            len(result["violations"]), len(result.get("inherited") or []),
            len(result.get("acknowledged") or []),
            len(result["mismatches"]), len(result["declarations"]),
            len(result["direct_pushes"]),
            "対象" if (has_findings(result) or always_report) else "不要",
        )
        return result

    from ryza.db.conn import connect
    from ryza.provenance import start_run

    run = start_run("audit.a18", {"repo": str(repo_path)})
    try:
        # 照合は read-only の別接続で完結させ、閉じてから書込接続を開く(軽微-11)。
        result = run_a18_readonly(
            repo_path,
            since_commit=since_commit,
            pr_since_commit=pr_since_commit,
            deemed_since_commit=deemed_since_commit,
            verify_prs=verify_prs,
        )
    except Exception:
        run.finish("failed")
        raise
    conn = connect()
    try:
        if has_findings(result) or always_report:
            oid = enqueue_alert(conn, result, run.run_id)
            log.info("A-18 警告を enqueue: outbox_id=%s", oid)
        else:
            log.info("A-18: 所見なし(enqueue しない)")
        conn.commit()
        run.finish("success")
    except Exception:
        conn.rollback()
        run.finish("failed")
        raise
    finally:
        conn.close()
    return result


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI 実行パス
    """CLI: ``python -m ryza.audit.a18 [--repo PATH] [--dry-run] [--always-report]``"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="A-18 規則⇔実装トレーサビリティ監査")
    parser.add_argument("--repo", default=".", help="監査対象の git リポジトリパス")
    parser.add_argument("--dry-run", action="store_true", help="DB へ書き込まずログのみ")
    parser.add_argument(
        "--always-report", action="store_true", help="所見が無くても #運営 へ結果を投入する"
    )
    parser.add_argument(
        "--no-pr-verify", action="store_true",
        help="GitHub API による PR 実在照合を行わない(オフライン実行)",
    )
    args = parser.parse_args(argv)

    result = run_and_report(
        args.repo,
        dry_run=args.dry_run,
        always_report=args.always_report,
        verify_prs=not args.no_pr_verify,
    )
    for v in result["violations"]:
        print(f"[違反] {v['commit']} {v['subject']}: {v['files']}", file=sys.stderr)
    for m in result["mismatches"]:
        print(f"[不整合] {m['doc']} v{m['doc_version']} ⇔ {m['config']} v{m['config_version']}",
              file=sys.stderr)
    for a in result.get("acknowledged") or []:
        print(f"[受容済み] {a['commit']} {a['subject']}: {a['files']}", file=sys.stderr)
    for d in result["direct_pushes"]:
        print(f"[直push] {d['commit']} {d['subject']}: {d['files']}", file=sys.stderr)
    for u in result.get("unnotified_deemed", []):
        print(f"[通知なき発効] decision id={u['decision_id']} {u['proposal_ref']}: {u['reason']}",
              file=sys.stderr)
    for u in result.get("unrecorded_prs") or []:
        print(f"[承認記録漏れ] {u['merge']} PR #{u['pr_number']} {u['subject']}: {u['reason']}",
              file=sys.stderr)
    bypass = result.get("resolution_bypass")
    if bypass and bypass["alert"]:
        print(f"[決議の批判経由] {bypass['line']}", file=sys.stderr)
    print(
        f"A-18 完了(検査 {result['checked_commits']} コミット, 違反 {len(result['violations'])}, "
        f"PR 承継 {len(result.get('inherited') or [])}, "
        f"受容済み {len(result.get('acknowledged') or [])}, "
        f"不整合 {len(result['mismatches'])}, 宣言 {len(result['declarations'])}, "
        f"直push {len(result['direct_pushes'])}, "
        f"通知なき発効 {len(result.get('unnotified_deemed', []))}, "
        f"承認記録漏れ {len(result.get('unrecorded_prs') or [])}, "
        f"批判を経ない決議 {(result.get('resolution_bypass') or {}).get('bypassed', '未照合')})",
        file=sys.stderr,
    )
    return 1 if has_findings(result) else 0


if __name__ == "__main__":
    raise SystemExit(main())
