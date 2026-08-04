# T-024: 本番環境での証憑ストア必須化(F-7 — Issue #122)

- 起草: 2026-08-04 設計リード / 対象: A-12 監査所見 pass1b-2(裁定 F-7・中)
- 前提知識: CLAUDE.md、docs/reviews/a12/00-adjudication.md §3 F-7、src/ryza/ledger/_util.py(`_evidence_store` L66-73)、src/ryza/provenance/evidence.py(EvidenceStore)、src/ryza/secrets.py(GCE メタデータ検出 L50-64)
- **保護領域**(会計エンジン)。統合は設計リードが独立役員審査+みなし承認手続で行う
- 本仕様書自体を実装ブランチの最初のコミットとして `docs/tasks/T-024-evidence-store-required.md` に含めること

## 問題(pass1b-2)

`RYZA_EVIDENCE_DIR` 未設定時、`create_evidence` は payload を `payload_ref` に JSON インラインで格納する。インライン証憑は DB 行そのものが原本であり、DB を直接操作すれば sha256 ごと書き換えられる — ファイルストアが持つ「DB 外の対照物」による不変性担保が働かず、設計書 §4 の不変保存が形骸化する。開発環境の利便性としてのインラインは妥当だが、本番(GCE)でこの経路に落ちることは統制欠陥である。

## 是正方針(設計リード裁定)

**本番(GCE 上で実行中)では RYZA_EVIDENCE_DIR を必須とし、未設定でインライン経路に入る瞬間に fail-closed で例外を送出する**。ローカル・CI・テストの挙動は変えない。

- 「起動時エラー」(裁定原文)の解釈: ジョブのエントリポイントは複数あり共通初期化点が薄いため、**インライン経路への最初の到達点で raise** する(= 証憑を1件も書かずに死ぬ)。これは起動直後の最初の記帳で顕在化し、裁定の意図(本番でインライン証憑を1件も作らせない)を満たす。純粋な読み取りジョブが無関係に死なない利点もある
- GCE 検出は src/ryza/secrets.py の既存メタデータ検出ロジックを再利用する(重複実装しない。必要なら secrets.py の検出部を小さな共通関数に抽出してよい — その場合 secrets.py の既存挙動は不変であること)。検出結果はプロセス内でキャッシュし、証憑作成のたびにメタデータサーバへ問い合わせない
- 例外メッセージには是正方法(RYZA_EVIDENCE_DIR の設定)と理由(インライン証憑は不変保存を満たさない)を明記

## 実装

1. `src/ryza/ledger/_util.py` の `_evidence_store()`(または `create_evidence` のインラインフォールバック直前)に GCE ガードを追加
2. GCE 検出ヘルパー: metadata サーバ到達性ベース(secrets.py と同一の判定基準)。環境変数での明示上書き(例 `RYZA_FORCE_INLINE_EVIDENCE=1` のような逃げ道)は**作らない** — 統制の迂回口になる
3. provenance 側(EvidenceStore)は変更不要のはず。インライン経路が ledger 側にしか無いことを確認し、他にもインライン格納経路があれば同じガードを適用して報告

## テスト(tests/ledger/ — 既存流儀)

1. GCE 検出を monkeypatch で真にし RYZA_EVIDENCE_DIR 未設定 → create_evidence が例外(メッセージに RYZA_EVIDENCE_DIR を含む)
2. GCE 真+RYZA_EVIDENCE_DIR 設定(tmp_path)→ 正常にファイルストア格納
3. GCE 偽+未設定 → 従来どおりインライン(開発環境の互換)
4. 検出キャッシュ: メタデータ問い合わせが複数回の証憑作成で1回しか走らない
5. 既存スイート全通過(CI 環境は GCE ではないため挙動不変のはず)

## 受け入れ基準

全テスト+ruff 通過 / 開発・CI の挙動不変 / 統制迂回の環境変数を作らない / LLM 非関与 / コミットは日本語+`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`、push しない(統合は設計リードが行う)。DB テストは `RYZA_DATABASE_URL=postgresql://ryza:ryza@localhost:15432/ryza`、worktree では `PYTHONPATH=$PWD/src` 必須
