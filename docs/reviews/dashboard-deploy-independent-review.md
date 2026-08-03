# 独立役員意見書 — ops/deploy-dashboard.sh ほかダッシュボード公開一式の審査

- 日付: 2026-08-03
- 審査者: 独立役員(personas/independent-officer/charter.md v1.0 に基づく盲検レビュー)
- 対象: ops/deploy-dashboard.sh(保護領域 deploy_path・新規)、dashboard/{Dockerfile,app.py,queries.py,github_api.py}、config/roadmap.yaml
- 対象 worktree: .claude/worktrees/agent-ae52aea8765e41178
- 判定: **差し戻し**(重大-1〜3 の是正後に再審査)

## 要旨

デプロイスクリプトは冪等性・秘密管理(Secret Manager 経由の env 注入)・非公開デプロイ
(--no-allow-unauthenticated)・コスト上限(max-instances 1)など基本設計は良い。しかし
保護領域 deploy_path として最も守るべき「稼働コード=承認済み main の一致」(定款第5条)を
執行する検証が一切なく、DB ロールは read-only 想定に反して書込全権を継承し、過去の
無認証公開版の allUsers バインディング残存という具体的な公開経路を検査しない。以上3点は
単体で統制目的を無効化するため差し戻しとする。

## 重大(差し戻し事由)

### 重大-1 承認済みコード一致の執行点ゼロ
- 根拠: deploy-dashboard.sh L45, L47, L143–151。ローカル作業ツリーをそのまま
  Cloud Build に送信し、git 状態(ブランチ・クリーン・SHA)を一切検証しない。
  タグは :latest でコミット SHA の記録もない(不変原則3違反)。
- 攻撃シナリオ: 侵害された開発エージェントが worktree 上で app.py に改竄
  (例: 役員室の書込接続で governance.stances を書き換える処理)を仕込み、PR も
  承認記録も経ず本スクリプト一発で本番化。git 履歴に痕跡が残らず A-13 監査を素通り。
- 是正: (a) `git status --porcelain` 空 && HEAD==origin/main を検証、不一致で中断。
  (b) タグをコミット SHA に。(c) Cloud Run にも code_version を記録。
  恒久策は GitHub main 連携の Cloud Build トリガー化。

### 重大-2 ryza_dashboard ロールの権限過剰(read-only 想定と矛盾)
- 根拠: deploy-dashboard.sh L103(IN ROLE ryza・INHERIT= 全権限継承。L14–16 の
  コメント自身が明記)。read-only の実体は queries.py L42 のセッション設定のみで
  クライアント側・解除可能(L39–43 の「DB 側で拒否」は不正確)。app.py L749–760 の
  役員室接続は全面書込可。
- 攻撃シナリオ: ダッシュボード侵害(依存 RCE・重大-1 経由の改竄)が帳簿仕訳の改竄、
  ops.trading_state の書換(Kill Switch 状態偽装)、監査対象レコードの削除に直結。
- 是正: IN ROLE ryza を廃し、SELECT 全般+governance.minutes / minute_resolutions /
  stances への INSERT+meta.runs の INSERT/UPDATE のみを明示 GRANT。
  `ALTER ROLE ... SET default_transaction_read_only = on` を既定に。

### 重大-3 旧・無認証公開版の allUsers invoker 残存を未検査
- 根拠: app.py L4–6 が 2026-08-02 の無認証 Cloud Run 公開版の存在を明言。
  deploy-dashboard.sh L155–166 は既存 IAM を保持したまま更新し、L168–182 も追加のみ。
- 失敗シナリオ: 同名サービスに allUsers の run.invoker が残存していれば、IAP 有効化後も
  直接 URL で全世界公開のまま。「完了」メッセージ(L185–186)がそれを隠す。
- 是正: デプロイ後に get-iam-policy で allUsers / allAuthenticatedUsers を検査し、
  存在すれば除去または中断するステップを必須追加。

## 中(再審査までに是正を強く推奨)

