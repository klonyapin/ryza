# GCP IAM 棚卸し(ryza-main)

- 対象プロジェクト: `ryza-main`(プロジェクト番号 287074591154・組織に属さない単独プロジェクト)
- as_of: **2026-08-04**(すべて読み取りコマンドの実測。§7 に再現手順)
- 根拠となる指摘: `ops/reminders.yaml` の `gcp-iam-inventory`、`docs/reviews/ops-weekly-vm-independent-review.md` L11・L21(低・旧経路の IAM 残滓)
- 本書は**棚卸しと提案のみ**で、IAM の変更は一切行っていない。是正の実施は `ops/reminders.yaml` の `gcp-iam-remediation`(期限 2026-08-24)に分離した

このプロジェクトの権限設計は「Secret だけが最小権限で、それ以外は無制限」という二極構造になっている。Secret の payload 読み取りは Secret 単位の `secretAccessor` で正しく絞られている一方、その同じサービスアカウント(以下 SA)がプロジェクト全体に `roles/editor`(11,889 権限)を持つため、Secret を除くすべての資源 —— VM・Cloud Run・Artifact Registry・BigQuery・Scheduler —— を作成・改変・削除できる。しかも `compute.instances.setMetadata` を含むため、この SA の資格情報を得た者は VM に SSH 鍵を注入して root を取り、結局は VM が読める全 Secret に到達する。したがって Secret 単位の絞り込みは**同一 SA からの経路に対しては実効性を持たない**。以下は付与の実測、宣言(`ops/deploy-*.sh`)との差分、是正提案の順に述べる。

---

## 1. 現状の権限マトリクス

### 1.1 プロジェクトレベル(`gcloud projects get-iam-policy ryza-main`)

| メンバー | ロール | 種別 | 根拠(宣言元) |
|---|---|---|---|
| `user:mileage_embassy_9x@icloud.com` | `roles/owner` | 人間 | プロジェクト作成者(代表) |
| `serviceAccount:287074591154-compute@developer.gserviceaccount.com` | **`roles/editor`** | 既定 SA | **宣言なし** — GCE 既定 SA への自動付与(プロジェクト作成時) |
| 同上 | `roles/bigquery.dataViewer` | 既定 SA | `ops/deploy-ops-weekly.sh:90-93` |
| 同上 | `roles/run.invoker` | 既定 SA | `ops/deploy-ops-weekly.sh:111-114` |
| `serviceAccount:287074591154@cloudbuild.gserviceaccount.com` | `roles/cloudbuild.builds.builder` | 既定 SA | Cloud Build API 有効化時の自動付与 |
| `serviceAccount:287074591154@cloudservices.gserviceaccount.com` | `roles/compute.instanceGroupManagerServiceAgent` | Google 管理 | API 自動 |
| `service-287074591154@…` 各種(artifactregistry / cloudbuild / cloudscheduler / compute-system / containerregistry / pubsub / serverless-robot) | 各 `*ServiceAgent` | Google 管理 | API 自動。**棚卸しの対象外**(削除すると当該サービスが壊れる) |

`roles/ryza-dashboard` 用 SA(`ryza-dashboard@ryza-main.iam.gserviceaccount.com`)は**プロジェクトレベルの付与を一切持たない**。`ops/deploy-dashboard.sh:607-626` の設計どおりで、SA 分離の唯一の成功例である。

### 1.2 サービスアカウント一覧(`gcloud iam service-accounts list`)

| SA | 用途(実測) | 実行主体 |
|---|---|---|
| `287074591154-compute@developer.gserviceaccount.com` | **3系統を兼務** — ①GCE VM `ryza-bot`(Discord Bot / 日次サイクル / A-18 監査)②Cloud Run Job `ops-weekly` ③Cloud Scheduler `ops-weekly-trigger` の OAuth 発行元 | VM・Cloud Run Job・Scheduler |
| `ryza-dashboard@ryza-main.iam.gserviceaccount.com` | Cloud Run サービス `ryza-dashboard` の実行 SA | Cloud Run |

ユーザー作成 SA は 2 つのみで、いずれも `get-iam-policy` は空(SA 偽装 `actAs` / `getAccessToken` の個別付与はない)。ただし `roles/editor` は `iam.serviceAccounts.actAs` を含むため、既定 Compute SA は `ryza-dashboard` SA として動く Cloud Run リビジョンをデプロイできる。

### 1.3 Secret 単位(`gcloud secrets get-iam-policy <name>`)

