---
review: t025-deploy-hardening
reviewed_sha: c41cf33fce930de8b4cb44bd2b0c60a86b24445c
reviewer: independent-review-agent (opus)
review_date: 2026-08-04
verdict: approve
---

# 独立役員意見書: PR #137 (T-025 デプロイ経路の堅牢化)

## 対象

- ブランチ: origin/t025-deploy-hardening
- HEAD: c41cf33fce930de8b4cb44bd2b0c60a86b24445c
- コミット: 4 件
  - 8b65969 docs(tasks): T-025 実装指示書
  - dd3833c feat(deploy): F-8 ロール名 env の SQL 識別子検証
  - e1db3e4 feat(deploy): F-13 uv sync --locked 統一
  - c41cf33 fix(deploy): Python 3.12 ピン復元・CI 言及コメント是正
- 変更ファイル: 6 (net +337 -8)

## 実行した検証

### 1. `bash -n` 構文検査

対象: ops/lib/sql_ident_check.sh / ops/deploy-dashboard.sh / ops/deploy-bot.sh / ops/deploy-daily.sh

結果: **全て構文エラーなし**(下記詳細セクション参照)。

### 2. pytest (tests/ops/ 全体・DB 不要のもの)

コマンド:
```
cd /tmp/review-t025 && PYTHONPATH=/tmp/review-t025/src \
  /Users/mmiyazaki/Projects/sukifura/ryza/.venv/bin/python -m pytest tests/ops/ -q
```

結果: **199 passed in 17.00s**

- test_sql_ident_check.py: 36 passed(新規)
- test_deploy_guards.py / test_deploy_role_gate.py / test_github.py / test_icon_revalidate.py / test_org_icon_overrides.py / test_weekly.py: 既存全通過

### 3. tests/ops/test_sql_ident_check.py の網羅性チェック

観点別に照合(仕様書 §テスト の「(a) 正当・(b) 大文字/ハイフン/空白/引用符/セミコロン/先頭数字/64バイト超・(c) エラーメッセージに理由」):

| ケース | テスト | 判定 |
|---|---|---|
| 正常系 (ryza/ryza_dashboard/ryza_boardroom/_underscore/abc123/1文字/63文字) | test_valid_identifiers_pass | 網羅 |
| 大文字 (Ryza / RYZA) | test_invalid | 網羅 |
| ハイフン (ryza-dashboard) | test_invalid | 網羅 |
| 空白 (単/複/タブ/改行) | test_invalid | 網羅 |
| ダブルクォート | test_invalid | 網羅 |
| シングルクォート | test_invalid | 網羅 |
| セミコロン | test_invalid | 網羅 |
| 先頭数字 (1abc) | test_invalid | 網羅 |
| ドット・カンマ・コメント記号 (`--` `/**/`) | test_invalid | 網羅(仕様書要求を超過) |
| 非 ASCII (ryzä / 役員室) | test_invalid | 網羅(仕様書要求を超過) |
| 64バイト超 (a*64 / a*128) | test_invalid | 網羅 |
| 空文字 | test_invalid | 網羅 |
| 引数不足(呼び出し側バグ) | test_missing_argument_is_treated_as_caller_bug | 網羅 |
| stdout に生値を吐かない(制御文字) | test_error_diagnostic_does_not_emit_raw_value_to_stdout | 網羅 |
| set -e 下で `\|\| exit 1` 経由の中断挙動 | test_function_survives_set_e | 網羅 |
| 本体スクリプトとの配線ドリフト防止 | test_deploy_script_sources_the_library / test_deploy_script_validates_role_envs_with_abort / test_deploy_script_validates_before_sql_generation | 網羅 |

**評価: 仕様書テスト要件を満たし、追加観点(呼び出し側配線・SQL 生成前検証順序)も自主的に補強している。**

### 4. SQL 識別子 env 埋め込みの適用範囲走査(観点2)

コマンド: `Grep 'CREATE ROLE|CREATE USER|ALTER ROLE|ALTER USER|GRANT|REVOKE|CREATE DATABASE|DROP|CREATE EXTENSION|CREATE SCHEMA' path=/tmp/review-t025/ops` および ops 配下 `psql` 呼び出しの網羅走査。

