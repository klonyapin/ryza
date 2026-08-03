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

# ロードマップ定義。issue を指定すると Issue の状態から自動判定される:
#   CLOSED → done / OPEN+in-progress ラベル → doing / OPEN+user-action → user / それ以外 → todo
# issue 未指定のものだけ status を手動指定(done | doing | todo | user)。
MILESTONES = [
    {"name": "全体設計 v3.2", "detail": "組織14部門・二系統会計・独立監査・研究本部・報道部・執筆規格", "status": "done"},
    {"name": "ガバナンス設計", "detail": "AI役員の役職資産・役員室・IPS改訂フロー", "status": "done"},
    {"name": "詳細設計(データ/報道/リサーチ/IPS)", "detail": "5スキーマ・3帳簿・文体リンター・分析エージェント・市場観・IPS v1.0ドラフト", "status": "done"},
    {"name": "GCP セットアップ", "detail": "ryza-main・API有効化・Billing Export 済み", "issue": 7},
    {"name": "T-001〜T-003 DB・会計・証憑", "detail": "マイグレーション+帳簿制約・記帳/締め/財務諸表・リネージ", "issue": 3},
    {"name": "T-004/T-005 週次ジョブ・統合", "detail": "ops-weekly(Cloud Run Job+Scheduler)稼働中", "issue": 14},
    {"name": "T-006 Discord Bot 基盤", "detail": "GCE e2-micro 常駐・4チャンネル・承認/KillSwitch・日報", "issue": 17},
    {"name": "T-009 データ取込", "detail": "J-Quants/TDnet/EDINET/RSS/FRED/カレンダー", "issue": 18},
    {"name": "T-010 階層0前処理", "detail": "重複排除・分類・銘柄タグ・一次重要度・埋め込み", "issue": 21},
    {"name": "T-011 分析エージェント+市場観", "detail": "macro/micro/sentiment/editor・慣性ルール・反証拠テスト", "issue": 22},
    {"name": "T-007/T-008 朝刊・速報", "detail": "毎朝10:00 玲音の朝刊・文体リンター・速報と的中率追跡", "status": "todo"},
    {"name": "T-012 取込ソース一括拡張", "detail": "EDGAR・e-Stat・海外中銀・国際機関", "issue": 20},
    {"name": "第1回フル実装監査", "detail": "別ベンダー AI によるコード監査(最初の運用可能版の後)", "issue": 15},
    {"name": "IPS v1.0 確定", "detail": "DD25%・レバ2.0x・集中度20%・政策ミックス。§7論点の判断待ち", "issue": 4},
    {"name": "戦略・リスク・執行 実装", "detail": "動物園・リスクエンジン・状態機械・アダプタ", "status": "todo"},
    {"name": "デモ口座開設(保留)", "detail": "執行層実装の2〜3週間前に IBKR のみ申請。それまで自前シミュレータで代替", "issue": 8},
    {"name": "ペーパー運用開始", "detail": "日次サイクル(取込→分析→朝刊→取引→記帳)フル稼働", "status": "todo"},
]

PHASE = "実装フェーズ(T-010 進行中/T-011 待機)"


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
        if "issue" in m and "status" not in m:
            iss = by_num.get(m["issue"])
            if iss and iss["state"] == "CLOSED":
                m["status"] = "done"
            elif iss and "in-progress" in iss["labels"]:
                m["status"] = "doing"
            elif iss and "user-action" in iss["labels"]:
                m["status"] = "user"
            else:
                m["status"] = "todo"
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