| Secret | `secretAccessor` | その他 | 宣言元 |
|---|---|---|---|
| `github-token` | compute SA | — | `deploy-ops-weekly.sh:84`, `deploy-a18.sh:41` |
| `discord-bot-token` | compute SA | **`secretVersionAdder`(compute SA)** | accessor= `deploy-bot.sh:65` / **versionAdder は宣言なし** |
| `anthropic-api-key` | compute SA, ryza-dashboard SA | — | `deploy-daily.sh:74`, `deploy-dashboard.sh:622-626` |
| `ryza-dashboard-db-url` | ryza-dashboard SA | — | `deploy-dashboard.sh:616-620` |
| `ryza-boardroom-db-url` | ryza-dashboard SA | — | 同上 |
| `jquants-api-key` / `jquants-refresh-token` / `fred-api-key` / `estat-app-id` | compute SA | — | **宣言なし**(手動付与。データ取込の実配線に対応) |
| `ryza-db-password` / `ryza-boardroom-db-password` | (バインディングなし) | — | 中間素材。`deploy-dashboard.sh` が実行者権限で読み URL Secret に合成する |

### 1.4 リソース単位

| リソース | ポリシー | 評価 |
|---|---|---|
| Cloud Run サービス `ryza-dashboard` | `roles/run.invoker` = IAP サービスエージェントのみ | 宣言どおり。ただし §1.1 のプロジェクトレベル `run.invoker` が**継承で上乗せ**される(→ P-3) |
| Cloud Run Job `ops-weekly` | 空(`etag: ACAB`) | 起動は §1.1 のプロジェクトレベル `run.invoker` に依存している |
| Artifact Registry `ryza` / `cloud-run-source-deploy` | 空 | プロジェクトレベルの `editor` / `builds.builder` で足りている |
| BigQuery データセット `billing_export` | `projectWriters`=WRITER, `projectOwners`=OWNER, `projectReaders`=READER, `billing-export-bigquery@system`=OWNER, 代表=OWNER | **`projectWriters` は `roles/editor` 保持者** = compute SA。つまり compute SA は請求エクスポートに**書き込み・テーブル削除ができる** |
| IAP(`ryza-dashboard`) | `roles/iap.httpsResourceAccessor` = 代表1名 | 宣言どおり(`deploy-dashboard.sh:674-682`) |
| GCS バケット `ryza-main_cloudbuild` / `run-sources-…` | legacy(projectEditor/Owner/Viewer)のみ | 公開バインディングなし |
| ファイアウォール | `default-allow-ssh` `default-allow-rdp` `default-allow-icmp` が `0.0.0.0/0`、`ryza-allow-dashboard-db` は `10.138.0.0/20`→tcp:5432/tag `ryza-db` | 後者は宣言どおり。前者は default VPC の既定(→ P-6・IAM 外の残課題) |

`allUsers` / `allAuthenticatedUsers` は**プロジェクト・Cloud Run・GCS のいずれにも存在しない**(`deploy-dashboard.sh:688-696` のゲートが機能している)。

---

## 2. 宣言との突合(deploy スクリプト ⇔ 実際の付与)

| # | 差分 | 内容 |
|---|---|---|
| D-1 | **宣言に無い付与** | `roles/editor`(compute SA・プロジェクト)。どの `deploy-*.sh` にも現れず、GCP の既定挙動で付いている。**リポジトリを読んでも存在に気づけない**のが最大の問題 |
| D-2 | **宣言に無い付与** | `roles/secretmanager.secretVersionAdder`(compute SA・`discord-bot-token`)。監査ログ上 2026-08-02T16:35:16Z に `gcloud secrets add-iam-policy-binding` で付与。`git log -S secretVersionAdder` は全 ref で 0 件 —— リポジトリに一度も存在したことがない手動付与である |
| D-3 | **宣言に無い付与** | `jquants-api-key` / `jquants-refresh-token` / `fred-api-key` / `estat-app-id` の accessor。実配線は正当だが `deploy-*.sh` が付与していないため、VM を作り直すと**再現しない**(デプロイの冪等性の穴) |
| D-4 | **宣言が過大** | `deploy-ops-weekly.sh` の `roles/run.invoker`(プロジェクト・`--condition=None`)。必要なのは Job `ops-weekly` に対する起動権のみ |
| D-5 | **宣言が過大** | `deploy-ops-weekly.sh` の `roles/bigquery.dataViewer`(プロジェクト)。必要なのは `billing_export` データセットの `tables.list` のみ |
| D-6 | **宣言が既に無効** | D-4・D-5 はいずれも `roles/editor` に包含され、現状では**1権限も増やしていない**。`editor` を剥がすまで、この2行は「効いていない宣言」である |

