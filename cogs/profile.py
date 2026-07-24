"""プロフィールCog: ユーザーの総合ステータスを1画面で表示。"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import game
from data import creatures

COIN = "⚜️"
GEM = "💎"


class Profile(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    @app_commands.command(name="profile", description="あなた（または指定ユーザー）のプロフィールを表示します")
    @app_commands.rename(user="ユーザー")
    @app_commands.describe(user="他の人のプロフィールを見る場合に指定")
    async def profile(self, interaction: discord.Interaction, user: discord.User | None = None):
        target = user or interaction.user
        uid = target.id

        bal = await self.db.get_balance(uid)
        rows = await self.db.list_creatures(uid)
        owned = await self.db.distinct_species(uid)
        owned_normal = sum(1 for sp in creatures.NORMAL_SPECIES if sp.species_id in owned)

        # 最高個体値の相棒を探す
        best = None
        best_pct = -1.0
        for row in rows:
            pct = game.iv_percent(row["iv_hp"], row["iv_atk"], row["iv_def"])
            if pct > best_pct:
                best_pct = pct
                best = row

        embed = discord.Embed(title=f"👤 {target.display_name} のプロフィール", color=0x5865F2)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name=f"{COIN} リリーコイン", value=f"{bal.coins:,}", inline=True)
        embed.add_field(name=f"{GEM} ジェム", value=f"{bal.gems:,}", inline=True)
        embed.add_field(
            name="📖 図鑑",
            value=f"{owned_normal} / {creatures.TOTAL_SPECIES} 種",
            inline=True,
        )
        embed.add_field(name="🎒 手なずけ数", value=f"{len(rows)} 体", inline=True)

        if best is not None:
            sp = creatures.get(best["species_id"])
            if sp is not None:
                pw = game.power(sp, best["iv_hp"], best["iv_atk"], best["iv_def"])
                embed.add_field(
                    name="⭐ 自慢の相棒",
                    value=f"{sp.element_info[1]}{sp.rarity_info.emoji} {sp.name}"
                          f"（IV {best_pct:.0f}%・{game.iv_grade(best_pct)}・総合力 {pw}）",
                    inline=True,
                )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Profile(bot))
