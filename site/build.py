#!/usr/bin/env python3
"""進捗データ(data.js)を GitHub Milestones / Issues / git ログから生成する。

ロードマップの正は GitHub Milestones(ハードコードしない)。編集は GitHub 側で行う:
  - 工程の追加/名称/説明: リポジトリの Milestones ページ
  - 状態: マイルストーンを閉じる=完了 / 所属 Issue の in-progress ラベル=進行中 /
          user-action ラベル=ユーザー待ち / それ以外=未着手

使い方: リポジトリルートで `python3 site/build.py` → site/data.js を更新(ローカル配信)。
機微情報(Issue 本文・認証情報)は含めない — タイトル・状態・ラベルのみ。
"""
import json
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent


def sh(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, check=True).stdout


def main() -> None:
    issues = json.loads(sh(["gh", "issue", "list", "--state", "all", "--limit", "200",
                            "--json", "number,title,state,labels,milestone"]))
    issues = [{"number": i["number"], "title": i["title"], "state": i["state"],
               "labels": [l["name"] for l in i["labels"]],
               "milestone": (i.get("milestone") or {}).get("title")}
              for i in sorted(issues, key=lambda x: x["number"])]

    ms_raw = json.loads(sh(["gh", "api", "repos/:owner/:repo/milestones?state=all&per_page=100"]))
    ms_raw.sort(key=lambda m: m["number"])  # 作成順=ロードマップ順

    milestones = []
    doing_titles = []
    for ms in ms_raw:
        attached = [i for i in issues if i["milestone"] == ms["title"]]
        open_attached = [i for i in attached if i["state"] == "OPEN"]
        if ms["state"] == "closed":
            status = "done"
        elif any("in-progress" in i["labels"] for i in open_attached):
            status = "doing"
        elif open_attached and all("user-action" in i["labels"] for i in open_attached):
            status = "user"
        else:
            status = "todo"
        if status == "doing":
            doing_titles.append(ms["title"])
        milestones.append({"name": ms["title"], "detail": ms.get("description") or "",
                           "status": status})

    phase = "実装フェーズ(" + "・".join(doing_titles) + " 進行中)" if doing_titles \
        else "実装フェーズ"

    commits = []
    for line in sh(["git", "log", "-10", "--pretty=%h\t%ad\t%s", "--date=format:%m/%d %H:%M"]).splitlines():
        h, d, s = line.split("\t", 2)
        commits.append({"hash": h, "date": d, "subject": s})

    data = {
        "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
        "phase": phase,
        "milestones": milestones,
        "issues": issues,
        "commits": commits,
    }
    out = ROOT / "site" / "data.js"
    out.write_text("window.RYZA_DATA = " + json.dumps(data, ensure_ascii=False, indent=1) + ";\n",
                   encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
