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

## 独立役員 追補審査 — 0032/0033・icon-hosting-legal(2026-08-04, origin/main..de485d0)

- 対象: migrations/0032・0033(保護領域 schema)、src/ryza/ops/icon_revalidate.py、bot/main.py 18:00 配線(kill_switch 隣接)、docs/research/icon-hosting-legal.md。審査者: 独立役員(非執行・起草者の選好は不知)。
- **C-11(重大・マージ前必須)**: 0032 の NOT VALID CHECK は既存行への **UPDATE にも効く**。0032 以前の未送行(embed 内に member_id)は送信成功**後**の mark_sent(outbox.py:119)が CheckViolation で失敗し、バッチ全体 rollback → 5秒ごとに Discord へ再送(承認・KS 通報含む)— 「高々1回」の冪等保証の破壊+配送の恒久詰まり。tests/bot/test_outbox.py の legacy テストは制約を DROP したまま検証し mark_sent を呼ばないため検出不能。是正: 0032 内で**未送行のみ** strip+列 backfill(送済行=証跡は不改変のまま。未送行は未配送のキュー状態であり証跡論は及ばない)。
- **C-12(中)**: 障害中のすり替えが「復旧」に化ける。changed は `not last_error` を要求し(icon_revalidate.py:247)、_record_error が「復旧時の比較基準に要る」と保存した指紋を復旧時に**比較していない**。1日 404 を返してから差し替えれば通知は通常色の cleared のみ。是正: 復旧時も保存指紋と比較し changed を併発。
- **C-13(中・敵対的観点)**: 「URL 変更=代表の意図」を検証せず無通知で再基準化する。ops.org_icon_overrides に書ける主体(侵害されたジョブ等)は URL 差し替えで検知を素通りでき、A→B→A の往復でも changed は出ない。基準表 org_icon_checks は設計上 UPDATE 可のため指紋の先回り改変にも検知は無い(events 表は検知記録を守るが検知自体は守らない)。是正: 再基準化時に override_log/台帳履歴と突合、または情報レベルで通知(頻度は低く騒音にならない)。
- **C-14(中)**: 握った失敗の可視化が systemd ログのみ。例外時は rollback で meta.runs 行ごと消え、「静寂=変化なし」と「静寂=不実行」を区別できるという自らの設計主張(docstring)が失敗時にこそ不成立。daily の失敗は再検証を巻き添えにする(分離は片方向のみ)。是正: 失敗 Run の別トランザクション記録、または週次監査へ実行有無の検査を追加。
- **C-15(低・法的整理)**: 結論(再ホストせず検知)は保守側で支持する。ただし §1「必ず誰でも取得できる URL を伴う」は本文 (c) の留保(Discord CDN の公衆性は §7 で未検証と自認 — 2023 年以降は署名付き・期限付き URL)と整合せず構造論として過剰主張。また現行ホットリンクでも Discord の embed プロキシが画像を取得・再配信する点で (a) と (c) の距離は記述より近く、投稿者の利用主体性(ロクラクⅡ系)が未検討 — これは (a) を無条件に安全とする記述を弱める方向に効く。30条1項4号の二次創作(28条の権利)除外の射程も §7 へ追記されたい。いずれも結論は変えない。
- **C-16(低)**: error の重複抑止が detail 文字列の完全一致依存で、例外文言が揺れる配信元では毎日イベント化し形骸化する。型名のみへの正規化を検討。妥当と認めた点: 配線順(DB のみの日報→HTTP・to_thread・失敗の握り)は KS・配送経路を阻害しない、遷移のみ通知+meta.runs 件数の設計、0032 の全書込経路の網羅(freshness=author なしで CHECK 素通り、a18/devchat/notices は enqueue 経由)を実査で確認。
- 判定: **条件付き承認** — C-11 是正がマージ前必須。C-12〜C-14 は同 PR 内是正が小さく望ましい。C-15 は文書追記で足りる。