- 中-4 実行 SA が既定 compute SA(L131–136)。専用最小権限 SA を作成し
  `--service-account` 指定に。侵害時の横展開を限定する。
- 中-5 役員室の「操作者=代表」根拠コメント(app.py L882–884)が公開後の実態と自己矛盾。
  DASHBOARD_USER の env 上書き(deploy L38)+IAP 追加専用バインディング(L178–182)で
  承認痕跡なしにアクセスリストが増え、増えた人物は決議を代表名義でマークできる。
  IAP ポリシーの宣言的管理(1名へ収束)とコメント修正を求める。
- 中-6 DB パスワードが VM の psql argv と PostgreSQL ログ(log_statement 設定次第)に
  平文露出(L104)。SCRAM 検証子のクライアント側生成などへ変更を。
- 中-7 pg_hba 末尾追記(L106–108)は先勝ち規則のため既存の広い行があれば無効。
  FW(L112–118)はサブネット全体に 5432 を開ける。既存行の検査と source の限定を。

## 低(記録)

- 低-8 本番 PostgreSQL の restart(L92–95)。「bot/daily は再接続で復帰」は未実証の
  仮定 — テストで実証を(議論規約4)。
- 低-9 public repo の Issue タイトル(第三者作成可)が markdown としてダッシュボードに
  描画される(github_api.py L34–119 → app.py L624–629)。リンク偽装フィッシングの面。
  XSS には至らない(unsafe_allow_html 不使用)。タイトルのリテラル化を推奨。
- 低-10 ベースイメージのダイジェスト未固定(Dockerfile L10, L13)/
  `gcloud config set project` の操作者環境への副作用(L52)/
  config/roadmap.yaml に本件(ダッシュボード公開)のマイルストーン未記載。

## 確認済み(懸念なしと判断した項目)

- コスト暴走: min 0 / max 1(L159)で計算資源は有界。GitHub API は 60 秒キャッシュ。
  役員室 LLM は IAP 単一ユーザー内でコスト記録あり。反対すべき点を探して重大なものは
  見つからなかった(議論規約1に基づく明示)。
- Secret の扱い: DB URL は Secret Manager 参照で env 注入(L165)。ログ出力に
  パスワードは含まれない(L184–188)。
- 非公開デプロイ: --no-allow-unauthenticated を一貫使用(L158)— ただし重大-3 の
  既存バインディング残存は別問題。

## 反対意見書様式(この差し戻し判定が間違っている場合の理由トップ3+代替案)

1. 単一運用者・単一 gcloud アカウントの現段階では、ローカルビルドの統制不足は
   実害より運用速度の損失が大きい可能性 → 代替案: git 検証だけ先行導入し
   トリガー化は Phase 5 に繰延。
2. ryza_dashboard の GRANT 明示列挙はスキーマ進化のたびに保守コストを生む
   → 代替案: 読取専用ロール+役員室専用の第2ロール(書込先3テーブルのみ)の
   2ロール構成で保守点を最小化。
3. allUsers 残存は「旧版が別サービス名だった」なら杞憂 → 代替案: 是正コードを
   入れずとも、再審査時に get-iam-policy の実出力を証跡として添付すれば足りる。

## 設計リード裁定(2026-08-03 追記)

- 重大-1: 是正 (a)(b)(c) を採用。Cloud Build トリガー化は Phase 5 に繰延(反対意見書1の
  代替案を採用)し、ops/reminders.yaml に登録。
- 重大-2: 反対意見書2の代替案(2ロール構成)を採用 — ryza_dashboard は読取専用
  (default_transaction_read_only=on)、役員室書込は専用第2ロール ryza_boardroom
  (governance 3テーブル INSERT+meta.runs のみ)。保守点最小化のため。
- 重大-3・中-4〜7・低-9・低-10: 是正を実装。
- 低-8: restart は設定変更があった初回のみ必要 — pg_hba/conf 変更時のみ reload/restart
  する条件分岐とし、09:00 JST 帯を避ける注記を追加。