対象コード上の SQL 埋め込み箇所と env 由来識別子の有無:

| ファイル | SQL 埋め込み | 識別子 env 由来 | assert_sql_ident 保護 |
|---|---|---|---|
| ops/deploy-dashboard.sh | Python ヒアドキュメント内 `.replace()` で ROLE/DB を差し込み | ✅ (RYZA_DASH_ROLE / RYZA_BR_ROLE / RYZA_OWNER / RYZA_DB) | **全4 env 保護済み**(§0.0) |
| ops/deploy-bot.sh L122-130 | `CREATE ROLE ryza LOGIN PASSWORD 'ryza'` 他 | ❌ **リテラル**(env 未使用) | 対象外(不要) |
| ops/deploy-daily.sh | SQL 埋め込みなし(migrations 経由) | ─ | 対象外 |
| ops/deploy-a18.sh | SQL 埋め込みなし(DATABASE_URL 参照のみ) | ─ | 対象外 |

**評価: 現在の適用範囲は網羅している。仕様書では「ロール名 env」を要求していたが、実装は同じテンプレートに埋め込まれる `RYZA_DB`(=DB_NAME)にも自主拡張して検査している(deploy-dashboard.sh L118-124 のコメントで判断根拠を明示)。判断は正当 — 同じ入口で片方だけ検査すると `"; DROP …` 型の値が DB 名では素通りする不整合を生む。**

Regex `^[a-z_][a-z0-9_]*$` を通り抜ける値の分析:
- 予約語(`select`/`role`/`user` 等): Regex は通るが、生成 SQL は `"ryza_dashboard"` のようにダブルクォート付きで埋め込まれるため予約語でも識別子として通用する(pg 側 folding 無効化)。**実害なし**。
- ASCII 数字混在(`abc123`): 通す(仕様通り)。悪用経路なし。
- 唯一のリスクは、将来 quote 無しで埋め込む新規箇所が追加された場合。今回の SQL テンプレートは全ての `__…__` 位置に `"…"` を付けており、Regex(小英字+数字+`_`)は unquoted 識別子文法とも整合するため二重に安全。

### 5. 既存機能の挙動保全(観点3・uv sync 移行の運用リスク)

`uv pip install -e '.[bot]'` → `uv sync --locked --extra bot --python 3.12` の移行で発生する挙動差:

| 挙動 | 旧 (pip install -e) | 新 (sync --locked) | 影響 |
|---|---|---|---|
| 依存解決 | 毎回 pyproject の `>=` を最新解決 | uv.lock で固定 | **是正の本旨(F-13)** |
| editable install | あり(`-e`) | あり(`uv sync` は既定でルートを editable) | 同等 |
| lockfile 外パッケージ | 保持 | **削除**(`uv sync` の既定) | 該当 VM は `deploy-bot.sh` 以外に pip 追加をしていない設計なので実質無害 |
| .venv 再作成 | `[ -d .venv ] || uv venv` で既存流用 | `uv sync --python 3.12` は既存 `.venv` を流用(Python が一致すれば) | 同等 |
| Python バージョンピン | `uv venv --python 3.12` で明示 | `--python 3.12` を e1db3e4 で欠落 → **c41cf33 で復元** | **修正済み** |
| tar への uv.lock 同梱 | 依存せず | 必須(欠落時 exit 1) | tar 未同梱時に「デプロイ資材の欠落」で明示的に落ちる(L155-158) |

uv.lock は Git 追跡下(`.gitattributes` に `export-ignore` なし)で `git archive HEAD` 経由の tar に含まれることを確認。存在チェックも実装済み。

**評価: 挙動差は F-13 の本旨に沿った是正であり、運用リスクは c41cf33 の Python ピン復元で解消。**

### 6. 保護領域統制(観点5)

`git diff --name-only origin/main..HEAD`:
```
docs/tasks/T-025-deploy-hardening.md
ops/deploy-bot.sh
ops/deploy-daily.sh
ops/deploy-dashboard.sh
ops/lib/sql_ident_check.sh
tests/ops/test_sql_ident_check.py
```

