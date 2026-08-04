#!/usr/bin/env python3
"""第1回フル実装監査(Issue #15・A-12)— GLM-5 を独立監査人として実行する使い捨てランナー。

プロンプト分離: 監査人には設計文書と実装のみを渡す。設計リードの見解・過去の審査結論は渡さない。
"""
import json
import pathlib
import sys
import time
import urllib.request

REPO = pathlib.Path("/Users/mmiyazaki/Projects/sukifura/ryza")
OUT = pathlib.Path("/tmp/glm_audit/findings")
OUT.mkdir(parents=True, exist_ok=True)

# 認証は opencode の auth.json から読む(ハードコードしない)
auth = json.loads((pathlib.Path.home() / ".local/share/opencode/auth.json").read_text())
API_KEY = auth["zai"]["key"] if isinstance(auth.get("zai"), dict) else auth["zai"]

URL = "https://api.z.ai/api/anthropic/v1/messages"
MODEL = "glm-5"

SYSTEM = """あなたは自動運用システム Ryza の「第1回フル実装監査」(監査コード A-12)を担当する独立監査人である。実装者(Claude 系モデル)とは別系統のモデルとして、利害から独立した立場で監査する。

義務:
- 設計文書と実装の突合: 設計が主張する統制・不変原則が実装に存在するか、実装が設計に無い挙動を持っていないかを両方向で検査する
- 所見には必ず根拠(ファイルパス・関数名・該当コードの引用)を付ける。根拠のない印象論は書かない
- 重大度を付ける: [重大](統制の欠落・資金/帳簿の混在・設計違反)/[重要](迂回可能な統制・テストの構造的な穴)/[中]/[軽微]
- 問題が見つからない領域は「検査したが所見なし」と検査内容を添えて明記する(検査しなかったことと区別する)
- 実装者への忖度をしない。疑わしきは所見として挙げる

出力は日本語。見出し・所見番号・重大度・根拠・推奨是正の形式で書く。"""

def read(path: str, max_lines: int = 0) -> str:
    p = REPO / path
    if not p.exists():
        return f"(({path} は存在しない))"
    text = p.read_text(encoding="utf-8", errors="replace")
    if max_lines:
        lines = text.splitlines()
        if len(lines) > max_lines:
            text = "\n".join(lines[:max_lines]) + f"\n((以下 {len(lines)-max_lines} 行省略))"
    return f"===== FILE: {path} =====\n{text}\n"

def read_glob(pattern: str) -> str:
    parts = []
    for p in sorted(REPO.glob(pattern)):
        if p.is_file():
            parts.append(read(str(p.relative_to(REPO))))
    return "\n".join(parts)

def call(name: str, prompt: str, max_tokens: int = 16000) -> None:
    body = json.dumps({
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    })
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=1800) as resp:
        data = json.loads(resp.read())
    text = "".join(c.get("text", "") for c in data.get("content", []))
    usage = data.get("usage", {})
    (OUT / f"{name}.md").write_text(text, encoding="utf-8")
    print(f"[{name}] done in {time.time()-t0:.0f}s "
          f"in={usage.get('input_tokens')} out={usage.get('output_tokens')}", flush=True)

DESIGN = read("docs/design/00-system-design.md") + read("CLAUDE.md")

