"""問い合わせCog: `/contact`（誰でも）と `/inbox`（社長専用）。

- `/contact` … 種別を選ぶとフォームが開き、送信すると社長にDMが届く。
  DMが届かない場合（社長がDMを拒否している等）でも内容はDBに保存されるので、
  `/inbox` で必ず確認できる。
- `/inbox` … 未読の問い合わせを一覧・既読化・その場から返信（BotがDMを代行送信）。

連投対策: 同一ユーザーは COOLDOWN 秒に1件・24時間で DAILY_LIMIT 件まで。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from cogs.admin import is_owner

COOLDOWN = 180          # 次の送信までの待ち時間（秒）
DAILY_LIMIT = 5         # 24時間あたりの上限件数
DAY = 86400

KIND_CHOICES = [
    app_commands.Choice(name="💡 要望・こんな機能がほしい", value="要望"),
    app_commands.Choice(name="🐛 不具合の報告", value="不具合"),
    app_commands.Choice(name="❓ 質問・使い方がわからない", value="質問"),
    app_commands.Choice(name="💰 コイン・換金に関すること", value="コイン"),
    app_commands.Choice(name="📮 その他", value="その他"),
]

KIND_COLOR = {
    "要望": 0x3498DB, "不具合": 0xE74C3C, "質問": 0x9B59B6,
    "コイン": 0xF1C40F, "その他": 0x95A5A6,
}


class ContactModal(discord.ui.Modal, title="お問い合わせ・ご要望"):
    subject = discord.ui.TextInput(
        label="件名", placeholder="ひとことで要点を（例：探索で〇〇したい）",
        max_length=100, required=True,
    )
    body = discord.ui.TextInput(
        label="内容", style=discord.TextStyle.paragraph,
        placeholder="くわしく書いてください。不具合の場合は「何をしたら」「どうなったか」を書くと助かります。",
        max_length=1500, required=True,
    )

    def __init__(self, cog: "Contact", kind: str):
        super().__init__()
        self.cog = cog
        self.kind = kind

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.submit(interaction, self.kind, str(self.subject), str(self.body))


class ReplyModal(discord.ui.Modal, title="問い合わせに返信"):
    body = discord.ui.TextInput(
        label="返信内容", style=discord.TextStyle.paragraph,
        placeholder="送信相手にBotからDMで届きます。", max_length=1500, required=True,
    )

    def __init__(self, cog: "Contact", contact_id: int):
        super().__init__()
        self.cog = cog
        self.contact_id = contact_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.send_reply(interaction, self.contact_id, str(self.body))


class InboxView(discord.ui.View):
    """未読の問い合わせを1件ずつ確認して、返信・既読化する。"""

    def __init__(self, cog: "Contact", user_id: int, rows: list):
        super().__init__(timeout=600)
        self.cog = cog
        self.user_id = user_id
        self.rows = rows
        self.index = 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("あなたの操作ではありません。", ephemeral=True)
            return False
        return True

    @property
    def current(self):
        return self.rows[self.index] if self.rows else None

    def embed(self) -> discord.Embed:
        if not self.current:
            return discord.Embed(title="📮 受信箱", description="問い合わせはありません。",
                                 color=0x95A5A6)
        e = self.cog.contact_embed(self.current)
        e.set_footer(text=f"{self.index + 1} / {len(self.rows)} 件"
                          f" ・ 状態: {self.current['status']}")
        return e

    async def _render(self, interaction: discord.Interaction):
        await interaction.edit_original_response(embed=self.embed(), view=self)

    @discord.ui.button(label="◀ 前", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.rows:
            self.index = (self.index - 1) % len(self.rows)
        await self._render(interaction)

    @discord.ui.button(label="次 ▶", style=discord.ButtonStyle.secondary)
    async def nxt(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.rows:
            self.index = (self.index + 1) % len(self.rows)
        await self._render(interaction)

    @discord.ui.button(label="返信する", style=discord.ButtonStyle.primary, emoji="✉️")
    async def reply(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.current:
            await interaction.response.send_message("返信する対象がありません。", ephemeral=True)
            return
        await interaction.response.send_modal(ReplyModal(self.cog, self.current["id"]))

    @discord.ui.button(label="既読にする", style=discord.ButtonStyle.success, emoji="✅")
    async def mark(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.current:
            await self.cog.db.set_contact_status(self.current["id"], "read")
            self.rows.pop(self.index)
            if self.rows:
                self.index %= len(self.rows)
            else:
                self.index = 0
        await self._render(interaction)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class Contact(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    def contact_embed(self, row) -> discord.Embed:
        e = discord.Embed(
            title=f"📮 [{row['kind']}] {row['subject']}",
            description=row["body"],
            color=KIND_COLOR.get(row["kind"], 0x95A5A6),
            timestamp=datetime.fromtimestamp(row["ts"], tz=timezone.utc),
        )
        e.add_field(name="送信者",
                    value=f"<@{row['user_id']}>\n`{row['user_tag']}`\nID: `{row['user_id']}`",
                    inline=True)
        e.add_field(name="サーバー",
                    value=(f"{row['guild']}\nID: `{row['guild_id']}`" if row["guild_id"] else "DM"),
                    inline=True)
        e.add_field(name="受付番号", value=f"`#{row['id']}`", inline=True)
        return e

    # ---- 送信処理 ---------------------------------------------------------
    async def submit(self, interaction: discord.Interaction, kind: str,
                     subject: str, body: str):
        await interaction.response.defer(ephemeral=True)
        uid = interaction.user.id

        last, today = await self.db.recent_contact_stats(uid, DAY)
        now = int(time.time())
        if now - last < COOLDOWN:
            await interaction.followup.send(
                f"⏳ 連続送信を防ぐため、あと **{COOLDOWN - (now - last)}秒** お待ちください。",
                ephemeral=True)
            return
        if today >= DAILY_LIMIT:
            await interaction.followup.send(
                f"本日の送信上限（{DAILY_LIMIT}件）に達しました。また明日どうぞ。", ephemeral=True)
            return

        guild = interaction.guild
        cid = await self.db.add_contact(
            uid, str(interaction.user), guild.id if guild else None,
            guild.name if guild else "", kind, subject, body,
        )

        row = await self.db.get_contact(cid)
        delivered = await self._dm_owner(row)

        note = ("運営に届きました。返信がある場合はBotからDMが届きます。"
                if delivered else
                "受け付けました（受付番号は控えられています）。")
        await interaction.followup.send(
            f"✅ お問い合わせを送信しました（受付番号 `#{cid}`）。\n{note}\n"
            f"> **[{kind}] {subject}**",
            ephemeral=True)

    async def _dm_owner(self, row) -> bool:
        """社長にDMで通知。失敗しても内容はDBに残るので False を返すだけ。"""
        try:
            owner = (self.bot.get_user(self.bot.cfg.owner_id)
                     or await self.bot.fetch_user(self.bot.cfg.owner_id))
            if owner is None:
                return False
            embed = self.contact_embed(row)
            embed.set_footer(text="/inbox で返信・既読にできます")
            await owner.send(content="📬 新しいお問い合わせが届きました。", embed=embed)
            return True
        except (discord.HTTPException, discord.Forbidden):
            return False

    async def send_reply(self, interaction: discord.Interaction, contact_id: int, body: str):
        await interaction.response.defer(ephemeral=True)
        row = await self.db.get_contact(contact_id)
        if row is None:
            await interaction.followup.send("その問い合わせは見つかりません。", ephemeral=True)
            return
        try:
            user = (self.bot.get_user(row["user_id"])
                    or await self.bot.fetch_user(row["user_id"]))
            embed = discord.Embed(
                title=f"✉️ お問い合わせへの返信（受付番号 #{row['id']}）",
                description=body, color=0x2ECC71,
            )
            embed.add_field(name="いただいた内容",
                            value=f"**[{row['kind']}] {row['subject']}**\n{row['body'][:200]}",
                            inline=False)
            embed.set_footer(text="運営より")
            await user.send(embed=embed)
        except (discord.HTTPException, discord.Forbidden, AttributeError):
            await interaction.followup.send(
                "❌ 相手にDMを送れませんでした（DMを拒否している可能性があります）。", ephemeral=True)
            return
        await self.db.set_contact_status(contact_id, "replied")
        await interaction.followup.send(
            f"✅ `#{contact_id}` に返信しました（DM送信済み・状態を replied に更新）。", ephemeral=True)

    # ---- コマンド ---------------------------------------------------------
    @app_commands.command(
        name="contact", description="運営への問い合わせ・要望を送ります（内容は運営にだけ届きます）")
    @app_commands.rename(kind="種別")
    @app_commands.describe(kind="問い合わせの種類を選んでください")
    @app_commands.choices(kind=KIND_CHOICES)
    async def contact(self, interaction: discord.Interaction, kind: app_commands.Choice[str]):
        await interaction.response.send_modal(ContactModal(self, kind.value))

    @app_commands.command(name="inbox", description="【社長用】届いた問い合わせを確認・返信します")
    @app_commands.default_permissions(administrator=True)
    @is_owner()
    @app_commands.rename(show_all="すべて表示")
    @app_commands.describe(show_all="既読・返信済みも含めて表示する")
    async def inbox(self, interaction: discord.Interaction, show_all: bool = False):
        await interaction.response.defer(ephemeral=True)
        rows = await self.db.list_contacts(None if show_all else "new", 25)
        counts = await self.db.contact_counts()
        view = InboxView(self, interaction.user.id, rows)
        embed = view.embed()
        if not rows:
            embed.description = (
                "未読の問い合わせはありません。\n"
                f"（内訳: 未読 {counts.get('new', 0)} ・ 既読 {counts.get('read', 0)} "
                f"・ 返信済み {counts.get('replied', 0)}）"
            )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @inbox.error
    async def inbox_error(self, interaction: discord.Interaction, error):
        if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
            msg = "このコマンドは社長本人専用です。"
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Contact(bot))