保護領域(定款第5条 / governance.yaml)の照合:
- deploy_path(ops/deploy-*.sh, ops/lib/*): 該当・変更あり(F-8/F-13 の対象)
- migrations: 変更なし ✅
- 会計エンジン(src/ryza/accounting): 変更なし ✅
- 監査コード / IPS / マンデート / 定款 / governance.yaml / CLAUDE.md / 執筆規格: 変更なし ✅

**評価: 保護領域変更はデプロイ経路のみで、指示書スコープと一致。**

## 所見

### 情報-1: 仕様書との軽微な逸脱(自己申告済み)

`assert_sql_ident RYZA_DB` の追加は仕様書「ロール名 env」を超過するが、判断根拠は
deploy-dashboard.sh L117-120 のコメントで明示され、完了報告での明示も指示書 §受け入れ基準
に沿っている。**逸脱として妥当。所見は情報止まり(是正不要)**。

### 情報-2: 検証呼び出し順序が SQL 生成より前であることのテストが独立して存在

`test_deploy_script_validates_before_sql_generation` は「検証を後で行っても手遅れ」
という統制設計の意図をテストとして固着させている。ドリフト防止の価値が高く、
質の高い実装。**情報のみ**。

### 情報-3: c41cf33 の是正が是正の是正である点

e1db3e4 で `--python 3.12` ピンを一時的に落としてしまい、c41cf33 で復元した経緯が
コミット履歴に残る。運用上のリスクは復元により解消しているが、レビュー観点では
「1コミット目の完成度が不十分だった」ことが履歴に見える。将来の類似 PR で
「移行時のバージョンピン継承」をチェックリスト化するなら価値がある。
**所見は情報止まり(是正不要)**。

### 軽微-1: `LC_ALL=C` の適用範囲がコマンド単位に留まる(sql_ident_check.sh L71)

`LC_ALL=C awk -v v="…" 'BEGIN { … }'` は awk 起動時のみ C ロケールで、シェル側で
`[[ =~ ]]` を使っていないため実害はない。**現状は問題なし**が、将来 `[[ =~ ]]`
に置換された場合の落とし穴として本ファイル L68-70 のコメントで注意喚起済み。
**是正不要。情報として記録。**

### 軽微-2: `wc -c` の LC_ALL=C 化(L61)

`LC_ALL=C printf '%s' … | wc -c` はバイト数を返す。Regex で ASCII 以外を弾いた
後にしか使わないため実質 byte==char で正しいが、順序が「長さ → Regex」であるため、
非 ASCII 混入時に**先に長さ検査で落ちる**ケースがある(例: `役員室` の UTF-8 表現は
9 バイトなので長さ検査は通り、Regex で落ちる → 正しい診断が出る)。一方 63 バイト超の
非 ASCII 文字列を渡すと「長すぎる」で落ち、Regex 診断は出ない。**運用者へのメッセージが
Regex ではなく長さになる**が、いずれにせよ非ゼロ終了で SQL 埋め込みは止まる。
**動作は正しい。所見は情報止まり。**

## verdict の根拠

- 仕様適合: F-8 の実装要件・テスト要件・受け入れ基準を全て満たし、判断根拠のある自主拡張(RYZA_DB)を伴う。F-13 は `uv sync --locked` に統一済みで uv.lock の tar 同梱と欠落検出も実装。
- 適用範囲: ops/ 配下で SQL 識別子として env を差し込む唯一の箇所(deploy-dashboard.sh)は完全にカバー。他ファイルは env 経路を持たない。
- 既存機能: 挙動差はいずれも是正の本旨か中立で、Python 3.12 ピンの一時欠落は c41cf33 で復元済み。
- テスト品質: 36 ケースで仕様書要求を網羅+ドリフト防止テストで超過的に補強。全 199 件通過。
- 保護領域: 変更は deploy_path のみで指示書スコープと一致。migrations・会計・監査・定款には及ばず。

重大・中の所見なし。軽微・情報のみ。**verdict: approve**。

---

**verdict: approve**(front matter を書き換え)

