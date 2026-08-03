# 独立役員意見書 — 0020 キャラクターアイコンの上書き(ee5bac9..09d8ee4)

- 日付: 2026-08-03 / 対象: migrations/0020_org_icon_overrides.sql(保護領域 schema)、ops/deploy-dashboard.sh の GRANT(deploy_path 相当)、src/ryza/org.py、src/ryza/bot/main.py、dashboard/app.py(8 files, +720/-23)
- 審査者: 独立役員(非執行・批判専任。起草者の選好は不知)
- 根拠: 定款第5条、config/governance.yaml(protected_areas)、migrations/0013・0017・0020、src/ryza/bot/outbox.py、dashboard/queries.py、src/ryza/audit/a13.py
- 検証: ops スキーマに関数が無く `USAGE ON SCHEMA ops` 追加で表権限が増えないこと、BR ロールが trading_state/flags/discord_webhooks に到達しないこと、履歴表が INSERT のみであること、内部キー member_id が Discord へ出ない全経路(resolve_author・payload_for・dict_to_embed・bridge_send)を確認

## 判定: 条件付き承認

- **C-1(重大・マージ前必須)**: 0020:26-28 が方式 B の担保と謳う「現在値とログを同一トランザクションで書く」が本番経路で不成立。connect_boardroom は autocommit=True(queries.py:74)で org.py:274-304/327-345 に transaction ブロックが無い。テストは autocommit=False 接続(tests/ops/…:23)のため検出できない。是正: `with conn.transaction():` + autocommit 前提のテスト。
- **C-2(重大・マージ前必須)**: main.py:303 の icon_overrides 例外が outbox.py:124-127 の無言 continue に落ち、0020 未適用や一時的 DB エラーで**全 Discord 配送が無言停止**(速報・Kill Switch 通報を含む)。是正: {} へフェイルオープン + log.warning。
- **C-3(中)**: 履歴表の追記オンリーが GRANT のみ。0013:59-82 の forbid_mutation トリガ + REVOKE FROM PUBLIC の先例に不一致で、owner ロールが履歴を消せる。
- **C-4(中)**: ops/deploy-dashboard.sh は protected_areas 未登録(governance.yaml:113-118 は bot/daily/ops-weekly のみ)。A-13-1 が変更を検出しない。本 PR に `Approved:` トレーラも無い(migrations は schema 領域)。
- **C-5(中)**: 証跡セクション(:297-305)は dash ロールのみ検査。to_regclass ガードで GRANT が黙ってスキップされうる。BR ロールの ops 権限が期待2表のみであることのアサーションを追加。
- **C-6/C-7(中)**: check_icon_url はリダイレクト自動追従・ホスト制限なし(SSRF)。ホットリンクすり替えの起草者懸念は妥当だが重大度は**中** — Discord はサーバ側取得のため閲覧者 IP は漏れず、被害は代表 IP の追跡と非公開サーバーへの意図しない画像に限る。検証は UA 識別可能な単発 HEAD で誠実な誤りしか防げない。恒久是正は保存時の再ホスト。
- **C-8〜C-10(低)**: image/svg+xml とサイズ上限なし / 保存ごとの未 close 接続(app.py:521。既存 _boardroom_conn を使うべき)/ 内部キーが全 outbox 行に永続化され除去が送信直前の1関数に依存。
- 妥当と認めた点: 台帳優先の層構造、未知 id の無視、検証と書込の結線、HTML エスケープ、action/icon_url 相関 CHECK、dash ロール除外リストに追加しない判断。

## 設計リード裁定(2026-08-03 追記)

- 今回の PR 内で是正: C-1・C-2(必須)、C-3(0013 同型トリガ+REVOKE)、C-5(BR ロールのデプロイ時アサーション)、
  C-9(既存 _boardroom_conn の再利用)、C-6 暫定(リダイレクト無効化+解決先 private/link-local IP の拒否)、
  C-8(MIME を png/jpeg/gif/webp に限定+Content-Length 上限 5MB)。
- リマインダー登録して後続: C-4(protected_areas への deploy-dashboard.sh 登録 — governance.yaml 側の別 PR)、
  C-7 恒久(保存時の再ホスト化 — 再配布の法的懸念の整理込み)、C-10(内部キーの構造分離)。
- 反対意見書1(投入時解決+TTL キャッシュ)は不採用: C-2 のフェイルオープン化で可用性懸念は解消され、
  配送時解決の「滞留中の投稿も新アイコンになる」利点を保つ方を選ぶ。
