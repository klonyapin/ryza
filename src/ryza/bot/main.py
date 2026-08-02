"""Ryza Discord Bot 常駐エントリポイント(discord.py 2.6 / GCE 常駐)。

systemd(Restart=always)で常駐し:
- 5秒間隔で ``press.outbox`` を配送(``outbox.deliver_pending``)
- 18:00 JST に日報を投入(``daily.enqueue_daily``)
- ``#承認`` のボタン押下を ``governance.decisions`` に記録(オーナー検証)
- ``/kill`` ``/resume``(2段階)で Kill Switch を操作
- 起動時に4チャンネル(報道/承認/運営/dev)を指定カテゴリ配下へ ensure し、``#運営`` へ再起動通知

**このモジュールは discord.py に依存する唯一の層**
(純ロジックは outbox/approvals/killswitch/daily/channels)。
テストはこのモジュールを import せず、純ロジックをライブ DB で検証する。

トークンは Secret Manager から起動時ロード(env ``RYZA_DISCORD_TOKEN`` があれば優先=ローカル検証用)。
オーナー ID・カテゴリ ID は環境変数で与える(ハードコードしない)。実チャンネル ID は起動時に
カテゴリ配下を ensure して ``ops.discord_channels`` に記録し、配送時はこの表を引く。

必要な環境変数(deploy 時に指定):
  RYZA_DISCORD_TOKEN          直接指定(未指定なら Secret Manager から取得)
  RYZA_DISCORD_TOKEN_SECRET   Secret 名(既定 'discord-bot-token')
  GCP_PROJECT                 Secret Manager のプロジェクト
  RYZA_OWNER_IDS              オーナーの Discord ユーザー ID(カンマ区切り)
  RYZA_GUILD_ID               スラッシュコマンド即時同期先のギルド ID(任意)
  RYZA_DISCORD_CATEGORY_ID    4チャンネルを配置するカテゴリ ID(必須)
  RYZA_DATABASE_URL           DB 接続(既定はローカル)
"""

from __future__ import annotations

import datetime as dt
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ryza.bot import COLOR_FLASH, COLOR_NORMAL, channels, killswitch, outbox
from ryza.bot import daily as daily_mod
from ryza.bot.approvals import KINDS, NotOwnerError, record_decision
from ryza.bot.daily import JST
from ryza.db.conn import connect
from ryza.provenance import start_run

log = logging.getLogger("ryza.bot")

POLL_SECONDS = 5.0
DAILY_TIME = dt.time(hour=18, minute=0, tzinfo=JST)  # 18:00 JST


# ────────────────────────────────────────────────────────────────────────────
# 設定(環境変数)
# ────────────────────────────────────────────────────────────────────────────
def owner_ids() -> list[str]:
    raw = os.environ.get("RYZA_OWNER_IDS", "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def category_id() -> int:
    """4チャンネルを配置するカテゴリ ID(必須)。"""
    raw = os.environ.get("RYZA_DISCORD_CATEGORY_ID", "")
    if not raw.strip().isdigit():
        raise SystemExit("RYZA_DISCORD_CATEGORY_ID(数値)が未設定です")
    return int(raw.strip())


def load_token() -> str:
    """Bot トークンを取得。env 優先、なければ Secret Manager から。"""
    tok = os.environ.get("RYZA_DISCORD_TOKEN")
    if tok:
        return tok
    secret = os.environ.get("RYZA_DISCORD_TOKEN_SECRET", "discord-bot-token")
    project = os.environ.get("GCP_PROJECT", "")
    if not project:
        raise SystemExit("RYZA_DISCORD_TOKEN も GCP_PROJECT も未設定です")
    from google.cloud import secretmanager  # 遅延インポート(ローカルでは不要)

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project}/secrets/{secret}/versions/latest"
    return client.access_secret_version(name={"name": name}).payload.data.decode("utf-8")


