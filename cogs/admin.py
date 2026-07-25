"""管理Cog: 経済ダッシュボード & 換金申請の確認/処理（社長・管理者専用）。

- /economy : よあコイン建て経済ダッシュボード（自動送金の状態・未処理申請も表示）
- /payouts : 未処理の換金申請（withdraw_requests テーブル）を一覧＆一括処理
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

import quests
from api.yoacoin_client import YoacoinAPIError, YoacoinClient


def _today_boundary_ts() -> int:
    """今日の7:00(JST)境界の UNIX 秒。"""
    now = datetime.now(quests.JST)
    boundary = now.replace(hour=quests.RESET_HOUR, minute=0, second=0, microsecond=0)
    if now.hour < quests.RESET_HOUR:
        boundary -= timedelta(days=1)
    return int(boundary.timestamp())

COIN = "⚜️"
GEM = "💎"
TARGET_EQUITY = 50000  # 目標純資産（よあコイン）


def is_owner():
    """社長（config.owner_id）本人のみ許可するチェック。"""
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id == interaction.client.cfg.owner_id
    return app_commands.check(predicate)


class PayoutQueueView(discord.ui.View):
    """未処理の換金申請を /payout で一括処理するボタン。"""

    def __init__(self, cog: "Admin", user_id: int):
        super().__init__(timeout=120)
        self.cog = cog
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("あなたの操作ではありません。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="自動送金で一括処理する", style=discord.ButtonStyle.success, emoji="📤")
    async def process_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await self.cog.process_pending(interaction, self)
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    # ---- /economy ---------------------------------------------------------
    @app_commands.command(name="economy", description="【社長用】よあコイン建て経済ダッシュボード")
    @app_commands.default_permissions(administrator=True)
    @is_owner()
    async def economy(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cfg = self.bot.cfg
        s = await self.db.economy_summary(TARGET_EQUITY)
        pending_n, pending_total = await self.db.pending_withdraw_summary()
        daily = await self.db.net_since(_today_boundary_ts())

        equity = s["equity"]
        remaining = s["goal_remaining"]
        if remaining == 0:
            health = "🎉 目標達成！純資産が 50,000 に到達"
        elif equity >= cfg.company_start_reserve:
            health = "🟢 健全（純資産が初期資本以上）"
        else:
            health = "🟡 純資産が初期資本を下回り中（faucet過多 or 出金過多）"

        payout_status = "🟢 有効（/cashout は自動送金）" if cfg.payout_enabled \
            else "🔴 無効（換金は申請キューに保存）"

        embed = discord.Embed(title="📊 会社経済ダッシュボード（よあコイン建て）", color=0x34495E)
        embed.add_field(name="🏦 準備金 (Reserve)", value=f"{s['reserve']:,}", inline=True)
        embed.add_field(name="📉 負債 (発行済みリリーコイン)", value=f"{s['liabilities']:,}", inline=True)
        embed.add_field(name="💠 純資産 (Equity)", value=f"**{equity:,}**", inline=True)
        embed.add_field(name="🎯 50,000までの残り", value=f"{remaining:,}", inline=False)
        embed.add_field(name="内訳: 利益源 (sink)",
                        value=(f"💎 ジェム売上: **+{s['gem_sales']:,}**\n"
                               f"🎮 ゲーム消費: **+{s['game_sink']:,}**\n"
                               f"🧾 換金手数料: **+{s['fees']:,}**"),
                        inline=True)
        embed.add_field(name="内訳: 配布 (faucet)",
                        value=(f"🗺️ クエスト: −{s['faucet_quest']:,}\n"
                               f"📅 ログイン: −{s['faucet_login']:,}\n"
                               f"🏅 マイルストーン: −{s['faucet_milestone']:,}\n"
                               f"🕊️ 逃がす還元: −{s['faucet_release']:,}"),
                        inline=True)
        net = daily["net"]
        net_icon = "🟢" if net >= 0 else "🔴"
        embed.add_field(
            name="📈 本日の純増（7:00〜）",
            value=(f"{net_icon} **{net:+,}** リリー相当"
                   f"（消費+{daily['game_sink']:,} ジェム+{daily['gem_sales']:,} "
                   f"手数料+{daily['fees']:,} − 配布{daily['faucet']:,}）"),
            inline=False,
        )
        gross_withdrawn = s["payouts"] + s["fees"]
        cap_remaining = s["deposits"] - gross_withdrawn
        embed.add_field(
            name="🛡️ 換金枠（会社が損しない保証）",
            value=(f"📥 入金累計: +{s['deposits']:,} ・ 換金gross累計: −{gross_withdrawn:,}\n"
                   f"残り換金可能枠（全体）: **{cap_remaining:,}**（入金≥換金gross なら準備金は減らない）"),
            inline=False,
        )
        embed.add_field(name="🤖 自動送金 (PAYOUT_ENABLED)", value=payout_status, inline=False)
        embed.add_field(name="🧾 未処理の換金申請",
                        value=(f"{pending_n:,} 件 ・ 合計 {pending_total:,} よあコイン"
                               + ("　→ `/payouts` で処理" if pending_n else "")),
                        inline=False)
        embed.add_field(name="登録ユーザー / 状態", value=f"{s['users']:,} 人 ・ {health}", inline=False)

        try:
            async with YoacoinClient(cfg.base_url, cfg.api_key) as client:
                info = await client.company()
            embed.add_field(name="🏢 API: 自社情報", value=f"```{info}```", inline=False)
        except YoacoinAPIError as e:
            embed.add_field(name="🏢 API: 自社情報", value=f"取得失敗: {e}", inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---- /payouts ---------------------------------------------------------
    @app_commands.command(name="payouts", description="【社長用】未処理の換金申請を確認・処理します")
    @app_commands.default_permissions(administrator=True)
    @is_owner()
    async def payouts(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        reqs = await self.db.list_withdraw_requests("pending", 20)
        n, total = await self.db.pending_withdraw_summary()

        embed = discord.Embed(
            title="🧾 未処理の換金申請",
            description=(
                f"保存先: SQLite `withdraw_requests` テーブル（status='pending'）\n"
                f"件数: **{n:,}** ・ 払い出し合計: **{total:,} よあコイン**\n"
                f"自動送金: {'🟢 有効' if self.bot.cfg.payout_enabled else '🔴 無効（PAYOUT_ENABLED=false）'}"
            ),
            color=0xE67E22,
        )
        if reqs:
            lines = [
                f"#{r['id']} ・ <@{r['user_id']}> ・ {r['net_payout']:,} よあコイン（手数料 {r['fee']:,}）"
                for r in reqs
            ]
            embed.add_field(name="一覧（先頭20件）", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="一覧", value="未処理の申請はありません。", inline=False)

        view = discord.utils.MISSING  # view=None は discord.py が受け付けない
        if reqs and self.bot.cfg.payout_enabled:
            view = PayoutQueueView(self, interaction.user.id)
        elif reqs and not self.bot.cfg.payout_enabled:
            embed.set_footer(text="自動処理するには PAYOUT_ENABLED=true にして再起動してください。")

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def process_pending(self, interaction: discord.Interaction, view: "PayoutQueueView"):
        cfg = self.bot.cfg
        await interaction.response.defer()
        if not cfg.payout_enabled:
            await interaction.edit_original_response(
                content="自動送金が無効です（PAYOUT_ENABLED=false）。", embed=None, view=view
            )
            return

        reqs = await self.db.list_withdraw_requests("pending", 50)
        paid = 0
        paid_total = 0
        failed = 0
        errors: list[str] = []
        async with YoacoinClient(cfg.base_url, cfg.api_key) as client:
            for r in reqs:
                net = r["net_payout"]
                # 準備金フロアを先に確認（送金前に）
                if (await self.db.get_reserve()) - net < cfg.reserve_floor:
                    failed += 1
                    errors.append(f"#{r['id']}: 準備金不足")
                    continue
                try:
                    await client.payout(r["user_id"], net,
                                        idempotency_key=f"req-{r['id']}", kind="withdraw")
                except YoacoinAPIError as e:
                    failed += 1
                    errors.append(f"#{r['id']}: APIエラー {e}")
                    continue
                if await self.db.mark_withdraw_paid(r["id"], cfg.reserve_floor):
                    paid += 1
                    paid_total += net
                else:
                    failed += 1
                    errors.append(f"#{r['id']}: 記録更新に失敗（送金済みの可能性）")

        embed = discord.Embed(title="📤 換金申請の処理結果", color=0x2ECC71)
        embed.add_field(name="送金完了", value=f"{paid:,} 件 ・ 合計 {paid_total:,} よあコイン", inline=False)
        if failed:
            embed.color = 0xE74C3C
            embed.add_field(name=f"失敗 {failed:,} 件",
                            value="\n".join(errors[:10]) or "—", inline=False)
        n, total = await self.db.pending_withdraw_summary()
        embed.set_footer(text=f"残り未処理: {n:,} 件 ・ {total:,} よあコイン")
        await interaction.edit_original_response(content=None, embed=embed, view=view)

    # ---- 権限エラー -------------------------------------------------------
    @economy.error
    @payouts.error
    async def admin_error(self, interaction: discord.Interaction, error):
        if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
            msg = "このコマンドは社長本人専用です。"
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