PASSES = {
    # 1b. 帳簿・会計(再監査)— 初回は 0005/0006/0013 等のスキーマを渡し漏れたため再実行
    "pass1b-ledger-schema": DESIGN + read_glob("src/ryza/ledger/*.py") + read("migrations/0005_ledger.sql") + read("migrations/0006_seed.sql") + read("migrations/0011_demo_capital_increase.sql") + read("migrations/0034_ledger_mtm_account.sql") + """

【監査対象】会計エンジン(ledger)と会計スキーマ(0005/0006/0011/0034)。今回はスキーマ DDL 全文を提供している。
【検査項目】①二系統分離がスキーマ制約で強制されているか(book_id 混合禁止トリガ・勘定科目の帳簿別定義と FK)②evidence_id 必須の強制 ③複式簿記の不変条件(Σ借方=Σ貸方)の強制箇所と、行レベル制約(debit/credit の非負・同一行での両建て禁止)の有無 ④OPS 帳簿とファンド帳簿の勘定科目セットが分離されており、実費→架空資金のみなし記帳が FK で物理的に防がれるか(0006 のシードを確認)⑤post_entry/reverse_entry/post_fill とスキーマ制約の整合 ⑥設計文書の主張と実装の乖離。""",

    # 3b. ガバナンス(再監査)— 0013/0019/0021/0029 と ops/reminders.yaml を渡し漏れたため再実行
    "pass3b-governance-schema": read("config/governance.yaml") + read("migrations/0013_governance_assets.sql") + read("migrations/0019_decisions_deemed.sql") + read("migrations/0021_decision_vetoes.sql") + read("migrations/0029_decision_reviewed_sha.sql") + read("ops/reminders.yaml") + read_glob("src/ryza/governance/decisions.py") + """

【監査対象】ガバナンススキーマ(0013/0019/0021/0029)・ops/reminders.yaml・承認記録実装。今回はスキーマ DDL 全文とリマインダー台帳を提供している。
【検査項目】①governance.minutes / stances / decisions の追記オンリー・改竄防止が DB 制約(トリガ・REVOKE)で強制されているか ②decisions/vetoes のスキーマに偽装や事後改変の余地がないか(CHECK・UNIQUE・FK の穴)③ops/reminders.yaml の構造 — 統制の発火期日を担うファイルとして、status/date の書換をどう検出できるか ④reviewed_sha(0029)の記録経路とスキーマ制約 ⑤スキーマと実装(decisions.py)の整合。""",

    # 1. 帳簿・会計 — 不変原則2(二系統分離)・3(証憑とリネージ)
    "pass1-ledger": DESIGN + read_glob("src/ryza/ledger/*.py") + read_glob("migrations/0002*.sql") + read_glob("migrations/0003*.sql") + """

【監査対象】会計エンジン(ledger)と関連スキーマ。
【検査項目】①取引=デモ/実・会計=架空/実費の二系統分離がスキーマ制約で強制されているか(book_id をまたぐ参照の禁止)②仕訳の evidence_id 必須が実装・スキーマの両方で強制されているか ③複式簿記の不変条件(借方=貸方)の強制箇所 ④LLM が会計経路に入り込む余地 ⑤設計文書の主張と実装の乖離。""",

    # 2. 取引経路 — 不変原則1(LLM は判断材料のみ)
    "pass2-trading": DESIGN + read_glob("src/ryza/gate/*.py") + read_glob("src/ryza/risk/*.py") + read_glob("src/ryza/fm/*.py")[:400000] + """

【監査対象】コンプライアンスゲート・リスクエンジン・FM(ファンドマネージャ)層。
【検査項目】①LLM 出力が発注・サイジング・リスク経路に直接入る箇所がないか(不変原則1)②ゲートが「唯一の発注経路」である保証(迂回経路の有無)③fail-closed 原則の一貫性(データ欠落・エンジン停止時に閉じる側に倒れるか)④リスクリミット(dd_hard 等)の解除経路 ⑤設計文書の主張と実装の乖離。""",

    # 3. ガバナンス・監査コード
    "pass3-governance": DESIGN + read("config/governance.yaml") + read_glob("src/ryza/audit/*.py") + read_glob("src/ryza/governance/*.py") + read("src/ryza/reviews.py") + """

【監査対象】保護領域制度(governance.yaml)・A-18 監査・承認記録・意見書パーサ。
【検査項目】①保護領域変更の検出に穴がないか(protected_areas の glob 漏れ・検出ロジックの迂回)②承認記録(Approved トレーラ・decisions)の偽装耐性 ③A-18 の各検査が「検査対象を実装から独立に参照」しているか(実装を書き換えると検査も黙る自己参照になっていないか)④意見書 front matter 処理の迂回口 ⑤監査コード自身は誰が監査するか(自己監査の盲点)。""",

    # 4. セキュリティ・入力境界・依存
    "pass4-security": read("pyproject.toml") + read_glob("src/ryza/bot/*.py") + read_glob("src/ryza/ingest/*.py") + read_glob("src/ryza/ops/*.py") + read_glob("ops/*.sh") + """

【監査対象】外部入力境界(Discord bot・データ取込・GitHub クライアント)・デプロイスクリプト・依存関係。
【検査項目】①インジェクション(SQL・コマンド・プロンプト)②秘密情報の扱い(ログ・エラーメッセージ・コミットへの漏出)③外部 API 応答の検証(型・範囲・悪意ある応答への耐性)④Discord からの指示の認可(誰の発言でも実行するか)⑤依存パッケージの既知の懸念 ⑥デプロイスクリプトの安全性。""",

    # 5. テストの穴
    "pass5-tests": read("config/governance.yaml", 200) + "\n===== テスト一覧(ファイル名と収集結果)=====\n" + "\n".join(
        str(p.relative_to(REPO)) for p in sorted(REPO.glob("tests/**/*.py"))
    ) + read_glob("tests/test_ledger*.py") + read_glob("tests/gate/*.py") + read_glob("tests/risk/*.py") + """

【監査対象】テストスイートの構造(全ファイル一覧)と重要領域(ledger・gate・risk)のテスト本文。
【検査項目】①重要統制(二系統分離・evidence 必須・fail-closed・リミット執行)のうちテストで固定されていないもの ②vacuous なテスト(前提が成立せず常に緑になるもの)③モックが実装の契約とずれ得る箇所(偽実装が本物より寛容)④境界値・異常系の欠落 ⑤invariant_tests 登録(governance.yaml)の網羅性。""",
}

if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for name, prompt in PASSES.items():
        if only and only != name:
            continue
        print(f"[{name}] prompt chars={len(prompt)}", flush=True)
        try:
            call(name, prompt)
        except Exception as e:  # 1パス失敗で全体を止めない
            print(f"[{name}] FAILED: {e}", flush=True)
    print("ALL DONE", flush=True)
