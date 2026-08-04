---
review: t024-evidence-store-required
reviewed_sha: 41cec1bf810ae3dd059f4e70ae4469fd33e19450
reviewer: independent-reviewer (opus)
review_date: 2026-08-04
verdict: approve
---

# T-024 独立審査意見書

## 検証ログ
- HEAD SHA: 41cec1bf810ae3dd059f4e70ae4469fd33e19450
- ベース: origin/main
- 対象: 監査所見 F-7 (Issue #122) の是正
- 変更ファイル: docs/tasks/T-024-*.md / src/ryza/secrets.py / src/ryza/ledger/_util.py / tests/conftest.py / tests/ledger/test_evidence_gce_guard.py / tests/test_secrets.py

## コード読解フェーズ所見

### fail-closed の完全性
- `_evidence_store()` は `RYZA_EVIDENCE_DIR` が未設定かつ GCE のとき、`_evidence_store_for()` 呼び出し以前・`store.store()` 呼び出し以前に `RuntimeError` を送出する。`create_evidence()` の DB 挿入は `store is not None` 分岐か else 分岐(インライン)にしかなく、両方とも `_evidence_store()` を先に呼ぶため、GCE ガード発火時は 1 行も INSERT が走らない。副作用ゼロを確認 (src/ryza/ledger/_util.py L157-L187)
- 例外メッセージには「RYZA_EVIDENCE_DIR」「不変」「A-12 F-7」「T-024」が含まれ、是正方法(パスの設定)と理由が明記されている

### GCE 判定
- `is_running_on_gce()` は `access_secret` と同一の `_METADATA_TOKEN_URL`(metadata.google.internal のトークンエンドポイント)へ `Metadata-Flavor: Google` で HEAD 相当(GET だが close()) を打つ。成功時 True、OSError/TimeoutError/URLError で False キャッシュ
- キャッシュは module-global `_gce_cache: bool | None`。プロセス生存中は永続

### 非 GCE 経路の不変性
- `_evidence_store()` の非 GCE 分岐は従来の「env なし → None」ロジックのまま。tests/ledger/test_provenance_integration.py `test_inline_fallback_without_env` が担保する

### 統制迂回口の非存在(確認)
- 全リポジトリ grep で `RYZA_EVIDENCE_DIR` / `RYZA_FORCE_INLINE` を確認 → 迂回用の env var は存在しない
- `_evidence_store()` 内には env unset + GCE から先に進む経路が無い(唯一の分岐は raise)

### 適用範囲確認(F-7 の穴の他経路)
- ledger 内の `INSERT INTO ledger.evidence` は 2 か所: `src/ryza/ledger/_util.py:181`(インライン経路 — ガード対象)と `src/ryza/provenance/evidence.py:221`(EvidenceStore.store — ストア経由なので原本は DB 外)。前者はガード発火で raise、後者は原本が DB 外にあるので F-7 の攻撃対象外
- `src/ryza/ingest/base.py:default_store()` は別系統(RYZA_EVIDENCE_BUCKET / RYZA_EVIDENCE_ROOT)を使い、常に EvidenceStore を返す — インラインには落ちないので F-7 の穴は無い

### テスト実行結果
- `RYZA_TEST_DATABASE_URL=postgresql://ryza:ryza@localhost:15432/ryza PYTHONPATH=$PWD/src pytest tests/ledger/test_evidence_gce_guard.py tests/ledger/test_provenance_integration.py tests/test_secrets.py -q` → **25 passed in 22.43s**
- `ruff check` → All checks passed

## 所見

### 重大: なし

### 中: 命名と検出範囲のズレ(残余リスクだが受容可能)

`is_running_on_gce()` は名前に反して「GCP メタデータサーバへ到達可能な任意の GCP 実行環境」を検出する — GCE VM だけでなく **Cloud Run(Jobs / Services)、GKE、Cloud Functions gen2、Cloud Build、App Engine Flex** も同じ `metadata.google.internal` を提供し、同じ `Metadata-Flavor: Google` を返す。docs/research/gcp-demo-environments.md によればこのシステムは **Cloud Run Jobs を日次バッチで使う設計**であり、常駐は GCE でもバッチが Cloud Run 上で証憑を作れば「Cloud Run で ledger.create_evidence が呼ばれる」経路は現実にあり得る。

これは**むしろ望ましい挙動**である(F-7 の意図は「GCP 本番でインライン証憑を作らせない」であり、Cloud Run も同じ本番であるため)。ただし関数名 `is_running_on_gce` は誤読を招く。将来 Cloud Run で証憑を作る運用が発生したときに「GCE ではないのにガードが発火する」と誤診断される可能性がある。

**推奨(request_changes 未満の助言)**:
- 関数名を `is_running_on_gcp_compute()` 相当に改名するか、docstring の「GCE 上で動いているか」を「GCP のメタデータサーバに到達可能か(= GCE / Cloud Run / GKE / Cloud Functions 等)」に明記する
- ただし本 PR の実装は正しく、意味論的な穴は無いため verdict を下げる要因ではない

### 中: メタデータサーバの一時障害と False キャッシュの永続化

`is_running_on_gce()` は初回問い合わせに失敗すると `_gce_cache = False` をプロセス生存中永続化する。GCE のメタデータサーバは高可用だが、起動直後の DHCP 遅延・ネットワーク初期化ラグ・iptables 適用中などで **一時的に到達失敗する既知パターン**がある(Google の公式ドキュメント "Troubleshooting metadata server" に記載あり)。仮に GCE 本番プロセスの最初の初期化フェーズで `is_running_on_gce()` が呼ばれてタイムアウトした場合、以後**そのプロセスは自分を非 GCE と誤認**し、`RYZA_EVIDENCE_DIR` 未設定で本番でインライン経路に落ちる(=まさに F-7 が防ぎたい状態)。

現実的緩和:
- 本番の GCE VM では `RYZA_EVIDENCE_DIR` を systemd EnvironmentFile 等で**常に**設定する運用が正で、env が設定されている限り `is_running_on_gce()` は呼ばれない(`_evidence_store()` L87-L89: env があれば即 return し GCE 判定に到達しない)ため、実害シナリオは「本番で env 未設定+デプロイ直後にメタデータ一時障害」の同時発生に限られる
- タイムアウトは 1.0 秒。GCE のメタデータは通常ミリ秒単位。1 秒で失敗するのは相当な異常
- 逆キャッシュ(True → False)の遷移が起きないことは意図的(証憑作成のたびに問い合わせない目的)

**推奨(request_changes 未満の助言)**:
- 「False キャッシュを一定時間で expire する」か「タイムアウトを 3〜5 秒に延ばす」検討余地はあるが、本 PR の受け入れ基準には含まれない
- docs/reviews に「メタデータサーバ一時障害時の残余リスクは env 未設定運用と同時発生でのみ顕在化する」と明記して受容記録を残すのが最小限の追加コスト

### 軽微: `tests/conftest.py` の `_gce_cache = False` 固定が GCE 上の CI で誤診断を隠す

conftest.py L73 で `secrets._gce_cache = False` を無条件に設定する。これはローカル・非 GCE CI では正しい(実メタデータへ問い合わせに行かない)が、**もし将来 CI を GCE 上の Cloud Build や GKE で走らせるようになった場合**、本物の GCE 判定が上書きされ「GCE 本番相当環境で env 設定漏れがあってもテストがそれを見逃す」状態になる。現状の CI 実行環境は GitHub Actions(GCE ではない)なので実害は無い。

軽微に留まる理由:
- テスト自体は `_force_gce(True)` を明示するテスト(guard の発火確認)と `_force_gce(False)` を明示するテスト(インラインフォールバック確認)の両方を持ち、CI 環境依存にならない設計になっている
- 実本番デプロイの検証は本来 CI ではなく IaC / smoke test の責務

### 軽微: `reset_gce_cache()` の役割が secrets.py の他コード用途と不整合

`reset_gce_cache()` は本 PR で追加され、テストからの用途しかない。プロダクションコードで「メタデータサーバの障害から復旧したら再チェックしたい」用途があるかは疑問だが、少なくとも本 PR の受け入れ基準には含まれず、`__all__` にも露出しているため将来の運用ツールが使う余地は確保されている。

### 軽微: `test_gce_detection_cached_across_calls` の `saved_cache` 復元とautouse フィクスチャの重複

`tests/ledger/test_evidence_gce_guard.py` の autouse `_restore_gce_cache` が各テスト後に `_gce_cache = False` に戻す一方、`test_gce_detection_cached_across_calls` は独自に `saved_cache = secrets._gce_cache` を保存し finally で復元している(L103, L125)。二重防御で害はないが、autouse があれば冗長。可読性の観点で片方に寄せるべきだが、機能的な問題は無い。

## 受け入れ基準との突合(docs/tasks/T-024-evidence-store-required.md §受け入れ基準)

| 基準 | 結果 |
|---|---|
| 全テスト+ruff 通過 | ✔ 25 passed / ruff clean |
| 開発・CI の挙動不変 | ✔ `test_inline_fallback_without_env` / `test_non_gce_and_env_unset_falls_back_to_inline` 双方 pass |
| 統制迂回の環境変数を作らない | ✔ grep で確認、`_evidence_store()` に迂回口なし |
| LLM 非関与 | ✔ 決定論的コードのみ |
| コミットは日本語 + Co-Authored-By | ✔ (git log で確認済み)|
| push しない(統合は設計リード) | ✔ |
| GCE 検出は secrets.py の既存メタデータ検出ロジックを再利用 | ✔ 同一 URL・同一ヘッダ・同一例外群 |
| 検出結果はプロセス内でキャッシュし証憑作成のたびに問い合わせない | ✔ `test_gce_detection_cached_across_calls` |
| 例外メッセージに是正方法(RYZA_EVIDENCE_DIR)と理由(不変保存)を明記 | ✔ メッセージ内容確認 |
| 「起動時エラー」の解釈(インライン経路への最初の到達点で raise、証憑を1件も書かずに死ぬ) | ✔ `test_gce_and_env_unset_raises_before_writing_any_evidence` で確認 |

## verdict

**approve** — 受け入れ基準を完全に満たし、fail-closed の完全性・統制迂回口の非存在・非 GCE 経路の不変性のすべてが検証済み。25/25 テスト pass、ruff clean。

残余リスクは (a) 関数名 `is_running_on_gce` が Cloud Run / GKE 等も含む GCP メタデータ検出であることの誤読可能性、(b) メタデータサーバ一時障害での False 永続キャッシュ、の 2 点だが、いずれも「env を本番で常に設定する」運用と同時発生でしか顕在化しない受容可能な残余リスクである。改善提案は非ブロッキングとして中程度の所見に記録した。

## 反対意見書(追従禁止規定に基づく)

approve に反対すべき点を探して見つからなかった。強いて反対の立場を採るなら:

1. **関数名は誤解を生むので改名すべき** → しかし本 PR の実装は意味論的に正しく、後日の renaming PR で対応可能な範囲
2. **False キャッシュの永続化は本番の一時障害で危険** → しかし env 未設定運用と同時発生でのみ顕在化し、確率は極めて低い。運用側で env 設定を強制する方が本質的
3. **`load_evidence_payload` の GCE 判定は不要な読み取り側で発火する可能性** → env が設定されていれば `_evidence_store()` は L87 で即 return し GCE 判定に到達しないため、正常運用では発火しない

いずれも approve を覆すには弱い。
