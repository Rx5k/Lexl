"""コレクションCog（v2）: 探索・手なずけ・図鑑・所持一覧。

コアループ:
  /explore（リリーコイン消費・空振りあり）→ 遭遇 → [手なずける]（リリーコイン消費・確率成功）
  → 成功で個体値(IV)付きの生き物をコレクションに追加 → /dex で図鑑を埋める。

探索/手なずけ/購入はリリーコインを消すだけ＝会社のシンク。
探索/遭遇/手なずけ成功は quests の進捗イベントを発火する。
限定個体は「限定探索チケット」(ジェムで購入) を使う /explore premium:true でのみ出現。
"""
from __future__ import annotations

import time

import discord
from discord import app_commands
from discord.ext import commands

import badges
import game
import quests
from data import creatures

COIN = "⚜️"
GEM = "💎"

BAIT_ENCOUNTER_BONUS = 0.20
GOLD_BAIT_ENCOUNTER_BONUS = 0.40
CHARM_TAME_BONUS = 0.20

LIMITED_TICKET = "limited_ticket"
AREA_TICKET = "area_ticket"

# /inventory の「アイテムを使う」で生き物に使えるアイテム
USABLE_ITEMS = {
    "iv_reroll": {"label": "個体値リロール薬", "emoji": "🎲", "desc": "個体値をランダムに振り直す"},
    "name_tag":  {"label": "なまえ札",       "emoji": "🏷️", "desc": "生き物に名前を付ける"},
}

HABITAT_CHOICES = [
    app_commands.Choice(name=f"{h.emoji} {h.name}", value=k)
    for k, h in creatures.HABITATS.items()
]

_NICK_BAD = set("@`*_~|<>\n\r\t")


def _clean_nickname(s: str) -> str:
    """あだ名のサニタイズ（メンション/マークダウン/改行を除去・20文字制限）。"""
    return "".join(c for c in (s or "") if c not in _NICK_BAD).strip()[:20]


def creature_embed(sp: creatures.Species, *, title: str, color: int) -> discord.Embed:
    r = sp.rarity_info
    embed = discord.Embed(title=title, description=sp.flavor, color=color)
    tag = "🌟限定 " if sp.limited else ""
    embed.add_field(name="種族", value=f"{tag}{r.emoji} **{sp.name}**（{r.label}）", inline=False)
    embed.add_field(name="基礎ステータス",
                    value=f"HP {sp.base_hp} / ATK {sp.base_atk} / DEF {sp.base_def}",
                    inline=False)
    return embed


def creature_detail_embed(row) -> discord.Embed:
    """1個体の詳細（実効ステータス・属性・個体値ランク）を1画面に。"""
    sp = creatures.get(row["species_id"])
    ivh, iva, ivd = row["iv_hp"], row["iv_atk"], row["iv_def"]
    pct = game.iv_percent(ivh, iva, ivd)
    grade = game.iv_grade(pct)
    eh, ea, ed = game.effective_stats(sp, ivh, iva, ivd)
    total = eh + ea + ed
    el_name, el_emoji = sp.element_info
    nick = f"「{row['nickname']}」" if row["nickname"] else ""
    tag = "🌟 " if sp.limited else ""
    embed = discord.Embed(
        title=f"{tag}{sp.rarity_info.emoji} {sp.name}{nick}",
        description=f"*{sp.flavor}*",
        color=0xE91E63,
    )
    embed.add_field(name="No.", value=f"#{sp.dex_no:03d}", inline=True)
    embed.add_field(name="属性", value=f"{el_emoji} {el_name}", inline=True)
    embed.add_field(name="レア度", value=f"{sp.rarity_info.emoji} {sp.rarity_info.label}", inline=True)
    embed.add_field(
        name="⚔️ 実効ステータス",
        value=(f"❤️ HP  **{eh}** （{sp.base_hp}+{ivh}）\n"
               f"⚔️ ATK **{ea}** （{sp.base_atk}+{iva}）\n"
               f"🛡️ DEF **{ed}** （{sp.base_def}+{ivd}）\n"
               f"💪 総合力 **{total}**"),
        inline=False,
    )
    embed.add_field(
        name="✨ 個体値 (IV)",
        value=(f"`{game.progress_bar(ivh + iva + ivd, game.IV_MAX * 3)}` "
               f"**{pct:.0f}%**　ランク **{grade}**\n"
               f"HP {ivh}/{game.IV_MAX} ・ ATK {iva}/{game.IV_MAX} ・ DEF {ivd}/{game.IV_MAX}"),
        inline=False,
    )
    embed.set_footer(text=f"生息: {sp.habitat_info.emoji} {sp.habitat_info.name} ・ 個体番号 #{row['instance_id']}")
    return embed


async def notify_quests(interaction: discord.Interaction, completed: list) -> None:
    """達成したクエストがあれば控えめに通知。"""
    if not completed:
        return
    names = "・".join(q.title for q in completed)
    try:
        await interaction.followup.send(
            f"🗺️ クエスト達成: **{names}**！ `/quests` で報酬を受け取ろう。", ephemeral=True
        )
    except discord.HTTPException:
        pass


