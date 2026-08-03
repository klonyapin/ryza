# T-013: 実 LLM プロバイダ+日次サイクル常駐化(玲音初投稿の最終ピース)

- 発行日: 2026-08-03 / 依存: T-007〜T-011 完了(済み)
- 必読: docs/design/30-press-discord.md §2・§5、docs/design/00-system-design.md §2(LLM境界)・§10(GCP構成)、src/ryza/research/llm.py(プロバイダ・プロトコル)、ops/deploy-bot.sh(VM 構成)、CLAUDE.md

## 背景と構成判断

- コードは揃ったが実 LLM 接続と定時実行が未配線。テストは引き続き全てモック(実 API はテスト対象外)
- **日次ジョブは Cloud Run でなく GCE VM(ryza-bot)上の systemd timer で動かす**。理由: DB が VM 内 PostgreSQL(localhost)にあり、Cloud Run からは届かない。設計 30 §1 の「Cloud Run Jobs」は Cloud SQL 移行後の姿とし、乖離をコードコメントに明記

## 実装

```
src/ryza/research/providers.py  -- AnthropicProvider(llm.py の LLMProvider 実装)。Secret Manager
                                   'anthropic-api-key' から遅延ロード(T-006 bot の stdlib REST 方式を踏襲)。
                                   モデルは config/press.yaml / config/llm.yaml で階層別に指定
                                   (執筆=中位: claude-sonnet-5 / トリアージ=軽量: claude-haiku-4-5-20251001)。
                                   リトライ・タイムアウト・コスト記録(Run.add_cost)は llm.py の既存機構を使用
src/ryza/jobs/daily.py          -- 日次サイクル: 取込(J-Quants/TDnet/EDINET/RSS/FRED/カレンダー)
                                   → 前処理 → 分析エージェント → 市場観更新 → 朝刊生成(09:40)→ outbox。
                                   各段は独立に失敗許容(前段失敗でも後段は前日データで走り「暫定」明記)。
                                   Run 記録+#運営 へ実行サマリ
config/llm.yaml                 -- 階層→モデル ID・単価表(経営管理部が実測更新する前提のコメント付き)
ops/deploy-daily.sh             -- VM に systemd timer(ryza-daily.timer: JST 09:00 起動、朝刊は 09:40 まで
                                   に outbox 投入)+ service を冪等設置。deploy-bot.sh と同じ流儀
tests/                          -- providers はリクエスト組立・レスポンス解釈・エラー処理を HTTP モックで。
                                   daily はステップ実行順・失敗許容・冪等(同日再実行で二重投稿しない)
```

## 制約・注意

- **VM は e2-micro(RAM 1GB)**: `[preprocess]`(torch)は**インストールしない**。前処理は埋め込みなしの縮退モード(content_hash 重複排除のみ・準重複スキップ)で動くこと — preprocess/runner に縮退パスが無ければ最小の分岐を追加してよい(src/ryza/preprocess/ の変更はその一点に限る)。埋め込みバックフィルは別途ローカルで行う設計とし、コメントに明記
- 同日再実行の冪等性: 朝刊は「その日の press.outbox に既に morning 投稿があればスキップ」
- 実 API のスモークは Secret 'anthropic-api-key' 登録後(Issue #26)に**1回だけ**手動実行する想定。コードにはドライラン(--dry-run: LLM 呼び出しをフィクスチャに差し替え)を必ず付ける
- 接触禁止: tests/ingest/・src/ryza/ingest/ の変更(読み・import 利用は可)、src/ryza/bot/、migrations/0001〜0011、保護領域
- Kill Switch フラグ(ops.kill_switch)が立っている場合、daily は取込・分析のみ行い投稿はスキップ(参照だけ。フラグ操作は bot の領分)

## 受け入れ基準

- [ ] AnthropicProvider: 構造化出力の往復・リトライ・コスト記録が HTTP モックで検証される
- [ ] daily --dry-run がローカル DB でエンドツーエンド完走(取込はモック可)し、同日2回目で朝刊が二重投稿されない
- [ ] deploy-daily.sh が冪等(2回実行可)で、timer が JST 09:00 に設定される
- [ ] `uv run pytest` 全通過(既存 303 を壊さない)・ruff パス
- 完了コミット: `feat(jobs): 実 LLM プロバイダと日次サイクル (T-013)`+Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