---

## 3. 過剰権限の指摘

### P-1(重大)既定 Compute SA の `roles/editor` — 3系統を兼務する単一 SA が実質フルアクセス

**事実**: `roles/editor` は 11,889 権限を含む(`gcloud iam roles describe roles/editor`)。うち本件で効くものは `compute.instances.setMetadata`(SSH 鍵注入)、`compute.instances.delete`、`iam.serviceAccounts.actAs`、`run.services.create/update/delete`、`run.jobs.run`、`run.routes.invoke`、`artifactregistry.repositories.uploadArtifacts`、`secretmanager.secrets.delete`、`secretmanager.versions.destroy`、`secretmanager.versions.add`。

**含まれないもの**(重要): `secretmanager.versions.access`。Secret の**中身の読み取り**は `editor` に含まれず、§1.3 の accessor 付与が担っている。ゆえに Secret の最小権限化そのものは無駄ではない。

**なぜ問題か**: この SA は VM `ryza-bot`(scope=`cloud-platform`)に紐付き、その VM で Discord Bot・日次サイクル・A-18 監査が同居する。Bot はネットワーク越しの入力(Discord メッセージ・LLM 出力)を扱うため攻撃面が最も広く、そこからの侵害が即座に「プロジェクト全体の editor」に化ける。加えて `versions.destroy` があるため、**Secret を読めなくても壊せる**(Bot・日次・ダッシュボードの同時停止)。そして `setMetadata` で VM に鍵を入れれば、その VM が読める全 Secret(Discord トークン・Anthropic キー・J-Quants・FRED・e-Stat・GitHub PAT)に到達するため、Secret 単位の絞り込みは同一 SA からの経路には効かない。

**リマインダー④の問い(共用の是非)への回答**: 分離すべきは「3系統」ではなく、まず `editor` である。SA を3つに割っても3つとも `editor` を継ぐなら爆風半径は変わらない。逆に `editor` さえ剥がせば、共用のままでも到達範囲は「7つの Secret + Job 起動 + billing_export 読み」に縮む。**順序は editor 剥離 → SA 分離**であり、逆順は効果がない。

### P-2(重大)`billing_export` データセットへの書き込み権 —— 会計証跡が消せる

**事実**: データセット ACL の `projectWriters` は `roles/editor` 保持者に解決される(`bq show ryza-main:billing_export`)。したがって compute SA は WRITER であり、`bigquery.tables.delete` 相当の操作ができる。