class TameView(discord.ui.View):
    def __init__(self, cog: "Collection", user_id: int, sp: creatures.Species, has_charm: bool):
        super().__init__(timeout=120)
        self.cog = cog
        self.user_id = user_id
        self.sp = sp
        self.done = False
        if not has_charm:
            self.remove_item(self.use_charm)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("これはあなたの遭遇ではありません。", ephemeral=True)
            return False
        return True

    async def _attempt(self, interaction: discord.Interaction, use_charm: bool):
        if self.done:
            return
        db = self.cog.db
        uid = self.user_id
        cost = game.tame_cost(self.sp)

        # インベントリ上限チェック（満杯なら手なずけ不可）
        if await db.creature_count(uid) >= await db.get_creature_cap(uid):
            await interaction.response.send_message(
                "🎒 インベントリが満杯です。`/release` で逃がすか、`/shop` の「インベントリ拡張」で枠を増やしてください。",
                ephemeral=True,
            )
            return

        bonus = 0.0
        if use_charm:
            if not await db.try_consume_item(uid, "charm", 1):
                await interaction.response.send_message("なつき薬を持っていません。", ephemeral=True)
                return
            bonus = CHARM_TAME_BONUS

        if not await db.try_spend_coins(uid, cost, reason="tame"):
            bal = await db.get_balance(uid)
            await interaction.response.send_message(
                f"リリーコインが足りません（必要: {cost} {COIN} / 所持: {bal.coins} {COIN}）。", ephemeral=True
            )
            return

        success = game.try_tame(self.sp, bonus)
        rate = int(game.tame_success_rate(self.sp, bonus) * 100)

        if success:
            self.done = True
            ivh, iva, ivd = game.roll_ivs()
            await db.add_creature(uid, self.sp.species_id, ivh, iva, ivd)
            await db.bump_stat(uid, "tames")
            pct = game.iv_percent(ivh, iva, ivd)
            bal = await db.get_balance(uid)
            embed = creature_embed(self.sp, title=f"🎉 {self.sp.name} を手なずけた！", color=0x2ECC71)
            embed.add_field(name="個体値 (IV)",
                            value=f"HP {ivh} / ATK {iva} / DEF {ivd}　→ **{pct:.1f}%**", inline=False)
            embed.set_footer(text=f"消費 {cost} リリー（成功率 {rate}%）・ 残高 {bal.coins:,} リリー")
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(embed=embed, view=self)
            completed = await quests.record_event(db, uid, "tame_success")
            await notify_quests(interaction, completed)
            await badges.notify(interaction, await badges.sync(db, uid))
            self.stop()
        else:
            bal = await db.get_balance(uid)
            embed = creature_embed(self.sp, title=f"💨 {self.sp.name} は警戒している…", color=0xE67E22)
            embed.add_field(name="結果",
                            value=f"手なずけ失敗（成功率 {rate}%）。もう一度試せます。", inline=False)
            embed.set_footer(text=f"消費 {cost} リリー ・ 残高 {bal.coins:,} リリー")
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="手なずける", style=discord.ButtonStyle.primary, emoji="🤝")
    async def tame(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._attempt(interaction, use_charm=False)

    @discord.ui.button(label="なつき薬を使う（成功率UP）", style=discord.ButtonStyle.success, emoji="🧪")
    async def use_charm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._attempt(interaction, use_charm=True)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class DexClaimView(discord.ui.View):
    """図鑑マイルストーン報酬を受け取るボタン。"""

    def __init__(self, cog: "Collection", user_id: int):
        super().__init__(timeout=120)
        self.cog = cog
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("あなたの図鑑ではありません。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="マイルストーン報酬を受け取る", style=discord.ButtonStyle.success, emoji="🎁")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        gained, count, new_badges = await self.cog.claim_milestones(self.user_id)
        embed = await self.cog.build_dex_embed(self.user_id)
        if count:
            await interaction.response.edit_message(embed=embed, view=self)
            await interaction.followup.send(
                f"🎁 図鑑マイルストーン {count}件 達成報酬 **+{gained:,} リリー** を受け取りました！",
                ephemeral=True,
            )
            await badges.notify(interaction, new_badges)
        else:
            await interaction.response.send_message("受け取れる報酬はありません。", ephemeral=True)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class _CreatureSelect(discord.ui.Select):
    """所持生き物から1体を選ぶドロップダウン。"""

    def __init__(self, rows, placeholder: str):
        options = []
        for r in rows[:25]:
            sp = creatures.get(r["species_id"])
            if sp is None:
                continue
            pct = game.iv_percent(r["iv_hp"], r["iv_atk"], r["iv_def"])
            nick = f"「{r['nickname']}」" if r["nickname"] else ""
            label = f"{sp.name}{nick} IV{pct:.0f}%"[:100]
            desc = f"{sp.element_info[0]} ・ {sp.rarity_info.label} ・ #{r['instance_id']}"[:100]
            options.append(discord.SelectOption(
                label=label, value=str(r["instance_id"]),
                description=desc, emoji=sp.rarity_info.emoji))
        super().__init__(placeholder=placeholder, options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        await self.view.on_pick(interaction, int(self.values[0]))


class _MaterialSelect(discord.ui.Select):
    """合体の素材（同種の別個体）を選ぶドロップダウン。"""

    def __init__(self, rows):
        options = []
        for r in rows[:25]:
            sp = creatures.get(r["species_id"])
            pct = game.iv_percent(r["iv_hp"], r["iv_atk"], r["iv_def"])
            options.append(discord.SelectOption(
                label=f"{sp.name} IV{pct:.0f}% (#{r['instance_id']})"[:100],
                value=str(r["instance_id"]), emoji=sp.rarity_info.emoji))
        super().__init__(placeholder="素材にする同種の個体を選ぶ（消費されます）",
                         options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        await self.view.do_merge(interaction, int(self.values[0]))


class InventoryView(discord.ui.View):
    """コレクションを1画面で操作: 選択→詳細→逃がす／合体。"""

    def __init__(self, cog: "Collection", user_id: int, rows: list, items: list, cap: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id
        self.rows = rows
        self.items = items
        self.cap = cap
        self.current: int | None = None   # 選択中の instance_id
        self.mode = "list"                # list / merge / release_confirm
        self.message: discord.Message | None = None
        self._rebuild()

    # ---- 権限・後片付け ----
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("これはあなたのコレクションではありません。", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    # ---- 補助 ----
    def _row(self, inst: int):
        return next((r for r in self.rows if r["instance_id"] == inst), None)

    def _same_species(self, inst: int) -> list:
        row = self._row(inst)
        if row is None:
            return []
        return [r for r in self.rows
                if r["species_id"] == row["species_id"] and r["instance_id"] != inst]

    def _usable_items(self) -> list:
        """生き物に使える所持アイテム（リロール薬／なまえ札）。"""
        return [r for r in self.items if r["item_id"] in USABLE_ITEMS and r["qty"] > 0]

    # ---- 画面構築 ----
    def _rebuild(self):
        self.clear_items()
        if self.mode == "merge" and self.current is not None:
            self.add_item(_MaterialSelect(self._same_species(self.current)))
            self.add_item(_BackButton())
            return
        if self.mode == "release_confirm" and self.current is not None:
            self.add_item(_ConfirmReleaseButton())
            self.add_item(_BackButton())
            return
        if self.mode == "item" and self.current is not None:
            usable = self._usable_items()
            if usable:
                self.add_item(_ItemSelect(usable))
            self.add_item(_BackButton())
            return
        # list モード
        if self.rows:
            self.add_item(_CreatureSelect(self.rows, "生き物を選んで詳細・操作"))
        if self.current is not None and self._row(self.current) is not None:
            can_merge = len(self._same_species(self.current)) > 0
            self.add_item(_MergeButton(disabled=not can_merge))
            self.add_item(_UseItemButton())
            self.add_item(_ReleaseButton())
            self.add_item(_ToListButton())

    def build_embed(self) -> discord.Embed:
        if self.current is not None and self._row(self.current) is not None:
            embed = creature_detail_embed(self._row(self.current))
            if self.mode == "merge":
                embed.color = 0x9B59B6
                n = len(self._same_species(self.current))
                embed.description = (embed.description or "") + \
                    f"\n\n🔗 **合体**：素材にする同種の個体を選んでください（候補 {n}体・コスト {game.MERGE_COST_COINS} リリー）。"
            elif self.mode == "release_confirm":
                embed.color = 0x95A5A6
                embed.description = (embed.description or "") + "\n\n🕊️ **この個体を逃がしますか？** （少額のリリーが戻ります）"
            elif self.mode == "item":
                embed.color = 0x3498DB
                if self._usable_items():
                    embed.description = (embed.description or "") + \
                        "\n\n🎁 **使うアイテムを選んでください**（🎲個体値リロール薬／🏷️なまえ札）。"
                else:
                    embed.description = (embed.description or "") + \
                        "\n\n🎁 使えるアイテムを持っていません。`/shop` で購入できます。"
            return embed
        # 一覧サマリ
        embed = discord.Embed(
            title=f"🎒 コレクション（{len(self.rows)} / {self.cap} 体）",
            description="下のメニューから生き物を選ぶと、詳細の確認・逃がす・合体・アイテム使用ができます。",
            color=0xE91E63,
        )
        if self.rows:
            top = sorted(self.rows,
                         key=lambda r: game.iv_percent(r["iv_hp"], r["iv_atk"], r["iv_def"]),
                         reverse=True)[:10]
            lines = []
            for r in top:
                sp = creatures.get(r["species_id"])
                if sp is None:
                    continue
                pct = game.iv_percent(r["iv_hp"], r["iv_atk"], r["iv_def"])
                nick = f"「{r['nickname']}」" if r["nickname"] else ""
                lines.append(f"{sp.element_info[1]}{sp.rarity_info.emoji} **{sp.name}**{nick} "
                             f"・ IV {pct:.0f}%（{game.iv_grade(pct)}）・ #{r['instance_id']}")
            if len(self.rows) > 10:
                lines.append(f"…ほか {len(self.rows) - 10} 体（メニューには先頭25体まで表示）")
            embed.add_field(name="個体値が高い順", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="生き物", value="まだいません。`/explore` で探そう！", inline=False)
        if self.items:
            item_lines = [f"・{iid_label(r['item_id'])} × {r['qty']}" for r in self.items]
            embed.add_field(name="🎁 アイテム", value="\n".join(item_lines), inline=False)
        return embed

    async def refresh(self, interaction: discord.Interaction):
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    # ---- アクション ----
    async def on_pick(self, interaction: discord.Interaction, inst: int):
        self.current = inst
        self.mode = "list"
        await self.refresh(interaction)

    async def enter_merge(self, interaction: discord.Interaction):
        self.mode = "merge"
        await self.refresh(interaction)

    async def enter_release(self, interaction: discord.Interaction):
        self.mode = "release_confirm"
        await self.refresh(interaction)

    async def enter_item(self, interaction: discord.Interaction):
        self.mode = "item"
        await self.refresh(interaction)

    async def back(self, interaction: discord.Interaction):
        """サブ操作（合体/逃がす/アイテム）から生き物の詳細に戻る。"""
        self.mode = "list"
        await self.refresh(interaction)

    async def to_list(self, interaction: discord.Interaction):
        """生き物の詳細から一覧サマリに戻る。"""
        self.current = None
        self.mode = "list"
        await self.refresh(interaction)

    async def use_item(self, interaction: discord.Interaction, item_id: str):
        if item_id == "name_tag":
            # なまえ札は名前入力のモーダルを開く（モーダル送信側で確定・再描画）
            await interaction.response.send_modal(_NameTagModal(self))
            return
        # iv_reroll はその場で適用
        msg = await self.cog.apply_iv_reroll(self.user_id, self.current)
        self.items = await self.cog.db.list_items(self.user_id)
        self.rows = await self.cog.db.list_creatures(self.user_id)
        self.mode = "list"
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        await interaction.followup.send(msg, ephemeral=True)

    async def finish_name_tag(self, interaction: discord.Interaction, name: str):
        msg = await self.cog.apply_name_tag(self.user_id, self.current, name)
        self.items = await self.cog.db.list_items(self.user_id)
        self.rows = await self.cog.db.list_creatures(self.user_id)
        self.mode = "list"
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        await interaction.followup.send(msg, ephemeral=True)
        await badges.notify(interaction, await badges.sync(self.cog.db, self.user_id))

    async def do_merge(self, interaction: discord.Interaction, material_inst: int):
        base = self.current
        msg = await self.cog.perform_merge(self.user_id, base, material_inst)
        self.rows = await self.cog.db.list_creatures(self.user_id)
        self.mode = "list"
        if self._row(base) is None:  # 念のため（存在しなければ選択解除）
            self.current = None
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        await interaction.followup.send(msg, ephemeral=True)
        await badges.notify(interaction, await badges.sync(self.cog.db, self.user_id))

    async def do_release(self, interaction: discord.Interaction):
        inst = self.current
        msg = await self.cog.perform_release(self.user_id, inst)
        self.rows = await self.cog.db.list_creatures(self.user_id)
        self.current = None
        self.mode = "list"
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
        await interaction.followup.send(msg, ephemeral=True)
        await badges.notify(interaction, await badges.sync(self.cog.db, self.user_id))


class _MergeButton(discord.ui.Button):
    def __init__(self, disabled: bool):
        super().__init__(label="合体する", style=discord.ButtonStyle.primary, emoji="🔗", disabled=disabled)

    async def callback(self, interaction: discord.Interaction):
        await self.view.enter_merge(interaction)


class _ReleaseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="逃がす", style=discord.ButtonStyle.danger, emoji="🕊️")

    async def callback(self, interaction: discord.Interaction):
        await self.view.enter_release(interaction)


class _ConfirmReleaseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="逃がす（確定）", style=discord.ButtonStyle.danger, emoji="✅")

    async def callback(self, interaction: discord.Interaction):
        await self.view.do_release(interaction)


class _UseItemButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="アイテムを使う", style=discord.ButtonStyle.success, emoji="🎁")

    async def callback(self, interaction: discord.Interaction):
        await self.view.enter_item(interaction)


class _ToListButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="一覧へ戻る", style=discord.ButtonStyle.secondary, emoji="📋")

    async def callback(self, interaction: discord.Interaction):
        await self.view.to_list(interaction)


class _BackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="戻る", style=discord.ButtonStyle.secondary, emoji="◀️")

    async def callback(self, interaction: discord.Interaction):
        await self.view.back(interaction)


class _ItemSelect(discord.ui.Select):
    """選択中の生き物に使うアイテムを選ぶドロップダウン。"""

    def __init__(self, usable_rows):
        options = []
        for r in usable_rows:
            meta = USABLE_ITEMS[r["item_id"]]
            options.append(discord.SelectOption(
                label=f"{meta['label']} × {r['qty']}", value=r["item_id"],
                description=meta["desc"], emoji=meta["emoji"]))
        super().__init__(placeholder="使うアイテムを選ぶ", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        await self.view.use_item(interaction, self.values[0])


class _NameTagModal(discord.ui.Modal, title="🏷️ なまえ札"):
    name = discord.ui.TextInput(label="新しい名前", placeholder="最大20文字", max_length=20, required=True)

    def __init__(self, view: "InventoryView"):
        super().__init__()
        self._view = view

    async def on_submit(self, interaction: discord.Interaction):
        await self._view.finish_name_tag(interaction, str(self.name.value))


class Collection(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    async def _area_status(self, uid: int, habitat: str) -> tuple[bool, str]:
        """エリアが解放済みか。(解放, 未解放理由)。"""
        hb = creatures.HABITATS[habitat]
        kind = hb.unlock[0]
        if kind == "start":
            return True, ""
        if kind == "dex":
            need = hb.unlock[1]
            cnt = await self.db.distinct_species_count(uid)
            if cnt >= need:
                return True, ""
            return False, f"図鑑を **{need}** 種集めると解放（現在 {cnt} 種）"
        # ticket
        if habitat in await self.db.unlocked_areas(uid):
            return True, ""
        return False, "**エリア解放チケット**（`/shop` でジェム購入）で解放できます"

    @app_commands.command(name="explore", description="エリアを選んで生き物を探します（深いほどレア＆高コスト）")
    @app_commands.rename(area="エリア", use_bait="餌を使う", premium="限定探索")
    @app_commands.describe(
        area="探索するエリア（未指定は草原）",
        use_bait="餌を使って遭遇率を上げます（所持していれば）",
        premium="限定探索チケット(ジェムで購入)を使い、限定個体を探します",
    )
    @app_commands.choices(area=HABITAT_CHOICES)
    async def explore(
        self, interaction: discord.Interaction,
        area: app_commands.Choice[str] | None = None,
        use_bait: bool = False, premium: bool = False,
    ):
        # 遭遇はみんなに見える（公開）／空振り・エラーは本人だけ（ephemeral）
        uid = interaction.user.id
        db = self.db

        # ---- 限定探索（チケット消費・遭遇確定） ----
        if premium:
            if not await db.try_consume_item(uid, LIMITED_TICKET, 1):
                await interaction.response.send_message(
                    "限定探索チケットがありません。`/shop` でジェムと交換できます。", ephemeral=True
                )
                return
            sp = game.weighted_encounter(pool=creatures.LIMITED_SPECIES)
            await db.bump_stat(uid, "explores")
            await self._present_encounter(interaction, uid, sp, foot_extra="限定チケットを1枚消費")
            await notify_quests(interaction, await quests.record_event(db, uid, "explore"))
            await notify_quests(interaction, await quests.record_event(db, uid, "encounter"))
            await badges.notify(interaction, await badges.sync(db, uid))
            return

        habitat = area.value if area else creatures.DEFAULT_HABITAT
        hb = creatures.HABITATS[habitat]

        # エリア解放チェック（チケット制は所持チケットで自動解放）
        unlocked, reason = await self._area_status(uid, habitat)
        if not unlocked:
            if hb.unlock[0] == "ticket" and await db.try_consume_item(uid, AREA_TICKET, 1):
                await db.unlock_area(uid, habitat)
                unlocked = True
            else:
                await interaction.response.send_message(
                    f"{hb.emoji} **{hb.name}** はまだ解放されていません。\n{reason}", ephemeral=True
                )
                return

        # 餌
        bonus = 0.0
        used_bait = None
        if use_bait:
            if await db.try_consume_item(uid, "gold_bait", 1):
                bonus, used_bait = GOLD_BAIT_ENCOUNTER_BONUS, "金の餌"
            elif await db.try_consume_item(uid, "bait", 1):
                bonus, used_bait = BAIT_ENCOUNTER_BONUS, "餌"
            else:
                await interaction.response.send_message(
                    "餌を持っていません。`/shop` で購入できます。", ephemeral=True)
                return

        # 深度・天候・コスト
        now = int(time.time())
        st = await db.get_explore_state(uid)
        depth = game.next_depth(st["habitat"] if st else None, habitat,
                                st["depth"] if st else 0, st["last_at"] if st else 0, now)
        cost = game.explore_cost(habitat, depth)
        weather = game.weather_for(quests.daily_period())
        wbonus = game.encounter_bonus(weather, habitat)

        if not await db.try_spend_coins(uid, cost, reason="explore"):
            bal = await db.get_balance(uid)
            await interaction.response.send_message(
                f"リリーが足りません（必要: {cost} / 所持: {bal.coins:,} リリー）。\n"
                f"`/quests` や `/login` で稼ぎましょう。", ephemeral=True,
            )
            return
        await db.set_explore_state(uid, habitat, depth, now)
        await db.bump_stat(uid, "explores")
        await db.bump_max_depth(uid, depth)

        rare_boost = depth * game.DEPTH_RARE_STEP + weather.rare_bonus
        info = f"{hb.emoji} {hb.name} ・ 深度{depth} ・ {weather.label}"

        if not game.try_encounter(bonus + wbonus):
            bal = await db.get_balance(uid)
            embed = discord.Embed(title="🌿 …何も見つからなかった",
                                  description=f"{info}\n生き物の気配はなかった。もう一度探そう。", color=0x95A5A6)
            foot = f"消費 {cost} リリー ・ 残高 {bal.coins:,} リリー"
            if used_bait:
                foot += f" ・ {used_bait}を1つ消費"
            embed.set_footer(text=foot)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            await notify_quests(interaction, await quests.record_event(db, uid, "explore"))
            await badges.notify(interaction, await badges.sync(db, uid))
            return

        sp = game.weighted_encounter(pool=creatures.species_in_habitat(habitat), rare_boost=rare_boost)
        await self._present_encounter(
            interaction, uid, sp,
            foot_extra=(f"{used_bait}を1つ消費" if used_bait else None),
            explore_cost=cost, info=info,
        )
        await notify_quests(interaction, await quests.record_event(db, uid, "explore"))
        await notify_quests(interaction, await quests.record_event(db, uid, "encounter"))
        await badges.notify(interaction, await badges.sync(db, uid))

    async def _present_encounter(self, interaction, uid, sp, *, foot_extra=None, explore_cost=0, info=None):
        db = self.db
        has_charm = (await db.get_item_qty(uid, "charm")) > 0
        bal = await db.get_balance(uid)
        cost = game.tame_cost(sp)
        embed = creature_embed(sp, title=f"✨ {interaction.user.display_name} の前に {sp.name} が現れた！",
                               color=0x9B59B6)
        if info:
            embed.description = f"{info}\n{embed.description}"
        embed.add_field(name="手なずけ",
                        value=f"コスト **{cost} {COIN} リリー** / 成功率 約 {int(game.tame_success_rate(sp)*100)}%",
                        inline=False)
        foot = f"残高 {bal.coins:,} リリー"
        if explore_cost:
            foot = f"探索消費 {explore_cost} リリー ・ " + foot
        if foot_extra:
            foot += f" ・ {foot_extra}"
        embed.set_footer(text=foot)
        view = TameView(self, uid, sp, has_charm)
        # 遭遇は公開（みんなに見える）
        await interaction.response.send_message(embed=embed, view=view)

    async def claim_milestones(self, user_id: int) -> tuple[int, int, list[str]]:
        """達成済み・未受取の図鑑マイルストーン報酬を受け取る。(合計, 件数, 新規バッジID)。"""
        cnt = await self.db.distinct_species_count(user_id)
        claimed = await self.db.claimed_milestones(user_id)
        gained = count = 0
        for need, reward in game.dex_milestones_reached(cnt):
            if need in claimed:
                continue
            if await self.db.claim_milestone(user_id, need):
                await self.db.add_coins(user_id, reward, "milestone")
                gained += reward
                count += 1
        new_badges = await badges.sync(self.db, user_id)
        return gained, count, new_badges

    async def build_dex_embed(self, user_id: int) -> discord.Embed:
        owned = await self.db.distinct_species(user_id)
        owned_normal = sum(1 for sp in creatures.NORMAL_SPECIES if sp.species_id in owned)

        pct = owned_normal / creatures.TOTAL_SPECIES * 100
        bar = game.progress_bar(owned_normal, creatures.TOTAL_SPECIES)
        embed = discord.Embed(
            title="📖 生き物図鑑",
            description=f"`{bar}` **{owned_normal} / {creatures.TOTAL_SPECIES}** 種 ・ 達成率 {pct:.0f}%",
            color=0x1ABC9C,
        )

        # エリアごとにまとめて表示（見つけた場所が分かる）
        for hkey, hb in creatures.HABITATS.items():
            pool = creatures.species_in_habitat(hkey)
            if not pool:
                continue
            have = sum(1 for sp in pool if sp.species_id in owned)
            names = []
            for sp in pool:
                el = sp.element_info[1]
                if sp.species_id in owned:
                    names.append(f"✅{el}{sp.name}")
                else:
                    names.append(f"▫️{el}？？？")
            embed.add_field(
                name=f"{hb.emoji} {hb.name}（{have}/{len(pool)}）",
                value="　".join(names), inline=False,
            )

        lim = []
        for sp in creatures.LIMITED_SPECIES:
            el = sp.element_info[1]
            lim.append(f"✅🌟{el}{sp.name}" if sp.species_id in owned else f"▫️🌟{el}？？？")
        if lim:
            embed.add_field(name="🌟 限定個体", value="　".join(lim), inline=False)

        # マイルストーン進捗
        claimed = await self.db.claimed_milestones(user_id)
        lines = []
        for need, reward in game.DEX_MILESTONES:
            if need in claimed:
                lines.append(f"✅ {need}種 … 受取済み")
            elif owned_normal >= need:
                lines.append(f"🎁 {need}種 … **+{reward:,} リリー 受取可能**")
            else:
                lines.append(f"▫️ {need}種 … {reward:,} リリー（あと {need - owned_normal}種）")
        embed.add_field(name="🏅 収集マイルストーン", value="\n".join(lines), inline=False)

        embed.set_footer(text="✅=収集済み ▫️=未発見 ・ 新種発見でリリー報酬")
        return embed

    def _has_claimable_milestone(self, owned_normal: int, claimed: set[int]) -> bool:
        return any(need not in claimed and owned_normal >= need for need, _ in game.DEX_MILESTONES)

    @app_commands.command(name="dex", description="図鑑の収集状況とマイルストーン報酬を表示します")
    async def dex(self, interaction: discord.Interaction):
        uid = interaction.user.id
        embed = await self.build_dex_embed(uid)
        # マイルストーン受取可能ならボタン付き（本人操作）
        owned_normal = await self.db.distinct_species_count(uid)
        claimed = await self.db.claimed_milestones(uid)
        # view=None は discord.py が受け付けない（MISSING を使う）
        view = (DexClaimView(self, uid)
                if self._has_claimable_milestone(owned_normal, claimed)
                else discord.utils.MISSING)
        # 図鑑は公開（みんなに見える）
        await interaction.response.send_message(embed=embed, view=view)

    async def perform_release(self, uid: int, inst: int) -> str:
        """1個体を逃がす。結果メッセージを返す（View/コマンド共通）。"""
        row = await self.db.get_creature(uid, inst)
        if row is None:
            return "その生き物は見つかりません。"
        sp = creatures.get(row["species_id"])
        if sp is None:
            return "不明な種です。"
        value = game.release_value(sp, row["iv_hp"], row["iv_atk"], row["iv_def"])
        pct = game.iv_percent(row["iv_hp"], row["iv_atk"], row["iv_def"])
        if not await self.db.release_creature(uid, inst):
            return "逃がせませんでした。"
        await self.db.add_coins(uid, value, "release")
        await self.db.bump_stat(uid, "releases")
        bal = await self.db.get_balance(uid)
        return (f"🕊️ {sp.rarity_info.emoji} **{sp.name}**（IV {pct:.0f}%）を逃がして "
                f"**+{value:,} リリー** を受け取った。（残高 {bal.coins:,} リリー）")

    async def perform_merge(self, uid: int, base: int, material: int) -> str:
        """base に material を合体（同種）。結果メッセージを返す。"""
        if base is None or base == material:
            return "別々の個体を選んでください。"
        rb = await self.db.get_creature(uid, base)
        rm = await self.db.get_creature(uid, material)
        if rb is None or rm is None:
            return "生き物が見つかりません。"
        if rb["species_id"] != rm["species_id"]:
            return "同じ種どうしのみ合体できます。"
        sp = creatures.get(rb["species_id"])
        if not await self.db.try_spend_coins(uid, game.MERGE_COST_COINS, "merge"):
            bal = await self.db.get_balance(uid)
            return f"リリーが足りません（必要 {game.MERGE_COST_COINS} / 所持 {bal.coins:,}）。"
        old_pct = game.iv_percent(rb["iv_hp"], rb["iv_atk"], rb["iv_def"])
        nh, na, nd = game.merge_ivs(
            (rb["iv_hp"], rb["iv_atk"], rb["iv_def"]),
            (rm["iv_hp"], rm["iv_atk"], rm["iv_def"]))
        if not await self.db.merge_creatures(uid, base, material, nh, na, nd):
            await self.db.add_coins(uid, game.MERGE_COST_COINS, "merge_refund")
            return "合体に失敗しました。"
        await self.db.bump_stat(uid, "merges")
        new_pct = game.iv_percent(nh, na, nd)
        return (f"🔗 {sp.rarity_info.emoji} **{sp.name}**（#{base}）を強化！ "
                f"IV {old_pct:.0f}% → **{new_pct:.0f}%** ⬆️（-{game.MERGE_COST_COINS} リリー）")

    @app_commands.command(name="release", description="生き物を逃がして少しのリリーコインを受け取ります")
    @app_commands.rename(creature="生き物")
    @app_commands.describe(creature="逃がす生き物を選択")
    async def release(self, interaction: discord.Interaction, creature: str):
        uid = interaction.user.id
        try:
            inst = int(creature)
        except (TypeError, ValueError):
            await interaction.response.send_message("逃がす生き物を一覧から選択してください。", ephemeral=True)
            return
        msg = await self.perform_release(uid, inst)
        await interaction.response.send_message(msg, ephemeral=True)
        await badges.notify(interaction, await badges.sync(self.db, uid))

    async def _creature_autocomplete(self, interaction: discord.Interaction, current: str):
        uid = interaction.user.id
        rows = await self.db.list_creatures(uid)
        out = []
        for r in rows:
            sp = creatures.get(r["species_id"])
            if sp is None:
                continue
            pct = game.iv_percent(r["iv_hp"], r["iv_atk"], r["iv_def"])
            label = f"{sp.name} IV{pct:.0f}% (#{r['instance_id']})"
            if not current or current.lower() in label.lower():
                out.append(app_commands.Choice(name=label[:100], value=str(r["instance_id"])))
            if len(out) >= 25:
                break
        return out

    @release.autocomplete("creature")
    async def release_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._creature_autocomplete(interaction, current)

    async def apply_iv_reroll(self, uid: int, inst: int) -> str:
        """個体値リロール薬を1つ消費して個体値を振り直す。結果メッセージを返す。"""
        row = await self.db.get_creature(uid, inst)
        if row is None:
            return "その生き物は見つかりません。"
        sp = creatures.get(row["species_id"])
        if not await self.db.try_consume_item(uid, "iv_reroll", 1):
            return "🎲 個体値リロール薬がありません。`/shop` で購入できます。"
        old_pct = game.iv_percent(row["iv_hp"], row["iv_atk"], row["iv_def"])
        h, a, d = game.roll_ivs()
        await self.db.reroll_creature_ivs(uid, inst, h, a, d)
        new_pct = game.iv_percent(h, a, d)
        arrow = "⬆️" if new_pct > old_pct else ("⬇️" if new_pct < old_pct else "➡️")
        return (f"🎲 {sp.rarity_info.emoji} **{sp.name}**（#{inst}）の個体値を振り直し："
                f"IV {old_pct:.0f}% → **{new_pct:.0f}%** {arrow}（HP{h}/ATK{a}/DEF{d}）")

    async def apply_name_tag(self, uid: int, inst: int, name: str) -> str:
        """なまえ札を1つ消費して名前を付ける。結果メッセージを返す。"""
        nick = _clean_nickname(name or "")
        if not nick:
            return "🏷️ 名前を入力してください。"
        row = await self.db.get_creature(uid, inst)
        if row is None:
            return "その生き物は見つかりません。"
        sp = creatures.get(row["species_id"])
        if not await self.db.try_consume_item(uid, "name_tag", 1):
            return "🏷️ なまえ札がありません。`/shop` で購入できます。"
        await self.db.set_nickname(uid, inst, nick)
        return f"🏷️ {sp.rarity_info.emoji} {sp.name}（#{inst}）を **「{nick}」** と名付けた。"

    @app_commands.command(name="merge", description="同種2体を合体して個体値を強化します（素材消費・コストあり）")
    @app_commands.rename(base="残す生き物", material="素材にする生き物")
    @app_commands.describe(base="強化して残す方", material="消費する方（同じ種）")
    async def merge(self, interaction: discord.Interaction, base: str, material: str):
        uid = interaction.user.id
        try:
            b, m = int(base), int(material)
        except (TypeError, ValueError):
            await interaction.response.send_message("生き物を一覧から選択してください。", ephemeral=True)
            return
        msg = await self.perform_merge(uid, b, m)
        # 成功（🔗）は公開して自慢できる／失敗は本人のみ
        await interaction.response.send_message(msg, ephemeral=not msg.startswith("🔗"))
        await badges.notify(interaction, await badges.sync(self.db, uid))

    @merge.autocomplete("base")
    async def merge_base_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._creature_autocomplete(interaction, current)

    @merge.autocomplete("material")
    async def merge_material_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._creature_autocomplete(interaction, current)

    @app_commands.command(name="badges", description="獲得したバッジ（称号）を表示します")
    async def badges(self, interaction: discord.Interaction):
        uid = interaction.user.id
        new = await badges.sync(self.db, uid)
        have = await self.db.get_badges(uid)
        embed = discord.Embed(title="🎖️ バッジ・称号", color=0xF1C40F)
        normal_lines = []
        secret_lines = []
        for b in game.BADGE_LIST:
            if b.id in have:
                (secret_lines if b.secret else normal_lines).append(f"✅ **{b.name}** — {b.desc}")
            elif b.secret:
                secret_lines.append("🔒 ???")
            else:
                normal_lines.append(f"🔒 ??? — 💡{b.hint}")
        embed.add_field(name="バッジ", value="\n".join(normal_lines), inline=False)
        embed.add_field(name="🕵️ 隠しバッジ", value="\n".join(secret_lines), inline=False)
        embed.set_footer(text=f"{len(have)} / {len(game.BADGE_LIST)} 獲得")
        await interaction.response.send_message(embed=embed)
        await badges.notify(interaction, new)

    @app_commands.command(name="inventory", description="コレクションを開いて詳細確認・逃がす・合体・アイテム使用ができます")
    async def inventory(self, interaction: discord.Interaction):
        uid = interaction.user.id
        rows = await self.db.list_creatures(uid)
        items = await self.db.list_items(uid)
        cap = await self.db.get_creature_cap(uid)
        view = InventoryView(self, uid, rows, items, cap)
        # 操作パネルなので本人にだけ表示（ephemeral）
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)
        view.message = await interaction.original_response()


def iid_label(item_id: str) -> str:
    from cogs.shop import SHOP_ITEMS
    it = SHOP_ITEMS.get(item_id)
    return it["name"] if it else item_id


async def setup(bot: commands.Bot):
    await bot.add_cog(Collection(bot))
