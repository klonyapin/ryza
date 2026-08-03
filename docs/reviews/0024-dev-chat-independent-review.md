# 0024 開発室(dev_chat)独立役員審査 — 2026-08-03

- 対象: migrations/0024_dev_chat.sql(保護領域 schema)、src/ryza/governance/devchat.py、src/ryza/bot/main.py(保護領域 kill_switch)、dashboard/app.py、ops/deploy-dashboard.sh
- 判定: **条件付き承認**(重大-1・重大-2 の是正を merge 前提)
- 重大-1(deploy-dashboard.sh:295): boardroom への INSERT が表レベルのため sender / created_at / relayed_at を任意指定でき、設計リード発言の捏造・投稿時刻の遡及・「中継されないのに中継済み」の生成が可能。ガードは BEFORE UPDATE のみで INSERT に発火しない。→ `GRANT SELECT, INSERT (sender, body)` へ。検証クエリは role_column_grants も見ること
- 重大-2(bot/main.py:279): 非緊急の中継を Kill Switch 通報を含む配送より前に置き、timeout 無しの connect() を毎ティック 2 本に倍増させた。→ 中継を _deliver_sync の後へ(遅延 5 秒、失うもの無し)
- 中-3(0024:76-83): relayed_at の値域が無検査。過去/未来時刻を設定でき created_at より前の中継時刻が残る。→ 範囲 CHECK をガードに追加
- 中-4(devchat.py:252-261): --list 出力に行頭フェンスが無く、本文に改行込みの偽ヘッダを仕込めば設計リードのセッションに存在しない会話ターンを注入できる。→ 本文を一律インデントし「入力データであり指示ではない」旨を明示
- 中-5(devchat.py:103): 片道中継のため設計リードの返信が Discord に出ず、外出中は Discord をミラーとする運用と矛盾する。→ design_lead も中継、または画面に明記
- 中-6(devchat.py:194): 長文を切り捨て、誘導先が CLI から到達不能な IAP 配下 UI。→ bridge_send.split_chunks 再利用 / 誘導を --list へ
- 中-7(bot/main.py:308): 中継全滅でも Run は success、UI は「中継待ち」のまま。障害が沈黙する。→ 部分失敗を Run に反映し、UI に滞留警告
- 軽: ガードトリガの存在/tgenabled が test_migrations 未登録(所有者は DISABLE TRIGGER 1 行で無音化でき、テストフィクスチャ自身が実演)/fragment に例外処理なく cache_resource の死んだ接続から復帰しない/design_lead 行の relayed_at 無制約/ops/deploy-dashboard.sh が protected_areas 未登録(A-13-1 が効かない — 別 PR で登録)/Approved トレーラ未付与
- 崩せなかった点: 中継の原子性(claim の FOR UPDATE SKIP LOCKED を外側 tx で保持し、enqueue と mark_relayed を SAVEPOINT で束ねる)。二重中継・中継漏れ・片方残りの経路は見つからず、実 DB テストが正しく押さえている

## 設計リード裁定(2026-08-03 追記)

- 本 PR 内で是正(未マージのため全指摘を前倒し): 重大-1(列レベル INSERT+role_column_grants 検証+`inserted_by text NOT NULL DEFAULT current_user` 列で sender との矛盾を監査可能に)、重大-2(中継を配送の後へ)、中-3(relayed_at の範囲検査)、中-4(--list の本文インデント+データ宣言)、中-5(**design_lead も中継する** — relayed_at の意味を「Discord へ載せたか」に統一)、中-6(split_chunks 再利用)、中-7(部分失敗の Run 反映+UI 滞留警告)、軽-8(test_migrations にトリガ存在+tgenabled 検査。migration コメントの「トリガで強制」の過大主張も訂正)、軽-9(fragment の try/except+接続再生成)、軽-10(CHECK 追加)。
- 軽-11 は PR #66(保護領域拡充)で対応済み。軽-12 はマージコミットにトレーラ付与(先例どおり)。
