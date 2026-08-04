# T-020: A-18-9 reminders 台帳の改変検査(A-12 是正 F-1)

- 起草: 2026-08-04 設計リード / 前提: **Issue #118(a12-fix-audit-code)が main に統合済みであること**(a18.py の競合回避)
- 出典: A-12 監査所見 A-12-15(裁定: 重要)・裁定書 `docs/reviews/a12/00-adjudication.md` §3 F-1・是正案修正の経緯は Issue #117 コメント(2026-08-04 設計リード裁定)
- 前提知識: CLAUDE.md(将来アクションの制度化)、`src/ryza/audit/a18.py`(A-18-1〜A-18-8 の流儀)、`ops/reminders.yaml` の様式、`config/governance.yaml` の approval_trailer

## 目的

`ops/reminders.yaml` は統制の発火期日(trailer-v1-sunset 等)を定義する台帳だが、保護領域外にあり、無承認コミットで期日・status を書き換えれば制度の発火を無音で止められる(A-12-15)。一方、直近1週間で全コミットの 35%(166/473)が本ファイルに触れており、protected_areas への全体登録は「1/3 の PR に独立審査+48h」を課してリマインダー登録の逆インセンティブを生む。そこで**疑わしい変更だけを検出する semantic tamper check** を A-18 の新検査(A-18-9)として実装する。

## 検査仕様

対象: 基準コミット以降(A-18-1 と同じ since_commit の流儀)の、`ops/reminders.yaml` に触れるコミットのうち **`Approved:` トレーラの無いもの**。トレーラ付き(承認済み変更)は対象外。マージコミットの扱いは A-18-1 の反復流儀に合わせる。

各対象コミットについて、変更前後の YAML をパースし、リマインダーを `id` で突合して次の3種のみを所見にする:

1. **期日の後ろ倒し**: `status: pending` のエントリの `conditions[].date_after.date` が**より遅い日付**に変わった(前倒しは対象外)
2. **pending エントリの削除**: 変更前に `status: pending` だったエントリが変更後に存在しない(id 改名は削除+追加に見えるが、改名も承認かエントリ内の経緯記載を要する運用とし、削除として鳴らす)
3. **証跡なしの done/fired 化**: `pending` → `done`/`fired`/`superseded` 等の終端遷移で、当該コミットの**当該エントリの diff ハンク**に証跡参照(7〜40桁 hex の SHA・`#\d+` の PR/Issue 番号・URL のいずれか)が含まれない(現行運用は `status: done # 2026-08-04 …(b4f21b6)` のように YAML コメントで証跡を書くため、パース後の値ではなく **diff の生テキスト**で判定する)

無音で通すもの: エントリの新規追加・証跡付きの終端遷移・期日の前倒し・コメントや `what` の文言変更・上記以外のフィールド変更。

### fail-closed の扱い

- 変更前後いずれかの YAML がパース不能 → 検査をスキップせず「パース不能で検査できなかったコミット」として件数を開示する(黙って緑にしない — A-18 の一貫原則)
- ファイルの改名・削除そのもの → 所見(台帳の消失は最も強い改変)

## 実装

- `src/ryza/audit/a18.py` に検査関数(例: `check_reminder_tampering`)+ dataclass(所見・検査コミット数・パース不能数)を追加。git 操作は既存ヘルパ(`_git` / `_rev_list`)を再利用
- `run_a18` に配線し、結果 dict と `build_alert_embed` に反映(所見ありは ⚠️、無しは ✅ と検査分母。A-18-7 の表示流儀に合わせる)
- docstring に「なぜ全体保護でなく semantic check か」(35% 実測・逆インセンティブ・Issue #117)を書く
- `tests/audit/test_a18.py` にテスト: ①後ろ倒し検出 ②前倒しは無音 ③pending 削除検出 ④証跡なし done 検出 ⑤証跡(SHA/PR 番号)付き done は無音 ⑥トレーラ付きコミットは対象外 ⑦追加のみは無音 ⑧パース不能の開示。既存の一時リポジトリ fixture の流儀に従う
- `ops/reminders.yaml` の既存エントリ `reminders-status-tamper-detection`(2026-08-25)を本実装の証跡付きで `superseded` に更新する(本検査がその決定の実装そのものである)

## 受け入れ基準

- 上記テスト 8 観点がすべて実装され、`pytest tests/audit/test_a18.py -q` 全件パス・ruff クリーン
- 検出3種の定義が仕様どおり(過剰検出で通常運用の PR が鳴らないこと — 直近 main の履歴に対して所見ゼロであることを確認し、報告に含める)
- 監査コード(`src/ryza/audit/**`)は保護領域 — 実装後に独立審査+承認記録が必要(実装エージェントの範囲外)