**なぜ問題か**: `billing_export` は運営帳簿(実費)の一次証憑になる想定であり(`ops/reminders.yaml` の `billing-export-verify`、Issue #7)、その証憑を**同じ SA が消せる**構成は不変原則3(証憑とリネージ)と噛み合わない。P-1 の是正で自動的に解消する(WRITER は `editor` 経由でしか来ていない)。

### P-3(中)プロジェクトレベル `roles/run.invoker` — ダッシュボードに継承される

**事実**: `ops/deploy-ops-weekly.sh:111-114` がプロジェクト全体に `--condition=None` で付与。Cloud Run のサービスレベルポリシーは IAP サービスエージェントのみだが、プロジェクトレベルの付与は**すべての Cloud Run 資源に継承**される。

**なぜ問題か**: IAM 上、compute SA は `ryza-dashboard` の invoker である。IAP を有効化した Cloud Run で `run.app` URL への直接呼び出しが IAP で終端されるか否かは、本棚卸しでは**未検証**である(認証情報を使った能動的な試行が必要で、読み取り専用の範囲を超える)。ゆえに「IAP を迂回できる」とは断定しない。断定できるのは「IAM の宣言が `deploy-dashboard.sh` の意図(閲覧者=代表1名)と一致していない」ことで、これ自体が是正理由として十分である。是正時には陽性テスト(compute SA の ID トークンで `ryza-dashboard` を叩き、拒否されること)を必ず添えること。

### P-4(中)`secretVersionAdder`(`discord-bot-token`)— 宣言なし・用途なし

**事実**: `grep -rn "add_secret_version|versions add" src/` は 0 件。Bot は Secret を**読むだけ**で、新版を積むコードは存在しない。

**なぜ問題か**: Bot トークンに新版を積めるということは、侵害時に「攻撃者の管理する Bot トークンへ差し替えて再起動を待つ」ことができるという意味で、乗っ取りの永続化経路になる。現状は `editor` に包含されて増分ゼロだが、**`editor` を剥がした後に残ると単独の穴になる**ため、剥離と同時に消すべきである。

### P-5(低)未使用資産の残滓

- **削除済み Cloud Run サービス `ryza-secret-drop`** のイメージが `cloud-run-source-deploy` リポジトリに 2 件(計 44 MB)。サービス自体は 2026-08-02T07:25:50Z に削除済み(監査ログ)で、IAM の残滓はない
- **`ryza/dashboard` イメージが 12 件**(タグ付き 11・無タグ 1、リポジトリ計 1.76 GB)。稼働中は `4bb5ba9490cef6a0c3c21a3ce46548dcca0b57cb` の 1 件のみ。`deploy-dashboard.sh` に世代整理も AR クリーンアップポリシーもない
- **`ryza/ops-weekly` の `latest` タグ**。Job の実体は `:20260802072633` を参照しており `latest` は使われていない(`deploy-ops-weekly.sh:75-78` が両方付けている)。`deploy-dashboard.sh` が SHA タグ固定にした理由(どのコードが動いているかを不変にする)と不整合

### P-6(低・IAM 外)default VPC の既定ファイアウォール

`default-allow-ssh`(0.0.0.0/0 → tcp:22)と `default-allow-rdp`(0.0.0.0/0 → tcp:3389)が有効。VM への SSH は IAM(OS Login / `compute.instances.setMetadata`)で守られているため直ちに侵入されるわけではないが、`ryza-bot` は外部 IP(136.118.169.10)を持ち、RDP は本プロジェクトで一切使わない。IAM の話ではないので本書では**指摘のみ**とし、是正は別枠とする。

---

## 4. 是正提案(優先度付き・実施は別タスク)

**実施順序に依存関係がある。** R-1 を先にやると ops-weekly と Scheduler が壊れる。必ず R-0 → R-1 の順で行う。

| 優先 | ID | 内容 | 前提 | リスク |
|---|---|---|---|---|
| 最優先 | **R-0** | `editor` 剥離の**前**に、必要な権限をリソースレベルで先に付ける:<br>①Job `ops-weekly` に `roles/run.invoker`(compute SA・**リソースレベル**)<br>②データセット `billing_export` に READER(compute SA・**データセット ACL**)<br>③`deploy-*.sh` から §1.3 の全 Secret accessor が付くことを確認(D-3 の 4 件を宣言に取り込む) | なし | なし(追加のみ) |
| 最優先 | **R-1** | プロジェクトレベルの `roles/editor` を compute SA から削除 | R-0 完了 | **高**。VM 上の bot/daily/a18、ops-weekly、Scheduler が同時に影響を受ける。実施は稼働の少ない時間帯に行い、直後に4系統(Bot 死活・`ryza-daily` 手動実行・`ops-weekly` 手動実行・`ryza-a18` 手動実行)の陽性確認を必須とする。**ロールバック手順を先に用意すること** |
| 高 | **R-2** | プロジェクトレベルの `roles/run.invoker` と `roles/bigquery.dataViewer` を削除し、`deploy-ops-weekly.sh:90-93,111-114` をリソースレベル付与に書き換える | R-0・R-1 | 中。スクリプトは保護領域 `deploy_path` のため独立役員審査+承認記録が必要 |
| 高 | **R-3** | `discord-bot-token` の `secretVersionAdder` を削除 | R-1(順序はどちらでもよいが R-1 前だと効果ゼロ) | 低。用途が無いことをコード検索で確認済み |
| 中 | **R-4** | D-3 の 4 Secret(`jquants-api-key` / `jquants-refresh-token` / `fred-api-key` / `estat-app-id`)の accessor 付与を `deploy-daily.sh` に宣言として書く | なし | 低(冪等な追加)。保護領域のため審査が要る |
| 中 | **R-5** | SA 分離の**判断**(§5 に材料)。分離するなら `ryza-bot`(VM)と `ryza-ops-weekly`(Job+Scheduler)の2つを新設 | R-1 完了後に再評価 | 中。VM の SA 変更は**インスタンス停止が必要** |
| 低 | **R-6** | AR クリーンアップポリシー(`ryza` リポジトリ・タグ付き最新 N 世代を保持、無タグは 7 日で削除)。`ryza-secret-drop` イメージと `cloud-run-source-deploy` リポジトリの削除 | なし | 低。ただしロールバック用の旧イメージを消しすぎないこと(N≥5 を推奨) |
| 低 | **R-7** | `deploy-ops-weekly.sh` の `latest` タグ付与を廃止し SHA タグ固定に揃える(`deploy-dashboard.sh` と同じ方針) | なし | 低 |
| 別枠 | **R-8** | `default-allow-rdp` の削除、`default-allow-ssh` の送信元を IAP TCP forwarding 範囲(35.235.240.0/20)へ限定 | なし | 中。SSH を絞ると `gcloud compute ssh` の経路が変わる(IAP トンネル必須になる) |

### 自動化してよいもの / してはいけないもの

**自動化してよい**(冪等・追加のみ・失敗しても縮退する):R-0 ①②③、R-4、R-6、R-7。いずれも `deploy-*.sh` に書けば冪等に収束する。

**自動化してはいけない**(削除を伴い、失敗が沈黙する):**R-1・R-2・R-3**。IAM の削除はスクリプト化すると「消したつもりで消えていない」「消しすぎて夜間ジョブが翌週まで止まる」のどちらも静かに起きる。`gcloud projects remove-iam-policy-binding` は etag 競合時に黙って失敗しうるため、**代表の確認を挟む手動実施+実施後の陽性確認**とすること。これが `gcp-iam-remediation` に「代表確認付き」を付けた理由である。

---

## 5. SA 分離のコスト見積り(R-5 の判断材料 — 結論は出さない)

| 項目 | 分離しない(現状維持) | 分離する(`ryza-bot` / `ryza-ops-weekly` を新設) |
|---|---|---|
| 実装コスト | 0 | SA 2 個作成 + 各 Secret の accessor 付け替え(7 Secret)+ `deploy-bot.sh` / `deploy-daily.sh` / `deploy-a18.sh` / `deploy-ops-weekly.sh` の 4 本改訂 |
| ダウンタイム | 0 | **VM の SA 変更はインスタンス停止が必要**(`gcloud compute instances set-service-account` は TERMINATED 状態でのみ可)。Bot 停止 5〜10 分 + PostgreSQL 再起動を伴う |
| 統制手続 | 0 | 4 本すべて保護領域 `deploy_path` → 独立役員審査 + 承認記録 ×1(まとめて1 PR なら1回) |
| 得られるもの(R-1 実施済みを前提) | 侵害時の到達範囲 = 7 Secret + Job 起動 + billing_export 読み | 侵害時の到達範囲 = Bot 侵害なら 6 Secret、ops-weekly 侵害なら `github-token` のみ |
| 得られるもの(R-1 未実施なら) | — | **ほぼゼロ**(3 SA とも editor を継ぐなら同じ) |

差分の実体は「Bot 侵害時に GitHub PAT が漏れるか否か」に集約される。GitHub PAT は fine-grained で、A-18 監査の clone と ops-weekly の Issue 操作に使われるため、漏れると**監査対象リポジトリを書き換えられる**(定款第5条の統制が直撃する)。この一点をどう重く見るかが判断の分かれ目で、軽く見るなら R-1 だけで打ち切ってよい。

---

## 6. 本棚卸しの限界(明示)

1. **到達可能性は検証していない**。IAM ポリシーの読みは「設定がそうなっている」ことしか示さない。P-3(IAP 迂回の可否)は能動的な試行が必要なため未検証で、是正時の陽性テストに委ねる
2. **`roles/editor` の 11,889 権限を全数評価していない**。§3 P-1 は本件で効くものを抜粋したにすぎず、「他に危険な権限が無い」ことは示していない
3. **VM 内部の権限(PostgreSQL ロール・OS ユーザー)は対象外**。DB ロールの棚卸しは `deploy-dashboard.sh` のゲート(§4.1〜4.9)が別に担っている
4. **監査ログの保持期間は 400 日**(Admin Activity)だが、本書の遡及は 90 日以内のクエリに留めた。プロジェクト作成が 2026-08-02 のため実質全期間をカバーしている

## 7. 再現手順(すべて読み取り専用)

```bash
gcloud projects get-iam-policy ryza-main --format=json
gcloud iam service-accounts list --project ryza-main --format=json
gcloud secrets list --project ryza-main
gcloud secrets get-iam-policy <SECRET> --project ryza-main --format=json   # 全 Secret に対して
gcloud run services get-iam-policy ryza-dashboard --region us-west1 --project ryza-main
gcloud run jobs get-iam-policy ops-weekly --region us-west1 --project ryza-main
gcloud beta iap web get-iam-policy --project ryza-main --resource-type=cloud-run \
  --service=ryza-dashboard --region=us-west1
bq show --format=prettyjson ryza-main:billing_export
gcloud artifacts docker images list us-west1-docker.pkg.dev/ryza-main/ryza --include-tags
gcloud compute firewall-rules list --project ryza-main
gcloud logging read 'protoPayload.serviceName="secretmanager.googleapis.com"' \
  --project ryza-main --freshness 90d --order=asc
```