# ────────────────────────────────────────────────────────────────────────────
# embed 変換
# ────────────────────────────────────────────────────────────────────────────
def dict_to_embed(data: dict) -> discord.Embed:
    """outbox の embed_json(dict)を discord.Embed に変換する。"""
    embed = discord.Embed(
        title=data.get("title"),
        description=data.get("description"),
        color=data.get("color", COLOR_NORMAL),
    )
    for f in data.get("fields", []):
        embed.add_field(
            name=f.get("name", ""), value=f.get("value", ""), inline=f.get("inline", False)
        )
    footer = data.get("footer")
    if footer:
        embed.set_footer(text=footer.get("text", ""))
    return embed


# ────────────────────────────────────────────────────────────────────────────
# 承認 View(ボタン)
# ────────────────────────────────────────────────────────────────────────────
class ApprovalView(discord.ui.View):
    """承認/却下/質問ボタン。押下時にオーナー検証して decisions に記録する。"""

    def __init__(self, bot: RyzaBot, proposal_ref: str, kind: str = "other") -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.proposal_ref = proposal_ref
        self.kind = kind if kind in KINDS else "other"

    async def _record(self, interaction: discord.Interaction, decision: str) -> None:
        user_id = str(interaction.user.id)
        msg_id = str(interaction.message.id) if interaction.message else None
        try:
            with connect() as conn:
                record_decision(
                    conn,
                    self.proposal_ref,
                    decision,
                    user_id,
                    self.bot.owner_ids,
                    kind=self.kind,
                    channel_msg_id=msg_id,
                )
                conn.commit()
        except NotOwnerError:
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001 - UniqueViolation 等は利用者に通知
            await interaction.response.send_message(f"記録できませんでした: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(f"記録しました: {decision}", ephemeral=True)

    @discord.ui.button(label="承認", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._record(interaction, "approve")

    @discord.ui.button(label="却下", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._record(interaction, "reject")

    @discord.ui.button(label="質問", style=discord.ButtonStyle.secondary)
    async def question(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._record(interaction, "question")


class ResumeConfirmView(discord.ui.View):
    """/resume の2段階目。確認ボタンで Kill Switch を解除する。"""

    def __init__(self, bot: RyzaBot) -> None:
        super().__init__(timeout=60)
        self.bot = bot

    @discord.ui.button(label="復帰を確定", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        try:
            with connect() as conn:
                killswitch.release(
                    conn, str(interaction.user.id), self.bot.owner_ids, confirmed=True
                )
                conn.commit()
        except NotOwnerError:
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        await interaction.response.send_message("Kill Switch を解除しました(通常運転)。")


# ────────────────────────────────────────────────────────────────────────────
# Bot 本体
# ────────────────────────────────────────────────────────────────────────────
class RyzaBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.owner_ids_list = owner_ids()
        self.category_id = category_id()

    @property
    def owner_ids(self) -> list[str]:  # type: ignore[override]
        return self.owner_ids_list

    async def setup_hook(self) -> None:
        self._register_commands()
        guild_id = os.environ.get("RYZA_GUILD_ID")
        if guild_id and guild_id.isdigit():
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.poll_outbox.start()
        self.daily_report.start()

    # ── ループ ────────────────────────────────────────────────────────────
    @tasks.loop(seconds=POLL_SECONDS)
    async def poll_outbox(self) -> None:
        await self._deliver_once()

    @poll_outbox.before_loop
    async def _before_poll(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(time=DAILY_TIME)
    async def daily_report(self) -> None:
        now = dt.datetime.now(dt.UTC)
        with connect() as conn:
            with start_run("bot.daily", conn=conn) as r:
                daily_mod.enqueue_daily(conn, now, r.run_id)
            conn.commit()

    @daily_report.before_loop
    async def _before_daily(self) -> None:
        await self.wait_until_ready()

    async def _deliver_once(self) -> None:
        loop = self.loop

        def send_fn(msg: outbox.OutboxMessage) -> str:
            # 論理チャンネル → 実 ID は ops.discord_channels(起動時 ensure 済み)から解決。
            with connect() as resolve_conn:
                channel_id = channels.resolve(resolve_conn, msg.channel)
            if channel_id is None:
                raise RuntimeError(f"チャンネル未解決(ensure 前?): {msg.channel}")
            embed = dict_to_embed(msg.embed)
            if msg.urgent:
                embed.color = discord.Color(COLOR_FLASH)
            channel = self.get_channel(int(channel_id))
            if channel is None:
                raise RuntimeError(f"チャンネル取得失敗: {channel_id}")
            # discord の I/O はコルーチン。同期 send_fn からイベントループに投げて待つ。
            import asyncio

            fut = asyncio.run_coroutine_threadsafe(channel.send(embed=embed), loop)
            sent = fut.result(timeout=15)
            return str(sent.id)

        try:
            with connect() as conn:
                outbox.deliver_pending(conn, send_fn)
        except Exception:  # noqa: BLE001 - 配送ループは死なせない
            log.exception("outbox 配送でエラー")

    # ── チャンネル ensure(カテゴリ配下に4チャンネルを確保して DB 記録)──────────
    async def ensure_channels(self) -> None:
        """指定カテゴリ配下に4チャンネルを ensure し、結果を ops.discord_channels に記録する。"""
        category = self.get_channel(self.category_id)
        if not isinstance(category, discord.CategoryChannel):
            log.error("カテゴリ %s が見つからない/種別不一致。ensure を中止", self.category_id)
            return
        existing_by_name = {ch.name: str(ch.id) for ch in category.text_channels}
        plans = channels.plan_ensure(existing_by_name)
        with connect() as conn:
            for plan in plans:
                if plan.action == "reuse" and plan.channel_id is not None:
                    channel_id = plan.channel_id
                else:
                    created = await category.guild.create_text_channel(
                        plan.channel_name, category=category
                    )
                    channel_id = str(created.id)
                    log.info("チャンネル作成: %s (%s)", plan.channel_name, channel_id)
                channels.record_channel(
                    conn, plan.logical, plan.channel_name, channel_id, str(self.category_id)
                )
            conn.commit()

    # ── コマンド登録 ───────────────────────────────────────────────────────
    def _register_commands(self) -> None:
        @self.tree.command(name="kill", description="Kill Switch を有効化(発注停止)")
        @app_commands.describe(reason="停止理由(任意)")
        async def kill(interaction: discord.Interaction, reason: str | None = None) -> None:
            try:
                with connect() as conn:
                    killswitch.engage(
                        conn, str(interaction.user.id), self.owner_ids, reason=reason
                    )
                    conn.commit()
            except NotOwnerError:
                await interaction.response.send_message("権限がありません。", ephemeral=True)
                return
            await interaction.response.send_message(
                f"⛔ Kill Switch 有効化。全発注を停止します。理由: {reason or '(なし)'}"
            )

        @self.tree.command(name="resume", description="Kill Switch を解除(2段階確認)")
        async def resume(interaction: discord.Interaction) -> None:
            if str(interaction.user.id) not in {str(o) for o in self.owner_ids}:
                await interaction.response.send_message("権限がありません。", ephemeral=True)
                return
            await interaction.response.send_message(
                "本当に復帰しますか?下のボタンで確定してください。",
                view=ResumeConfirmView(self),
                ephemeral=True,
            )

    # ── 起動時: チャンネル ensure + 再起動通知 ───────────────────────────────
    async def on_ready(self) -> None:
        log.info("ready as %s (guilds=%d)", self.user, len(self.guilds))
        try:
            await self.ensure_channels()
        except Exception:  # noqa: BLE001 - ensure 失敗でも Bot は生かす
            log.exception("チャンネル ensure に失敗")
        now = dt.datetime.now(dt.UTC)
        embed = {
            "title": "Ryza Bot 起動",
            "description": f"再起動しました({now.astimezone(JST):%Y-%m-%d %H:%M JST})。",
            "color": COLOR_NORMAL,
        }
        try:
            with connect() as conn:
                with start_run("bot.startup", conn=conn) as r:
                    outbox.enqueue(conn, "ops", embed, r.run_id)  # #運営 へ
                conn.commit()
        except Exception:  # noqa: BLE001
            log.exception("起動通知の投入に失敗")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    bot = RyzaBot()
    bot.run(load_token())


if __name__ == "__main__":
    main()
