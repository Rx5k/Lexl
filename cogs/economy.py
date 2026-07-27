"""経済Cog（v2）: 残高・クエスト・稼ぎ・入出金。

- /balance : リリーコイン(=よあコイン)とジェムの残高
- /quests  : デイリー(3)＋通常(1日更新)の目標達成型クエスト。毎朝7:00(JST)更新
- /work    : 無制限クエスト（クールダウン＋逓減でインフレ抑制）
- /deposit : よあコイン入金の案内
- /cashout: リリーコイン→よあコイン換金（手数料・最低額・クールダウン・準備金ガード）
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

import badges
import game
import quests
from api.yoacoin_client import YoacoinAPIError, YoacoinClient

COIN = "⚜️"   # リリーコイン（ゲーム内通貨）
GEM = "💎"    # ジェム（課金通貨）
YC = "🟡"     # よあコイン残高（換金可能なキャッシュ）

# 取引履歴の理由ラベル（ユーザーに分かりやすい日本語で表示）
_REASON_LABELS = {
    "deposit": "入金（よあコイン→リリー）",
    "withdraw": "換金（リリー→よあコイン）",
    "buygems": "ジェム購入",
    "explore": "探索",
    "tame": "手なずけ",
}


def _reason_label(reason: str) -> str:
    if reason.startswith("shop:"):
        return "ショップ購入"
    if reason.startswith("quest:"):
        return "クエスト報酬"
    if reason.startswith("daily:"):
        return "デイリー報酬"
    return _REASON_LABELS.get(reason, reason)


class QuestBoardView(discord.ui.View):
    """達成済みクエストの報酬をまとめて受け取るボタン。"""

    def __init__(self, cog: "Economy", user_id: int):
        super().__init__(timeout=120)
        self.cog = cog
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("あなたのクエストではありません。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="達成分を受け取る", style=discord.ButtonStyle.success, emoji="🎁")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        gained, claimed = await self.cog.claim_completed(self.user_id)
        embed = await self.cog.build_quest_embed(self.user_id)
        if claimed:
            await interaction.response.edit_message(embed=embed, view=self)
            await interaction.followup.send(
                f"🎁 {claimed}件のクエスト報酬 **+{gained} {COIN}** を受け取りました！",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "受け取れる達成済みクエストはありません。", ephemeral=True
            )

    @discord.ui.button(label="通常クエストをリロール", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def reroll(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 実行前に確認画面（かかるコストを提示）を挟む
        note = await self.cog.reroll_cost_preview(self.user_id)
        embed = discord.Embed(
            title="🔄 通常クエストのリロール確認",
            description=f"通常クエストの枠をすべて引き直します。\n\n**{note}**\n\nよろしいですか？",
            color=0xE67E22,
        )
        await interaction.response.edit_message(
            embed=embed, view=_RerollConfirmView(self.cog, self.user_id))


class _RerollConfirmView(discord.ui.View):
    """通常クエストのリロールの確認（コストを見せてから実行）。"""

    def __init__(self, cog: "Economy", user_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("あなたのクエストではありません。", ephemeral=True)
            return False
        return True

    async def _back_to_board(self, interaction: discord.Interaction, note: str | None):
        embed = await self.cog.build_quest_embed(self.user_id)
        await interaction.response.edit_message(embed=embed, view=QuestBoardView(self.cog, self.user_id))
        if note:
            await interaction.followup.send(note, ephemeral=True)

    @discord.ui.button(label="リロールする", style=discord.ButtonStyle.danger, emoji="🔄")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg = (await self.cog.reroll_normal_quests(self.user_id))[1]
        await self._back_to_board(interaction, msg)

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._back_to_board(interaction, None)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class ConfirmView(discord.ui.View):
    """換金/払い出しの最終確認（内容を見せてからボタンで実行）。"""

    def __init__(self, cog: "Economy", user_id: int, action: str,
                 gross: int, net: int, fee: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.user_id = user_id
        self.action = action   # "sell"=リリーコイン→よあコイン残高 / "cashout"=よあコイン残高→実換金
        self.gross = gross
        self.net = net
        self.fee = fee

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("あなたの操作ではありません。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="実行する", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await self.cog.execute_confirm(interaction, self)
        self.stop()

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="キャンセルしました。", embed=None, view=self
        )
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class Economy(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 換金の二重実行・レース対策（確認→実行の間の割り込みを直列化）
        self._withdraw_lock = asyncio.Lock()

    @property
    def db(self):
        return self.bot.db

    # ---- helpers ----------------------------------------------------------
    def _reward_text(self, r) -> str:
        """報酬の表示（例:「1,200 リリー ＋ 🍖 餌×3 ＋ 🧪 なつき薬×1」）。"""
        from cogs.shop import SHOP_ITEMS

        parts = []
        if r.coins:
            parts.append(f"{r.coins:,} リリー")
        for iid, qty in r.items:
            name = SHOP_ITEMS.get(iid, {}).get("name", iid)
            parts.append(f"{name}×{qty}")
        return " ＋ ".join(parts) if parts else "0 リリー"

    async def claim_completed(self, user_id: int) -> tuple[int, int]:
        """デイリー(period)＋通常(枠)の達成済みをすべて受け取る。(合計コイン, 件数)。"""
        gained = count = 0
        period = quests.daily_period()
        for q in quests.daily_quests_for(period):
            if await self.db.try_claim_quest(user_id, q.quest_id, period):
                await self._grant_reward(user_id, q, f"daily:{q.quest_id}")
                gained += q.reward.coins
                count += 1
        # 通常クエスト（枠ごと）
        await quests.ensure_normal_quests(self.db, user_id)
        for r in await self.db.get_user_quests(user_id):
            q = quests.quest_from(r["quest_id"], r["target"])
            if q is None or r["claimed"] or r["progress"] < q.target:
                continue
            await self.db.set_user_quest_claimed(user_id, r["slot"])
            await self._grant_reward(user_id, q, f"quest:{q.quest_id}")
            await quests.refill_slot(self.db, user_id, r["slot"])  # 補充
            gained += q.reward.coins
            count += 1
        return gained, count

    async def _grant_reward(self, user_id: int, q, reason: str) -> None:
        if q.reward.coins:
            await self.db.add_coins(user_id, q.reward.coins, reason=reason)
        for iid, qty in q.reward.items:
            await self.db.add_item(user_id, iid, qty)

    async def reroll_cost_preview(self, user_id: int) -> str:
        """次のリロールにかかるコストの説明（消費はしない）。確認画面で表示する。"""
        period = quests.daily_period()
        rr = await self.db.get_reroll(user_id)
        free_used = rr["free_used"] if (rr and rr["period"] == period) else 0
        if free_used == 0:
            return "本日の無料リロールを使用します（コスト無料）"
        if await self.db.get_item_qty(user_id, "reroll_ticket") > 0:
            return "🔄 リロール券を1枚消費します"
        return f"⚜️ {game.REROLL_COST_COINS} リリーを消費します"

    async def reroll_normal_quests(self, user_id: int) -> tuple[bool, str]:
        """通常クエストの全枠を引き直す。1日1回無料、以降はリロール券orリリー。"""
        period = quests.daily_period()
        rr = await self.db.get_reroll(user_id)
        free_used = rr["free_used"] if (rr and rr["period"] == period) else 0

        if free_used == 0:
            note = "（本日の無料リロール）"
        elif await self.db.try_consume_item(user_id, "reroll_ticket", 1):
            note = "（リロール券を1枚消費）"
        elif await self.db.try_spend_coins(user_id, game.REROLL_COST_COINS, "reroll"):
            note = f"（-{game.REROLL_COST_COINS} リリー）"
        else:
            return (False,
                    f"本日の無料リロールは使用済みです。リロール券 または {game.REROLL_COST_COINS} リリーが必要です。")

        for slot in range(quests.NORMAL_SLOTS):
            await quests.refill_slot(self.db, user_id, slot)
        await self.db.set_reroll(user_id, period, free_used + 1)
        return (True, f"🔄 通常クエストを引き直しました {note}")

    def _quest_line(self, title, diff, desc, prog, target, claimed, rtext) -> str:
        d = f"［{diff}］" if diff else ""
        if claimed:
            state = "✅ 受取済み"
        elif prog >= target:
            state = f"🎁 **達成！受取可能** → {rtext}"
        else:
            state = f"進捗 {prog}/{target} ・ 報酬 {rtext}"
        return f"{d}**{title}** — {desc}\n　{state}"

    async def build_quest_embed(self, user_id: int) -> discord.Embed:
        embed = discord.Embed(
            title="🗺️ クエストボード",
            description="達成分は「受け取る」で報酬に。通常クエストは「リロール」で引き直せます（1日1回無料）。",
            color=0x2ECC71,
        )
        period = quests.daily_period()
        daily_lines = []
        for q in quests.daily_quests_for(period):
            row = await self.db.get_quest(user_id, q.quest_id, period)
            daily_lines.append(self._quest_line(
                q.title, q.difficulty, q.desc, row["progress"] if row else 0, q.target,
                row["claimed"] if row else 0, self._reward_text(q.reward)))
        embed.add_field(name="📅 デイリー（毎朝7:00 JST更新・日替わり）",
                        value="\n".join(daily_lines), inline=False)

        await quests.ensure_normal_quests(self.db, user_id)
        normal_lines = []
        for r in await self.db.get_user_quests(user_id):
            q = quests.quest_from(r["quest_id"], r["target"])
            if q is None:
                continue
            normal_lines.append(self._quest_line(
                q.title, q.difficulty, q.desc, r["progress"], q.target,
                r["claimed"], self._reward_text(q.reward)))
        embed.add_field(name="📜 通常クエスト（難易度別・進捗継続・リロール可）",
                        value="\n".join(normal_lines) or "—", inline=False)
        return embed

    async def fire_event(self, user_id: int, event: str, amount: int = 1) -> list:
        """他Cog（探索など）から呼ぶ進捗イベント。達成したクエスト定義を返す。"""
        return await quests.record_event(self.db, user_id, event, amount)

    # ---- commands ---------------------------------------------------------
    @app_commands.command(name="balance", description="財布（リリーコイン・ジェム）を確認します")
    async def balance(self, interaction: discord.Interaction):
        uid = interaction.user.id
        bal = await self.db.get_balance(uid)
        cfg = self.bot.cfg
        cap = await self.db.withdrawable(uid)
        embed = discord.Embed(title="💰 あなたの財布", color=0xF1C40F)
        embed.add_field(name=f"{COIN} リリーコイン", value=f"**{bal.coins:,}**", inline=True)
        embed.add_field(name=f"{GEM} ジェム", value=f"**{bal.gems:,}**", inline=True)
        embed.add_field(name="💱 換金可能枠", value=f"**{cap:,}** リリー", inline=True)
        embed.add_field(
            name="🔁 通貨の流れ",
            value=(
                f"・よあコインを入金することで同額の {COIN} リリーコインを取得\n"
                f"　※入金方法は `/deposit` で確認できます\n"
                f"・`/cashout` で {COIN} リリーコインをよあコインに換金"
                f"（手数料 {cfg.withdraw_fee_bps/100:g}%・**換金は入金した分まで**）\n"
                f"・{GEM} ジェムは `shop` で購入可能"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="help", description="すべてのコマンドの説明を表示します")
    async def help_cmd(self, interaction: discord.Interaction):
        desc_by_name = {
            c.name: c.description
            for c in self.bot.tree.walk_commands()
            if isinstance(c, app_commands.Command)
        }
        # 社長用コマンド（economy/payouts/codex）は一覧に載せない。
        groups = [
            ("💰 通貨・財布", ["balance", "login", "deposit", "cashout", "history", "ranking"]),
            ("🗺️ ゲーム", ["explore", "dex", "inventory", "quests", "profile", "badges"]),
            ("🐾 生き物の操作", ["release", "merge"]),
            ("🏪 ショップ", ["shop", "buy"]),
            ("ℹ️ その他", ["help", "contact"]),
        ]
        embed = discord.Embed(
            title="📖 コマンド一覧",
            description=(
                "ゲーム内通貨は2種類：リリーコイン／ジェム。\n"
                f"よあコインを入金することでリリーコインを取得し遊ぶことができます。"
            ),
            color=0x5865F2,
        )
        for title, names in groups:
            # コマンド名はクリック可能なメンション（同期後）で表示
            lines = [f"{self.bot.cmd(n)} — {desc_by_name[n]}" for n in names if n in desc_by_name]
            if lines:
                embed.add_field(name=title, value="\n".join(lines), inline=False)
        # コマンド一覧は公開（他の人にも見える）
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="quests", description="デイリー＆通常クエストの進捗を確認・報酬受取")
    async def quests_cmd(self, interaction: discord.Interaction):
        embed = await self.build_quest_embed(interaction.user.id)
        view = QuestBoardView(self, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="history", description="最近のゲーム内での取引履歴を表示します")
    @app_commands.rename(count="件数")
    @app_commands.describe(count="表示する件数（既定10・最大20）")
    async def history(self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 20] = 10):
        rows = await self.db.recent_transactions(interaction.user.id, count)
        embed = discord.Embed(title="🧾 取引履歴", color=0x7F8C8D)
        if not rows:
            embed.description = "まだ取引がありません。"
        else:
            lines = []
            for r in rows:
                label = _reason_label(r["reason"])
                cur = {"coins": COIN, "gems": GEM, "yc": YC}.get(r["currency"], "")
                sign = "＋" if r["amount"] > 0 else "－"
                lines.append(f"{label} … {sign}{abs(r['amount']):,} {cur}")
            embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="login", description="ログインボーナスを受け取ります（1日1回・連続で増加）")
    async def login(self, interaction: discord.Interaction):
        uid = interaction.user.id
        period = quests.daily_period()
        row = await self.db.get_login(uid)
        if row and row["last_period"] == period:
            await interaction.response.send_message(
                "本日のログインボーナスは受取済みです。また明日どうぞ！", ephemeral=True)
            return
        prev = quests.daily_period(datetime.now(quests.JST) - timedelta(days=1))
        streak = (row["streak"] + 1) if (row and row["last_period"] == prev) else 1
        reward = game.login_reward(streak)
        await self.db.add_coins(uid, reward, "login")
        await self.db.set_login(uid, period, streak)
        bal = await self.db.get_balance(uid)
        embed = discord.Embed(
            title=f"📅 ログインボーナス +{reward} リリー",
            description=f"連続 **{streak}** 日目！ 毎日受け取ると増えていきます。",
            color=0x2ECC71,
        )
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text="毎朝7:00(JST)にリセット")
        # ログインは公開（連続日数を見せ合えると楽しい）。残高は伏せる。
        await interaction.response.send_message(embed=embed)
        await badges.notify(interaction, await badges.sync(self.db, uid))

    @app_commands.command(name="ranking", description="分野別のランキングを表示します")
    @app_commands.rename(category="分野")
    @app_commands.describe(category="ランキングの分野")
    @app_commands.choices(category=[
        app_commands.Choice(name="⚜️ リリーコイン所持", value="coins"),
        app_commands.Choice(name="📖 図鑑の収集数", value="species"),
        app_commands.Choice(name="🐾 生き物の数", value="creatures"),
        app_commands.Choice(name="🎖️ バッジ数", value="badges"),
    ])
    async def ranking(self, interaction: discord.Interaction,
                      category: app_commands.Choice[str] | None = None):
        cat = category.value if category else "coins"
        meta = {
            "coins": ("⚜️ リリーコイン所持ランキング", self.db.top_coins, "リリー"),
            "species": ("📖 図鑑 収集数ランキング", self.db.top_species, "種"),
            "creatures": ("🐾 生き物の数ランキング", self.db.top_creatures, "体"),
            "badges": ("🎖️ バッジ数ランキング", self.db.top_badges, "個"),
        }[cat]
        title, fn, unit = meta
        rows = await fn(10)
        lines = []
        for i, r in enumerate(rows, 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"**{i}.**")
            lines.append(f"{medal} <@{r['user_id']}> — **{r['v']:,}** {unit}")
        embed = discord.Embed(title=title, description="\n".join(lines) or "まだ誰もいません。",
                              color=0xF1C40F)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="deposit", description="よあコインの入金方法を表示します")
    async def deposit(self, interaction: discord.Interaction):
        company = self.bot.cfg.company_name
        embed = discord.Embed(
            title="📥 よあコインの入金（→ リリーコイン）",
            description=(
                "以下のコマンドで**よあコインをこの会社へ送金**すると、"
                "自動で検知して**同額のリリーコイン**が反映されます（1:1・手数料なし）。\n\n"
                f"```\ny!支払 {company} <額>\n```\n"
                f"例：1000リリーコイン欲しいときは `y!支払 {company} 1000`\n"
                "反映まで数秒。反映後は探索・手なずけ・ショップで使えます。"
            ),
            color=0x1ABC9C,
        )
        embed.set_footer(text="リリーコインは /cashout でよあコインに戻せます（手数料あり）")
        # 入金方法の案内は公開（他の人にも見える）
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="cashout", description="リリーコインをよあコインに換金します（手数料あり）")
    @app_commands.rename(amount="額")
    @app_commands.describe(amount="換金するリリーの額（必須・ミス防止のため必ず指定）")
    async def cashout(
        self, interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 10_000_000],
    ):
        cfg = self.bot.cfg
        uid = interaction.user.id
        now = int(time.time())
        bal = await self.db.get_balance(uid)
        gross = amount

        if bal.coins < gross:
            await interaction.response.send_message(
                f"リリーコインが足りません（所持 {bal.coins:,} {COIN}）。", ephemeral=True
            )
            return
        # 換金上限＝入金累計（無料リリーは換金不可・farmer対策・会社が損しない）
        cap = await self.db.withdrawable(uid)
        if gross > cap:
            await interaction.response.send_message(
                f"換金できるのは**入金した分まで**です。換金可能枠は **{cap:,} リリー**。\n"
                f"（ログインやクエストで得た無料リリーはゲーム内で使えます）",
                ephemeral=True,
            )
            return
        if gross < cfg.min_withdraw:
            await interaction.response.send_message(
                f"最低換金額は {cfg.min_withdraw} {COIN} です。", ephemeral=True
            )
            return
        last = await self.db.last_withdraw_at(uid)
        if now - last < cfg.withdraw_cooldown:
            await interaction.response.send_message(
                f"⏳ 換金クールダウン中です。あと {cfg.withdraw_cooldown - (now - last)}秒。",
                ephemeral=True,
            )
            return

        net, fee = game.withdraw_split(gross, cfg.withdraw_fee_bps)
        embed = discord.Embed(
            title="💱 換金の確認（リリーコイン → よあコイン）",
            description="内容を確認して「実行する」を押してください。",
            color=0xE67E22,
        )
        embed.add_field(name="換金リリーコイン", value=f"{gross:,} {COIN}", inline=True)
        embed.add_field(name=f"手数料（{cfg.withdraw_fee_bps/100:g}%）", value=f"-{fee:,} {COIN}", inline=True)
        embed.add_field(name="受取よあコイン", value=f"**{net:,}** {YC}", inline=True)
        embed.set_footer(text=f"換金後の残高: {bal.coins - gross:,} リリー")
        view = ConfirmView(self, uid, "withdraw", gross, net, fee)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ---- 確認ボタンからの実行（リリーコイン → よあコイン） ----------------------
    async def execute_confirm(self, interaction: discord.Interaction, view: "ConfirmView"):
        cfg = self.bot.cfg
        uid = view.user_id
        now = int(time.time())
        gross, net, fee = view.gross, view.net, view.fee
        await interaction.response.defer()

        # 二重確認パネル・レースでも「換金gross > 入金累計」を絶対に許さないよう直列化して再検証。
        async with self._withdraw_lock:
            last = await self.db.last_withdraw_at(uid)
            if now - last < cfg.withdraw_cooldown:
                await interaction.edit_original_response(
                    content=f"⏳ 換金クールダウン中です。あと {cfg.withdraw_cooldown - (now - last)}秒。",
                    embed=None, view=view,
                )
                return

            # 実行時点の残高・換金枠を再チェック（パネル表示後に変動している可能性）
            bal = await self.db.get_balance(uid)
            if bal.coins < gross:
                await interaction.edit_original_response(
                    content=f"リリーコインが足りません（所持 {bal.coins:,} {COIN}）。", embed=None, view=view)
                return
            cap = await self.db.withdrawable(uid)
            if gross > cap:
                await interaction.edit_original_response(
                    content=f"換金できるのは入金した分までです。換金可能枠は **{cap:,} リリー**。",
                    embed=None, view=view)
                return

            if cfg.payout_enabled:
                reserve = await self.db.get_reserve()
                if reserve - net < cfg.reserve_floor:
                    await interaction.edit_original_response(
                        content="現在、会社の準備金が不足しているため換金できません。時間をおいて再試行してください。",
                        embed=None, view=view,
                    )
                    return
                # 送金（冪等キー付き）→ 成功後に台帳を確定。ロック保持中なので
                # 残高・準備金は検証時から変化せず、台帳確定が失敗することはない。
                try:
                    async with YoacoinClient(cfg.base_url, cfg.api_key) as client:
                        await client.payout(uid, net, idempotency_key=str(uuid.uuid4()), kind="withdraw")
                except YoacoinAPIError as e:
                    await interaction.edit_original_response(
                        content=f"払い出しAPIエラー: {e}", embed=None, view=view
                    )
                    return
                ok = await self.db.withdraw_to_yoacoin(uid, gross, net, fee, cfg.reserve_floor, queue_only=False)
                if not ok:
                    await interaction.edit_original_response(
                        content="換金に失敗しました（残高または準備金）。", embed=None, view=view
                    )
                    return
                msg = f"✅ リリーコイン {gross:,} {COIN} を換金し、**{net:,} よあコイン**を送金しました（手数料 {fee:,} {COIN}）。"
            else:
                ok = await self.db.withdraw_to_yoacoin(uid, gross, net, fee, cfg.reserve_floor, queue_only=True)
                if not ok:
                    await interaction.edit_original_response(
                        content="リリーコイン残高が不足しています。", embed=None, view=view
                    )
                    return
                msg = (
                    f"🧾 換金申請を受け付けました：リリーコイン {gross:,} {COIN} → **{net:,} よあコイン**"
                    f"（手数料 {fee:,} {COIN}）。\n自動送金は準備中のため、運営が確認後に `y!` で送金します。"
                )

        bal = await self.db.get_balance(uid)
        embed = discord.Embed(title="📤 換金", description=msg, color=0x2ECC71)
        embed.set_footer(text=f"残高: {bal.coins:,} リリー")
        await interaction.edit_original_response(content=None, embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))
