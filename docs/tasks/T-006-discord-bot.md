# T-006: Ryza Discord Bot 基盤(GCE 常駐)

- 発行日: 2026-08-03 / 発行者: 設計リード(Fable)/ 依存: T-001(完了済)
- 必読: docs/design/30-press-discord.md §1, §5, §7(仕様の正)、CLAUDE.md

## ゴール

GCE e2-micro 上に常駐する Ryza 本体の Discord Bot を実装・デプロイする。範囲は 30-press-discord.md の T-006 行: outbox 配送・承認 UI・Kill Switch・日報骨格・press スキーマ。朝刊・速報の生成は対象外(T-007/T-008)。

## 実装

```
migrations/0007_press.sql        -- press.outbox / press.predictions / governance.decisions(最小形)/ ops.flags(kill switch)
src/ryza/bot/__init__.py
src/ryza/bot/main.py             -- discord.py 2.6 常駐(トークンは Secret Manager から)
src/ryza/bot/outbox.py           -- outbox ポーリング配送(5秒間隔、sent_at で冪等)
src/ryza/bot/approvals.py        -- 承認 embed+ボタン(承認/却下/質問)→ governance.decisions 記録。オーナーID検証
src/ryza/bot/killswitch.py       -- /kill /resume(2段階確認)→ ops.flags 更新
src/ryza/bot/daily.py            -- 18:00 JST 日報骨格(当面は稼働状況のみ)
ops/deploy-bot.sh                -- GCE VM 作成(e2-micro us-west1・無料枠)+ systemd 設置 + Secret 取得(冪等)
tests/bot/                       -- outbox 冪等・承認記録・killswitch 状態遷移(discord API はモック)
```

- オーナー ID・カテゴリ ID は環境変数(deploy 時に指定。カテゴリは RYZA_DISCORD_CATEGORY_ID=1533512287816782017)。ハードコードしない
- **チャンネルは4つ(報道/承認/運営/dev)を指定カテゴリ配下に Bot が起動時 ensure(存在確認・自動作成)**し、name→id を DB に記録。outbox.channel は press|approval|ops|dev(設計書 §7 改訂済み)
- VM 上の DB 接続: 当面 VM 内 PostgreSQL(将来 Cloud SQL 移行を想定し RYZA_DATABASE_URL で切替可能に)。**注意: 本タスクでは VM に PostgreSQL も同居設置し、migrations を適用する**(00-system-design §10 の構成)
- Bot 停止検知: systemd Restart=always + 起動時に `#経営` へ再起動通知

## git 規約

パス指定 add のみ / wip コミット30分ごと / 15分詰まったら questions ファイルで停止 / push しない。完了コミット: `feat(bot): Ryza Discord Bot 基盤 (T-006)` + Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

## 受け入れ基準

- [ ] ローカル(モック)テスト: outbox 二重送信なし / 承認ボタン→decisions 記録 / kill→flags 反映 / 非オーナーの操作拒否
- [ ] `uv run pytest` 全通過・ruff パス
- [ ] deploy スクリプトが冪等(VM 既存時はコード更新+再起動のみ)
- [ ] 実デプロイと疎通(テストメッセージ送信)は設計リードが実施するため、スクリプトと手順を README コメントに明記
