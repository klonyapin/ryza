#!/usr/bin/env python3
"""進捗データ(data.js)を GitHub Issues / git ログから生成する。

使い方: リポジトリルートで `python3 site/build.py` → site/data.js を更新。
その後 `gcloud run deploy ryza-status --source site ...` で再デプロイ。
機微情報(Issue 本文・認証情報)は含めない — タイトル・状態・ラベルのみ。
"""
import json
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent

# ロードマップ定義。issue を指定すると CLOSED で自動的に done になる。
# status: done | doing | todo | user(あなた待ち)を手動指定(issue 未指定時)
MILESTONES = [
    {"name": "全体設計 v3.2", "detail": "組織14部門・二系統会計・独立監査・研究本部・報道部・執筆規格", "status": "done"},
    {"name": "ガバナンス設計", "detail": "AI役員の役職資産・役員室・IPS改訂フロー", "status": "done"},
    {"name": "データ基盤+会計 詳細設計", "detail": "5スキーマ・3帳簿・証憑・リネージ", "status": "done"},
    {"name": "IPS v1.0", "detail": "DD25%・レバ2.0x・集中度20%・政策ミックス。§7論点の判断待ち", "issue": 4},
    {"name": "GCP セットアップ", "detail": "ryza-fund 作成・API有効化済み。Billing Export のみ手作業", "issue": 7},
    {"name": "デモ口座開設", "detail": "IBKR / Alpaca / Saxo / Testnet", "issue": 8},
    {"name": "T-001 DB基盤", "detail": "マイグレーション+帳簿制約", "issue": 1},
    {"name": "T-002 会計エンジン", "detail": "記帳・締め・財務諸表・照合", "issue": 2},
    {"name": "T-003 証憑・リネージ", "detail": "不変保存・改竄検知・遡及クエリ", "issue": 3},
    {"name": "報道部 詳細設計", "detail": "文体リンター・速報閾値・embed", "issue": 5},
    {"name": "リサーチ層 詳細設計", "detail": "エージェント入出力・市場観ステート", "issue": 6},
    {"name": "ガバナンス基盤 実装", "detail": "personas・議事録スキーマ・役員室チャット", "issue": 9},
    {"name": "データ取込・リサーチ実装", "detail": "J-Quants/EDINET 取込→分析→市場観", "status": "todo"},
    {"name": "報道部・Discord Bot 実装", "detail": "朝刊10:00・速報・承認フロー", "status": "todo"},
    {"name": "戦略・リスク・執行 実装", "detail": "動物園・リスクエンジン・状態機械・アダプタ", "status": "todo"},
    {"name": "GCP デプロイ・ペーパー運用開始", "detail": "e2-micro+Cloud Run、日次サイクル稼働", "status": "todo"},
]

PHASE = "実装フェーズ(T-001 進行中)"


def sh(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, check=True).stdout


def main() -> None:
    issues = json.loads(sh(["gh", "issue", "list", "--state", "all", "--limit", "100",
                            "--json", "number,title,state,labels"]))
    issues = [{"number": i["number"], "title": i["title"], "state": i["state"],
               "labels": [l["name"] for l in i["labels"]]} for i in sorted(issues, key=lambda x: x["number"])]
    by_num = {i["number"]: i for i in issues}

    milestones = []
    for m in MILESTONES:
        m = dict(m)
        if "issue" in m and m.get("status") is None or ("issue" in m and "status" not in m):
            iss = by_num.get(m["issue"])
            if iss and iss["state"] == "CLOSED":
                m["status"] = "done"
            elif iss and "user-action" in iss["labels"]:
                m["status"] = "user"
            elif iss and "impl" in iss["labels"] and m["issue"] == 1:
                m["status"] = "doing"
            else:
                m["status"] = m.get("status", "todo")
        milestones.append(m)

    commits = []
    for line in sh(["git", "log", "-10", "--pretty=%h\t%ad\t%s", "--date=format:%m/%d %H:%M"]).splitlines():
        h, d, s = line.split("\t", 2)
        commits.append({"hash": h, "date": d, "subject": s})

    data = {
        "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
        "phase": PHASE,
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
