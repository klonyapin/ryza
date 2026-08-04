"""Ryza Discord Bot 常駐エントリポイント(discord.py 2.6 / GCE 常駐)。

systemd(Restart=always)で常駐し:
- 5秒間隔で ``press.outbox`` を配送し(``outbox.deliver_pending``)、続けて開発室
  (``ops.dev_chat``・0024)の未中継発言を #dev へ中継(``devchat.relay_pending``)。
  順序は固定で、非緊急の中継を Kill Switch 通報の配送より前に置かない
- 18:00 JST に日報を投入(``daily.enqueue_daily``)し、続けてアイコン URL の指紋を
  再検証(``ops.icon_revalidate`` — 0033。差し替え・到達不能を検知した日だけ #運営 へ)
- ``#承認`` のボタン押下を ``governance.decisions`` に記録(オーナー検証)。みなし承認の
  発効通知には**否認ボタン**を付け、押下 → 理由入力(モーダル)→ ``governance.notices``
  経由で否認記録+``#運営`` への取消義務リマインドを1トランザクションで書く。同じ配線を
  ``/veto``(ボタン View は再起動をまたがないため)と ``/unveto``(誤った否認の撤回・
  0021 の ``withdrawal``)にも持たせる(定款第3条2号「代表はいつでも否認できる」)
- ``/kill``(凍結)``/winddown``(計画的現金化)``/flatten``(緊急清算・2段階)
  ``/resume``(復帰・2段階)で Kill Switch 状態機械を操作(killswitch.py・保護領域)
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

import asyncio
import datetime as dt
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ryza import org
from ryza.bot import CHANNELS, COLOR_FLASH, COLOR_NORMAL, channels, killswitch, outbox, webhooks
from ryza.bot import daily as daily_mod
from ryza.bot.approvals import KINDS, NotOwnerError, parse_proposal, record_decision
from ryza.bot.daily import JST
from ryza.db.conn import connect
from ryza.governance import devchat, notices
from ryza.ops import icon_revalidate
from ryza.provenance import start_run

log = logging.getLogger("ryza.bot")

POLL_SECONDS = 5.0
DAILY_TIME = dt.time(hour=18, minute=0, tzinfo=JST)  # 18:00 JST


def _mask_channel_id(channel_id: object) -> str:
    """Discord チャネル ID をログ・例外向けに伏字化する(pass4-security 所見5・F-13-6)。

    Discord サーバーの内部 ID は「システム上の致命的な秘密」ではないが、ログや例外
    メッセージに素で流出させる利得は無い(``mask_url`` と同じ流儀)。数字の下 4 桁
    だけ残し、それ以外は ``*`` で置き換える(識別しやすさの下限は保つ)。文字列に
    ならない場合や短すぎる場合は全体を伏せる。
    """
    s = str(channel_id) if channel_id is not None else ""
    if not s:
        return "<masked>"
    if len(s) <= 4:
        return "*" * len(s)
    return "*" * (len(s) - 4) + s[-4:]


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
    # google-cloud-secret-manager は proto-plus/protobuf の版差で壊れやすいため、
    # GCE メタデータトークン + REST(stdlib のみ)で取得する
    import base64
    import json as _json
    import urllib.request

    meta = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        "service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    access_token = _json.load(urllib.request.urlopen(meta, timeout=10))["access_token"]
    req = urllib.request.Request(
        f"https://secretmanager.googleapis.com/v1/projects/{project}"
        f"/secrets/{secret}/versions/latest:access",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    payload = _json.load(urllib.request.urlopen(req, timeout=10))
    return base64.b64decode(payload["payload"]["data"]).decode("utf-8")


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
    # 発信者キャラクター(config/org.yaml — 生成側が embed_json に author を入れる)。
    # icon_url が 404 でも Discord は名前だけで表示するため配送は失敗しない。
    author = data.get("author")
    if author and author.get("name"):
        embed.set_author(name=author["name"], icon_url=author.get("icon_url"))
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


def _veto_sync(
    bot: RyzaBot, proposal_ref: str, reason: str, user_id: str, origin: str
) -> None:
    """否認の記録+``#運営`` 通知(同期・DB I/O)。イベントループから ``to_thread`` で呼ぶ。

    ``origin``(0030)は呼び出し元が渡す。ボタン経路(``VetoModal``)と ``/veto`` は
    **同じ ``job_name`` で Run を開く**ため、``meta.runs`` を辿っても両者は区別できない。
    この関数を共有する以上、経路の申告はここより上でしかできない。
    """
    with connect() as conn:
        r = start_run("bot.governance.veto", conn=conn)
        notices.apply_veto(
            conn, proposal_ref, reason,
            vetoed_by=user_id, owner_ids=bot.owner_ids, run_id=r.run_id,
            origin=origin,
        )
        r.finish("success")
        conn.commit()


def _withdraw_veto_sync(
    bot: RyzaBot, proposal_ref: str, reason: str, user_id: str, origin: str
) -> None:
    """否認の撤回(同期・DB I/O)。"""
    with connect() as conn:
        r = start_run("bot.governance.veto_withdrawal", conn=conn)
        notices.withdraw_veto(
            conn, proposal_ref, reason,
            vetoed_by=user_id, owner_ids=bot.owner_ids, run_id=r.run_id,
            origin=origin,
        )
        r.finish("success")
        conn.commit()


async def _run_governance_action(
    interaction: discord.Interaction, action, ok_message: str, fail_prefix: str
) -> None:
    """defer → ワーカースレッドで DB 操作 → followup の共通形。

    **なぜ defer が要るか**(独立役員審査 中-8): Discord の初回応答は 3 秒以内に返す必要が
    あり、同期 DB I/O をイベントループ上で待つとハートビートも塞ぐ。3 秒を超えると
    Discord 側が失敗表示にするため、**記録は成功しているのに代表には失敗に見える** —
    定款第3条の否認において最悪の食い違いになる。先に defer(ephemeral)で応答枠を確保し、
    DB 操作は ``asyncio.to_thread`` へ逃がす(配送ループが ``_deliver_sync`` でしている扱いと同じ)。
    """
    await interaction.response.defer(ephemeral=True)
    try:
        await asyncio.to_thread(action)
    except NotOwnerError:
        await interaction.followup.send("権限がありません。", ephemeral=True)
        return
    except Exception as exc:  # noqa: BLE001 - 対象不明・二重否認などは利用者に通知
        await interaction.followup.send(f"{fail_prefix}: {exc}", ephemeral=True)
        return
    await interaction.followup.send(ok_message, ephemeral=True)


class VetoModal(discord.ui.Modal, title="否認(定款第3条)"):
    """否認理由を受け取り、否認記録+``#運営`` への取消義務リマインドを書く。

    **なぜモーダルか**: ``governance.decision_vetoes.reason`` は NOT NULL(空文字も CHECK で
    拒否)である。理由の無い否認は、執行側が何を巻き戻すべきか判断できず、定款第3条が課す
    取消義務を実行できないためである。ボタン単独では理由を集められないので、押下 → モーダルの
    2段にする(``/flatten`` ``/resume`` の2段階確認と同じ流儀 — 不可逆な操作は1クリックで
    確定させない)。

    記録と通知の原子性・オーナー検証・対象取り違えの検出は ``governance.notices`` 側にある
    (main は保護領域なので配線のみを置く)。
    """

    reason = discord.ui.TextInput(
        label="否認理由(必須)",
        style=discord.TextStyle.paragraph,
        placeholder="何が問題で否認するのか。執行側はこれを起点に取消範囲を決める",
        required=True,
        max_length=500,
    )

    def __init__(self, bot: RyzaBot, proposal_ref: str) -> None:
        super().__init__()
        self.bot = bot
        self.proposal_ref = proposal_ref

    async def on_submit(self, interaction: discord.Interaction) -> None:
        user_id = str(interaction.user.id)
        reason = str(self.reason)
        await _run_governance_action(
            interaction,
            # 出所: #承認 の否認ボタン → 理由モーダル(0030 の discord_button)。
            lambda: _veto_sync(
                self.bot, self.proposal_ref, reason, user_id, "discord_button"
            ),
            ok_message=(
                f"⛔ 否認を記録しました({self.proposal_ref})。"
                "#運営 に取消義務のリマインドを投稿します。誤操作なら `/unveto` で撤回できます。"
            ),
            fail_prefix="否認できませんでした",
        )


class VetoView(discord.ui.View):
    """みなし承認の発効通知に付く否認ボタン(定款第3条2号「いつでも否認できる」)。

    承認/却下ボタン(``ApprovalView``)は付けない。みなし承認は通知の時点で発効済みであり、
    そこで「承認」を押させると1提案=1決定の UNIQUE に当たって失敗するだけで、代表に
    残された選択肢(否認)を隠してしまう。
    """

    def __init__(self, bot: RyzaBot, proposal_ref: str) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.proposal_ref = proposal_ref

    @discord.ui.button(label="否認", style=discord.ButtonStyle.danger)
    async def veto(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(VetoModal(self.bot, self.proposal_ref))


class ResumeConfirmView(discord.ui.View):
    """/resume の2段階目。確認ボタンで Kill Switch を解除する。"""

    def __init__(self, bot: RyzaBot) -> None:
        super().__init__(timeout=60)
        self.bot = bot

    @discord.ui.button(label="復帰を確定", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        try:
            with connect() as conn:
                result = killswitch.release(
                    conn, str(interaction.user.id), self.bot.owner_ids, confirmed=True
                )
                conn.commit()
        except NotOwnerError:
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        except killswitch.InvalidTransitionError as exc:
            await interaction.response.send_message(f"復帰できません: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Kill Switch を解除しました({result.previous} → 通常運転)。"
        )


class FlattenConfirmView(discord.ui.View):
    """/flatten の2段階目。確認ボタンで成行即時全清算(決定論コード)を開始する。"""

    def __init__(self, bot: RyzaBot, reason: str | None) -> None:
        super().__init__(timeout=60)
        self.bot = bot
        self.reason = reason

    @discord.ui.button(label="全清算を確定", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        try:
            with connect() as conn:
                r = start_run("bot.killswitch.flatten", conn=conn)
                result = killswitch.flatten(
                    conn,
                    str(interaction.user.id),
                    self.bot.owner_ids,
                    r.run_id,
                    confirmed=True,
                    reason=self.reason,
                    hook=self.bot.execution_hook,
                )
                r.finish("success")
                conn.commit()
        except NotOwnerError:
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        except killswitch.InvalidTransitionError as exc:
            await interaction.response.send_message(f"清算できません: {exc}", ephemeral=True)
            return
        note = (
            ""
            if result.hook_engaged
            else "\n⚠ 執行層未接続のため状態遷移のみ(#運営 に通知済み)。"
        )
        await interaction.response.send_message(
            f"🚨 緊急清算を確定しました({result.previous} → flattening)。"
            f"成行で全ポジションを清算します。{note}"
        )


# ────────────────────────────────────────────────────────────────────────────
# Bot 本体
# ────────────────────────────────────────────────────────────────────────────
class RyzaBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        # 親クラス(commands.Bot)が __init__ 内で self.owner_ids に代入するため、
        # プロパティで上書きせず属性として設定する(is_owner 側は文字列比較で吸収)
        self.owner_ids = {int(o) for o in owner_ids() if str(o).isdigit()}
        self.category_id = category_id()
        # 執行層(ブローカーアダプタ)は未実装。実装後にここへ ExecutionHook を注入する。
        # None の間、/winddown /flatten は状態遷移のみ記録し #運営 へ「執行層未接続」を通知する。
        self.execution_hook: killswitch.ExecutionHook | None = None

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
        # _deliver_once は同期 DB I/O と run_coroutine_threadsafe(...).result() を含むため、
        # イベントループ上で直接実行するとハートビートを塞ぎ自己デッドロックする。
        # 必ずワーカースレッドへ逃がす。
        #
        # **配送が先、開発室(0024)の中継は後**(独立役員審査 重大-2)。中継は非緊急の
        # 開発連絡であり、Kill Switch 通報・速報を含む press.outbox の配送より前に
        # 置いてはならない。中継が DB で詰まると(connect() に timeout が無いため)
        # 同ティックの配送ごと待たされ、緊急通報の到達が遅れる。順序を入れ替えても
        # 中継が Discord に出るのは次ティック(最大 5 秒後)で、失うものは無い。
        await asyncio.to_thread(self._deliver_sync)
        await asyncio.to_thread(self._relay_dev_chat_sync)

    @poll_outbox.before_loop
    async def _before_poll(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(time=DAILY_TIME)
    async def daily_report(self) -> None:
        now = dt.datetime.now(dt.UTC)
        with connect() as conn:
            r = start_run("bot.daily", conn=conn)
            daily_mod.enqueue_daily(conn, now, r.run_id)
            r.finish("success")
            conn.commit()
        # 日報とは別の Run として分ける(片方の失敗でもう片方を巻き込まない)。
        # 外向き HTTP を伴うため、DB だけの日報より先に置かない。
        await asyncio.to_thread(self._revalidate_icons_sync)

    def _revalidate_icons_sync(self) -> None:
        """アイコン URL の指紋を再検証する(0033・独立役員審査 0020 C-7 の検知側是正)。

        リマインダー ``icon-rehost-storage`` は週次を想定していたが日次で回す。検査は
        メンバー数(9件)ぶんの HEAD だけで、通知は遷移があった日にしか出ない
        (``icon_revalidate.run_revalidation``)ため、頻度を上げても増える騒音は無く、
        すり替えの検知から通知までのラグだけが最大7日から1日に縮む。

        **失敗は握る。ただし黙って消さない**(独立役員 追補審査 C-14)。外部サイトへの HTTP を
        伴う検査であり、例外で Bot の常駐ループを落とすと配送・Kill Switch 操作まで巻き添えに
        なる(0020 C-2 と同じ判断)。一方で、例外時は同じ接続の rollback で ``meta.runs`` の
        開始行ごと消えるため、握るだけでは「静寂=変化なし」と「静寂=そもそも動いていない」が
        区別できなくなる — 本ジョブが「遷移が無い日は投稿しない」設計を採る根拠が、失敗した
        日にこそ崩れる。したがって失敗は**別接続(autocommit)**で failed の Run として残す。
        """
        try:
            with connect() as conn:
                r = start_run("bot.icon_revalidate", conn=conn)
                result = icon_revalidate.run_revalidation(conn, r.run_id)
                r.record_runtime(result.as_runtime())
                r.finish("success")
                conn.commit()
            log.info("アイコン再検証: %s", result.as_runtime())
        except Exception as exc:  # noqa: BLE001 - 再検証の失敗で常駐ループを死なせない
            log.exception("アイコン再検証でエラー")
            self._record_failed_run("bot.icon_revalidate", exc)

    def _record_failed_run(self, job: str, exc: BaseException) -> None:
        """握った例外を failed の Run として**別接続**に残す(追補審査 C-14)。

        失敗した接続はトランザクションがアボートしており、そこへ書いても rollback で
        消える。``conn`` を渡さない ``start_run`` は autocommit 接続を自前で開いて各文を
        即時確定し、``finish`` で閉じる。この記録自体が失敗しても(DB 断など)ログだけ
        残して常駐ループは続ける — 記録のために Bot を落とさない。
        """
        try:
            r = start_run(job)
            r.record_runtime({"error": f"{type(exc).__name__}: {exc}"[:500]})
            r.finish("failed")
        except Exception:  # noqa: BLE001 - 失敗の記録に失敗しても落とさない
            log.exception("失敗 Run の記録にも失敗した: %s", job)

    @daily_report.before_loop
    async def _before_daily(self) -> None:
        await self.wait_until_ready()

    def _relay_dev_chat_sync(self) -> None:
        """開発室(``ops.dev_chat``・0024)の未中継の発言を #dev へ中継する。

        代表がダッシュボードから書いた連絡と、設計リードが CLI から返した回答の双方を、
        Discord のブリッジチャンネルへ流す経路。中継の実体は ``press.outbox`` への
        enqueue で、Discord API は叩かない(配送の冪等・リトライは既存経路に委ねる)。

        未中継が無いときは ``meta.runs`` に行を作らない — 5 秒ごとの空振りで実行記録を
        埋めないため(``devchat.has_pending``)。

        **部分失敗は Run に反映する**(独立役員審査 中-7)。1 件でも中継できなければ
        status=failed とし、占有・成功・失敗の件数を ``params.runtime`` に残す。
        全滅しても success が並ぶと、ダッシュボードの「ジョブ」ページからは障害が
        見えない(UI 側は滞留そのものも警告する)。
        """
        try:
            with connect() as conn:
                if not devchat.has_pending(conn):
                    return
                r = start_run("bot.devchat.relay", conn=conn)
                result = devchat.relay_pending(conn, r.run_id)
                r.record_runtime(result.as_runtime())
                r.finish("success" if result.ok else "failed")
                conn.commit()
            if result.relayed:
                log.info("開発室を %d 件中継した: %s", len(result.relayed), result.relayed)
            if result.failed:
                log.error(
                    "開発室の中継に失敗した %d/%d 件(未中継のまま次回リトライ): %s",
                    len(result.failed), result.claimed, result.failed,
                )
        except Exception:  # noqa: BLE001 - 中継の失敗で配送ループを死なせない
            log.exception("開発室の中継でエラー")

    def _deliver_sync(self) -> None:
        loop = self.loop

        def send_fn(msg: outbox.OutboxMessage) -> str:
            # 論理チャンネル → 実 ID / webhook は起動時 ensure の記録(ops.*)から解決。
            # アイコン上書き(0020)も同じ接続で読む。**投入時ではなく配送時**に解決する
            # ことで、代表がダッシュボードで差し替えた直後の投稿から新アイコンになる
            # (キャッシュしない — org.icon_overrides)。
            deemed_target = notices.DeemedViewTarget(None)
            with connect() as resolve_conn:
                channel_id = channels.resolve(resolve_conn, msg.channel)
                webhook_url = webhooks.resolve_webhook(resolve_conn, msg.channel)
                if msg.channel == "approval":
                    # 否認ボタンは「実在する deemed 決定」の通知にだけ付ける(審査 重要-4)。
                    # フッターは誰でも書ける自己申告であり、偽の通知に本物の決定を指す
                    # ボタンを付けると代表が別提案の否認を押させられる。
                    deemed_target = notices.resolve_deemed_view(resolve_conn, msg.embed)
                    if deemed_target.warning:
                        log.warning("否認ボタンを付与しない: %s", deemed_target.warning)
                try:
                    overrides = org.icon_overrides(resolve_conn)
                except Exception:  # noqa: BLE001 - フェイルオープン(独立役員審査 0020 C-2)
                    # **アイコンが古いのは許容、配送停止は不許容**。ここで例外を上げると
                    # outbox.deliver_pending が当該メッセージを送れないまま次へ進み
                    # (無言の再試行待ち)、0020 未適用の環境や一時的な DB エラーで
                    # 速報・Kill Switch 通報を含む全 Discord 配送が静かに止まる。
                    # 見た目の鮮度より配送の到達性を優先し、台帳の値で送る。
                    log.warning("アイコン上書きを読めないため台帳の値で配送する", exc_info=True)
                    resolve_conn.rollback()  # 失敗でアボートした tx を明示的に畳む
                    overrides = {}
            if channel_id is None:
                # 論理チャネル名(press.outbox.channel: approval/ops/press/dev)は
                # 秘密ではなく素で載せる。Discord 内部 ID を露出させるのはこの経路の
                # 隣にある「取得失敗」側の raise だけで、そちらは _mask_channel_id
                # で伏字化する(pass4-security 所見5 の是正・F-13-6)。
                raise RuntimeError(f"チャンネル未解決(ensure 前?): {msg.channel}")
            # #承認 向けで proposal footer を持つ embed には承認ボタンを付ける
            # (凍結中の例外的取引などを1件ずつオーナー承認する経路)。
            # みなし承認の発効通知(deemed footer)は既に発効済みなので、承認ボタンではなく
            # 否認ボタンを付ける(定款第3条2号)。両者は排他で、**deemed が優先**(軽微-9)—
            # マーカーが両方読める embed に承認ボタンを出すと、発効済みの提案をもう一度
            # 承認させることになり、押下は UNIQUE 違反で失敗するだけになる。
            deemed_ref = deemed_target.ref
            parsed = (
                parse_proposal(msg.embed)
                if msg.channel == "approval" and deemed_ref is None
                else None
            )
            # 発信者の内部キーは **embed ではなく列**(0032・独立役員審査 0020 C-10)から
            # 渡す。0032 以前に投入された行は列が NULL で、その場合だけ resolve_author が
            # embed 内の旧キーへフォールバックする(後方互換)。
            embed_json = org.apply_icon_overrides(
                msg.embed, overrides, member_id=msg.author_member_id
            )
            # webhook 方式(代表指示 2026-08-03): author キャラクターを username /
            # avatar_url へ昇格して投稿(webhooks.py)。承認ボタン付きは webhook に
            # コンポーネントを付けられないため従来の Bot 投稿のまま(否認ボタンも同じ)。
            if webhook_url is not None and parsed is None and deemed_ref is None:
                return webhooks.post(webhook_url, embed_json, urgent=msg.urgent)
            embed = dict_to_embed(embed_json)
            if msg.urgent:
                embed.color = discord.Color(COLOR_FLASH)
            channel = self.get_channel(int(channel_id))
            if channel is None:
                raise RuntimeError(f"チャンネル取得失敗: id={_mask_channel_id(channel_id)}")
            send_kwargs: dict = {"embed": embed}
            if deemed_ref is not None:
                send_kwargs["view"] = VetoView(self, deemed_ref)
            elif parsed is not None:
                send_kwargs["view"] = ApprovalView(self, parsed[0], kind=parsed[1])
            # discord の I/O はコルーチン。ワーカースレッドからイベントループに投げて待つ。
            fut = asyncio.run_coroutine_threadsafe(channel.send(**send_kwargs), loop)
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

    # ── webhook ensure(webhook 方式配送 — webhooks.py・代表指示 2026-08-03)────
    async def ensure_webhooks(self) -> None:
        """各チャンネルの webhook ``ryza-org`` を ensure し ``ops.discord_webhooks`` へ記録。

        Manage Webhooks 権限が無いチャンネルはスキップして Bot 投稿(embed author 方式)へ
        自動フォールバックし、#運営 へ起動時に一度だけ案内する。
        """
        missing: list[str] = []
        with connect() as conn:
            for logical in CHANNELS:
                channel_id = channels.resolve(conn, logical)
                channel = self.get_channel(int(channel_id)) if channel_id else None
                if channel is None:
                    continue
                try:
                    hooks = await channel.webhooks()
                    hook = next(
                        (h for h in hooks if h.name == webhooks.WEBHOOK_NAME), None
                    )
                    if hook is None:
                        hook = await channel.create_webhook(name=webhooks.WEBHOOK_NAME)
                    webhooks.record_webhook(conn, logical, str(hook.id), hook.url)
                except discord.Forbidden:
                    missing.append(logical)
            if missing:
                notice = {
                    "title": "Webhook 権限の案内",
                    "description": (
                        f"チャンネル {', '.join(missing)} で webhook を確保できませんでした。"
                        "Bot に Manage Webhooks 権限を付与すると、投稿がキャラクターの"
                        "名前(役職)とアイコンで完全に表示されるようになります"
                        "(それまでは embed author 表示で代替)。"
                    ),
                    "color": COLOR_NORMAL,
                }
                r = start_run("bot.webhook_notice", conn=conn)
                outbox.enqueue(conn, "ops", notice, r.run_id)
                r.finish("success")
            conn.commit()

    # ── コマンド登録 ───────────────────────────────────────────────────────
    def _register_commands(self) -> None:
        @self.tree.command(name="kill", description="凍結: 全新規発注停止・ポジション維持")
        @app_commands.describe(reason="停止理由(任意)")
        async def kill(interaction: discord.Interaction, reason: str | None = None) -> None:
            try:
                with connect() as conn:
                    r = start_run("bot.killswitch.kill", conn=conn)
                    result = killswitch.engage(
                        conn,
                        str(interaction.user.id),
                        self.owner_ids,
                        reason=reason,
                        run_id=r.run_id,
                        hook=self.execution_hook,
                    )
                    r.finish("success")
                    conn.commit()
            except NotOwnerError:
                await interaction.response.send_message("権限がありません。", ephemeral=True)
                return
            await interaction.response.send_message(
                f"⛔ 凍結({result.previous} → frozen)。全新規発注を停止します"
                f"(ポジションは維持)。理由: {reason or '(なし)'}"
            )

        @self.tree.command(
            name="winddown", description="計画的現金化: 決定論アルゴで段階的に全ポジションを清算"
        )
        @app_commands.describe(reason="理由(任意)")
        async def winddown(interaction: discord.Interaction, reason: str | None = None) -> None:
            try:
                with connect() as conn:
                    r = start_run("bot.killswitch.winddown", conn=conn)
                    result = killswitch.winddown(
                        conn,
                        str(interaction.user.id),
                        self.owner_ids,
                        r.run_id,
                        reason=reason,
                        hook=self.execution_hook,
                    )
                    r.finish("success")
                    conn.commit()
            except NotOwnerError:
                await interaction.response.send_message("権限がありません。", ephemeral=True)
                return
            except killswitch.InvalidTransitionError as exc:
                await interaction.response.send_message(f"実行できません: {exc}", ephemeral=True)
                return
            note = (
                ""
                if result.hook_engaged
                else "\n⚠ 執行層未接続のため状態遷移のみ(#運営 に通知済み)。"
            )
            await interaction.response.send_message(
                f"🪙 計画的現金化を開始({result.previous} → winding_down)。"
                f"理由: {reason or '(なし)'}{note}"
            )

        @self.tree.command(
            name="flatten", description="緊急清算: 成行で即時全清算(2段階確認)"
        )
        @app_commands.describe(reason="理由(任意)")
        async def flatten(interaction: discord.Interaction, reason: str | None = None) -> None:
            try:
                with connect() as conn:
                    current = killswitch.request_flatten(
                        conn, str(interaction.user.id), self.owner_ids, reason=reason
                    )
                    conn.commit()
            except NotOwnerError:
                await interaction.response.send_message("権限がありません。", ephemeral=True)
                return
            except killswitch.InvalidTransitionError as exc:
                await interaction.response.send_message(f"実行できません: {exc}", ephemeral=True)
                return
            await interaction.response.send_message(
                f"🚨 成行で **全ポジションを即時清算** します(現在: {current})。"
                "コスト・スリッページを受け入れる緊急用です。下のボタンで確定してください。",
                view=FlattenConfirmView(self, reason),
                ephemeral=True,
            )

        @self.tree.command(name="veto", description="否認: 発効中の承認決定を取り消す(定款第3条)")
        @app_commands.describe(
            proposal_ref="提案参照(通知の「提案参照」フィールドの値・PR URL 等)",
            reason="否認理由(必須。執行側はこれを起点に取消範囲を決める)",
        )
        async def veto(interaction: discord.Interaction, proposal_ref: str, reason: str) -> None:
            # ボタン経路(VetoView)と同じ配線をコマンドでも用意する。ボタン View は
            # Bot 再起動をまたいで復元されない(既存 ApprovalView と同じ制約)ため、
            # ボタンだけだと「代表はいつでも否認できる」(第3条2号)が再起動で切れる。
            user_id = str(interaction.user.id)
            await _run_governance_action(
                interaction,
                # 出所: スラッシュコマンド(0030 の discord_command)。ボタン経路と同じ
                # job_name で Run を開くので、run_id では両者を区別できない。
                lambda: _veto_sync(self, proposal_ref, reason, user_id, "discord_command"),
                ok_message=(
                    f"⛔ 否認を記録しました({proposal_ref})。"
                    "#運営 に取消義務のリマインドを投稿します。"
                    "誤操作なら `/unveto` で撤回できます。"
                ),
                fail_prefix="否認できません",
            )

        @self.tree.command(
            name="unveto", description="否認の撤回: 誤って押した否認を取り消す(定款第3条)"
        )
        @app_commands.describe(
            proposal_ref="提案参照(否認通知の「提案参照」フィールドの値)",
            reason="撤回理由(必須。誤操作の是正か方針変更かが残らないと否認統計が壊れる)",
        )
        async def unveto(
            interaction: discord.Interaction, proposal_ref: str, reason: str
        ) -> None:
            # 否認がボタン1つで押せる以上、復旧経路も同じ強度で用意する(0021 審査 C-3:
            # UNIQUE(proposal_ref) により提案の再記録ができず、撤回が唯一の復旧手段)。
            user_id = str(interaction.user.id)
            await _run_governance_action(
                interaction,
                # 出所: スラッシュコマンド(0030 の discord_command)。撤回はボタン経路が
                # 無く /unveto だけだが、値は経路の申告であって唯一性の申告ではない。
                lambda: _withdraw_veto_sync(
                    self, proposal_ref, reason, user_id, "discord_command"
                ),
                ok_message=f"否認を撤回しました({proposal_ref})。#運営 に通知します。",
                fail_prefix="撤回できません",
            )

        @self.tree.command(name="resume", description="復帰: Kill Switch を解除(2段階確認)")
        async def resume(interaction: discord.Interaction) -> None:
            if str(interaction.user.id) not in {str(o) for o in self.owner_ids}:
                await interaction.response.send_message("権限がありません。", ephemeral=True)
                return
            with connect() as conn:
                current = killswitch.get_state(conn)
            if current == killswitch.NORMAL:
                await interaction.response.send_message("既に通常運転です。", ephemeral=True)
                return
            await interaction.response.send_message(
                f"本当に復帰しますか(現在: {current})?下のボタンで確定してください。",
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
        try:
            await self.ensure_webhooks()
        except Exception:  # noqa: BLE001 - webhook 無しでも Bot 投稿で配送は続く
            log.exception("webhook ensure に失敗")
        now = dt.datetime.now(dt.UTC)
        embed = {
            "title": "Ryza Bot 起動",
            "description": f"再起動しました({now.astimezone(JST):%Y-%m-%d %H:%M JST})。",
            "color": COLOR_NORMAL,
        }
        try:
            with connect() as conn:
                r = start_run("bot.startup", conn=conn)
                outbox.enqueue(conn, "ops", embed, r.run_id)  # #運営 へ
                r.finish("success")
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
