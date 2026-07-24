"""ショップCog: `/buy` でアイテム・ジェムを購入する。

- 通常アイテム（餌・なつき薬）はリリーコイン建て（消費＝会社のシンク）。
- 限定系（金の餌・限定探索チケット）はジェム建て。
- ジェムの購入は**ショップの `/buy` からのみ**（単体コマンドは廃止）。
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

COIN = "⚜️"   # リリーコイン（ゲーム内通貨）
GEM = "💎"    # ジェム（課金通貨）

SHOP_ITEMS: dict[str, dict] = {
    "bait": {
        "name": "🍖 餌",
        "desc": "探索の遭遇率アップ（+20%）。`/explore use_bait:true` で使用。",
        "price_coins": 200,
        "price_gems": 0,
    },
    "charm": {
        "name": "🧪 なつき薬",
        "desc": "手なずけの成功率アップ（+20%）。遭遇後のボタンで使用。",
        "price_coins": 600,
        "price_gems": 0,
    },
    "gold_bait": {
        "name": "✨ 金の餌",
        "desc": "探索の遭遇率が大アップ（+40%）。`/explore use_bait:true` で優先使用。",
        "price_coins": 0,
        "price_gems": 2,
    },
    "limited_ticket": {
        "name": "🌟 限定探索チケット",
        "desc": "限定個体を探せる（遭遇確定）。`/explore premium:true` で使用。",
        "price_coins": 0,
        "price_gems": 5,
    },
    "area_ticket": {
        "name": "🗺️ エリア解放チケット",
        "desc": "ロック中のエリア（洞窟/空など）を1つ解放。`/explore` でそのエリアを選ぶと自動使用。",
        "price_coins": 0,
        "price_gems": 8,
    },
    "iv_reroll": {
        "name": "🎲 個体値リロール薬",
        "desc": "手持ち1体の個体値(IV)を振り直し（厳選）。`/inventory` から使用。",
        "price_coins": 0,
        "price_gems": 3,
    },
    "reroll_ticket": {
        "name": "🔄 クエストリロール券",
        "desc": "通常クエストを追加で引き直せる（現金リロールより割安）。`/quests` のリロールで自動使用。",
        "price_coins": 400,
        "price_gems": 0,
    },
    "name_tag": {
        "name": "🏷️ なまえ札",
        "desc": "生き物に名前を付けられる。`/inventory` から使用。",
        "price_coins": 300,
        "price_gems": 0,
    },
    "cap_expansion": {
        "name": "📦 インベントリ拡張(+10)",
        "desc": "生き物の保有上限を+10。購入時に自動適用。",
        "price_coins": 0,
        "price_gems": 4,
    },
}

CAP_EXPANSION_VALUE = "cap_expansion"

# 各商品の使用方法（ショップ表示用）
USE_TEXT = {
    "bait": "`/explore 餌を使う:true` で消費",
    "charm": "遭遇後の「なつき薬を使う」ボタンで消費",
    "gold_bait": "`/explore 餌を使う:true` で優先消費",
    "limited_ticket": "`/explore 限定探索:true` で消費",
    "area_ticket": "`/explore` でロック中エリアを選ぶと自動消費",
    "iv_reroll": "`/inventory` で生き物を選び「アイテムを使う」で使用",
    "reroll_ticket": "`/quests` のリロールで自動消費",
    "name_tag": "`/inventory` で生き物を選び「アイテムを使う」で使用",
    "cap_expansion": "購入時に自動で保有上限 +10",
}

GEMS_CHOICE_VALUE = "gems"

# /buy の選択肢は「日本語の商品名」で選べるようにする（value は内部ID）。
ITEM_CHOICES = (
    [app_commands.Choice(name=v["name"], value=k) for k, v in SHOP_ITEMS.items()]
    + [app_commands.Choice(name="💎 ジェム（課金通貨）", value=GEMS_CHOICE_VALUE)]
)


def price_label(it: dict) -> str:
    return f"{it['price_gems']} {GEM}" if it["price_gems"] else f"{it['price_coins']} {COIN}"


class BuyConfirmView(discord.ui.View):
    """購入の最終確認パネル。"""

    def __init__(self, cog: "Shop", user_id: int, value: str, quantity: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.user_id = user_id
        self.value = value
        self.quantity = quantity

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("あなたの操作ではありません。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="購入する", style=discord.ButtonStyle.success, emoji="🛒")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await self.cog.execute_buy(interaction, self)
        self.stop()

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="購入をキャンセルしました。", embed=None, view=self)
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class Shop(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    @app_commands.command(name="shop", description="ショップの品揃えと使い方を表示します")
    async def shop(self, interaction: discord.Interaction):
        cfg = self.bot.cfg
        bal = await self.db.get_balance(interaction.user.id)
        embed = discord.Embed(
            title="🏪 ショップ",
            description=f"{self.bot.cmd('buy')} で商品を選んで購入できます（数量は任意）。",
            color=0xF39C12,
        )
        # ジェム（課金通貨）はショップから購入
        embed.add_field(
            name=f"💎 ジェム — {cfg.gem_price_coins} {COIN} / 個",
            value="限定探索チケットや金の餌の購入に使います。",
            inline=False,
        )
        for iid, it in SHOP_ITEMS.items():
            use = USE_TEXT.get(iid, "")
            value = it["desc"] + (f"\n使い方: {use}" if use else "")
            embed.add_field(name=f"{it['name']} — {price_label(it)}", value=value, inline=False)
        embed.set_footer(text=f"所持: {bal.coins:,} リリー ・ {bal.gems:,} ジェム")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="buy", description="商品を選んで購入します")
    @app_commands.rename(item="商品", quantity="数量")
    @app_commands.describe(item="購入する商品", quantity="購入する数量（任意・既定は1）")
    @app_commands.choices(item=ITEM_CHOICES)
    async def buy(
        self,
        interaction: discord.Interaction,
        item: app_commands.Choice[str],
        quantity: app_commands.Range[int, 1, 99] = 1,
    ):
        cfg = self.bot.cfg
        # 確認パネルを表示（実際の購入はボタンで実行）
        if item.value == GEMS_CHOICE_VALUE:
            cost = quantity * cfg.gem_price_coins
            name = f"💎 ジェム × {quantity:,}"
            spent = f"{cost:,} {COIN}"
        else:
            it = SHOP_ITEMS[item.value]
            if it["price_gems"]:
                spent = f"{it['price_gems'] * quantity:,} {GEM}"
            else:
                spent = f"{it['price_coins'] * quantity:,} {COIN}"
            name = f"{it['name']} × {quantity:,}"

        embed = discord.Embed(
            title="🛒 購入の確認",
            description=f"**{name}** を **{spent}** で購入しますか？",
            color=0xF39C12,
        )
        bal = await self.db.get_balance(interaction.user.id)
        embed.set_footer(text=f"所持: {bal.coins:,} リリー ・ {bal.gems:,} ジェム")
        view = BuyConfirmView(self, interaction.user.id, item.value, quantity)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def execute_buy(self, interaction: discord.Interaction, view: "BuyConfirmView"):
        """確認ボタンからの実際の購入処理（履歴は transactions に記録される）。"""
        cfg = self.bot.cfg
        uid = view.user_id
        value, quantity = view.value, view.quantity
        await interaction.response.defer()

        # ジェム購入
        if value == GEMS_CHOICE_VALUE:
            cost = quantity * cfg.gem_price_coins
            if not await self.db.buy_gems(uid, quantity, cfg.gem_price_coins):
                bal = await self.db.get_balance(uid)
                await interaction.edit_original_response(
                    content=f"リリーコインが足りません（必要 {cost:,} {COIN} / 所持 {bal.coins:,} {COIN}）。",
                    embed=None, view=view,
                )
                return
            desc = f"💎 ジェム × {quantity:,} を購入（-{cost:,} {COIN}）。"
        else:
            it = SHOP_ITEMS[value]
            if it["price_gems"]:
                total = it["price_gems"] * quantity
                ok = await self.db.try_spend_gems(uid, total, reason=f"shop:{value}")
                spent = f"{total:,} {GEM}"
                need_msg = f"ジェムが足りません（必要 {spent}）。"
            else:
                total = it["price_coins"] * quantity
                ok = await self.db.try_spend_coins(uid, total, reason=f"shop:{value}")
                spent = f"{total:,} {COIN}"
                need_msg = f"リリーコインが足りません（必要 {spent}）。"
            if not ok:
                await interaction.edit_original_response(content=need_msg, embed=None, view=view)
                return
            if value == CAP_EXPANSION_VALUE:
                # アイテムではなく保有上限を直接拡張
                import game
                new_cap = await self.db.add_creature_cap(uid, game.CAP_EXPANSION_STEP * quantity)
                desc = f"{it['name']} × {quantity:,} を購入（-{spent}）。保有上限が **{new_cap}** になりました。"
            else:
                await self.db.add_item(uid, value, quantity)
                desc = f"{it['name']} × {quantity:,} を購入（-{spent}）。"

        bal = await self.db.get_balance(uid)
        embed = discord.Embed(title="🛍️ 購入完了！", description=desc, color=0x2ECC71)
        embed.set_footer(text=f"所持: {bal.coins:,} リリー ・ {bal.gems:,} ジェム")
        await interaction.edit_original_response(content=None, embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(Shop(bot))
