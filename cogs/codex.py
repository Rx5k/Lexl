"""資料室Cog（社長専用）: `/codex`

ゲームの仕組み・データ・攻略情報をすべて1コマンドで閲覧する。表示内容:

- 📜 クエスト … デイリー30/通常50テンプレの目標範囲・報酬・受取実績
- 🐾 生き物   … 全種の属性/レア度/エリア/ステータス/出現率/手なずけ率とコスト
- 📖 図鑑     … 全体の収集状況（誰が何種持っているか・未発見の種）
- 🎖️ バッジ   … 隠しバッジを含む全条件と獲得者数
- 🗺️ エリア   … 解放条件・コスト・深度・天候の補正
- 🏪 アイテム … ショップ価格・効果・全ユーザーの保有総数
- ⚙️ 経済     … コスト/報酬/確率などの全パラメータ

**この情報は攻略の核心（出現率・成功率・隠しバッジ条件）を含むため、社長本人以外には
決して表示しない。** 権限チェックは admin.is_owner（Discordユーザーnnnnn ID一致）を使う。
他人のサーバーで管理者権限を持っていても、IDが一致しなければ実行できない。
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import game
import quests
from cogs.admin import is_owner
from cogs.shop import SHOP_ITEMS
from data import creatures

COIN = "⚜️"
GEM = "💎"

LINES_PER_PAGE = 12


def _reward_text(r) -> str:
    parts = []
    if r.coins:
        parts.append(f"{r.coins:,}{COIN}")
    for iid, qty in r.items:
        parts.append(f"{SHOP_ITEMS.get(iid, {}).get('name', iid)}×{qty}")
    return " ＋ ".join(parts) if parts else "—"


def _paginate(title: str, color: int, lines: list[str], note: str = "") -> list[discord.Embed]:
    """行のリストを複数ページの Embed に分割する（Discordの文字数制限対策）。"""
    if not lines:
        lines = ["（データなし）"]
    pages: list[list[str]] = []
    buf: list[str] = []
    size = 0
    for ln in lines:
        # 1ページ ≒ 3,500文字 か LINES_PER_PAGE 行で区切る
        if buf and (size + len(ln) > 3500 or len(buf) >= LINES_PER_PAGE):
            pages.append(buf)
            buf, size = [], 0
        buf.append(ln)
        size += len(ln)
    if buf:
        pages.append(buf)

    embeds = []
    for i, chunk in enumerate(pages, 1):
        e = discord.Embed(title=title, description="\n".join(chunk), color=color)
        e.set_footer(text=f"ページ {i}/{len(pages)}" + (f" ・ {note}" if note else ""))
        embeds.append(e)
    return embeds


class CodexView(discord.ui.View):
    """カテゴリ選択＋ページ送りのパネル。社長本人だけが操作できる。"""

    def __init__(self, cog: "Codex", user_id: int):
        super().__init__(timeout=600)
        self.cog = cog
        self.user_id = user_id
        self.pages: list[discord.Embed] = []
        self.index = 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("あなたの操作ではありません。", ephemeral=True)
            return False
        return True

    @discord.ui.select(
        placeholder="見たい資料を選んでください",
        options=[
            discord.SelectOption(label="クエスト全一覧", value="quests", emoji="📜",
                                 description="デイリー30・通常50の目標範囲と報酬・受取実績"),
            discord.SelectOption(label="生き物データ", value="creatures", emoji="🐾",
                                 description="全種の能力・出現率・手なずけ率・コスト"),
            discord.SelectOption(label="図鑑の収集状況", value="dex", emoji="📖",
                                 description="全体で何種が発見済みか・未発見の種"),
            discord.SelectOption(label="バッジ全条件", value="badges", emoji="🎖️",
                                 description="隠しバッジを含む達成条件と獲得者数"),
            discord.SelectOption(label="エリア・天候・深度", value="areas", emoji="🗺️",
                                 description="解放条件・探索コスト・確率補正"),
            discord.SelectOption(label="アイテム・ショップ", value="items", emoji="🏪",
                                 description="価格・効果・全ユーザーの保有総数"),
            discord.SelectOption(label="経済パラメータ", value="economy", emoji="⚙️",
                                 description="コスト・報酬・確率などの全定数"),
        ],
    )
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer()
        self.pages = await self.cog.build(select.values[0])
        self.index = 0
        await self._render(interaction)

    @discord.ui.button(label="◀ 前", style=discord.ButtonStyle.secondary, row=1)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.index = (self.index - 1) % max(1, len(self.pages))
        await self._render(interaction)

    @discord.ui.button(label="次 ▶", style=discord.ButtonStyle.secondary, row=1)
    async def nxt(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.index = (self.index + 1) % max(1, len(self.pages))
        await self._render(interaction)

    async def _render(self, interaction: discord.Interaction):
        has_pages = len(self.pages) > 1
        self.prev.disabled = not has_pages
        self.nxt.disabled = not has_pages
        embed = self.pages[self.index] if self.pages else discord.Embed(
            title="📚 資料室", description="上のメニューから資料を選んでください。", color=0x9B59B6)
        await interaction.edit_original_response(embed=embed, view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class Codex(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    # ---- 各資料の組み立て -------------------------------------------------
    async def build(self, key: str) -> list[discord.Embed]:
        return await getattr(self, f"_build_{key}")()

    async def _build_quests(self) -> list[discord.Embed]:
        claims = await self.db.quest_claim_counts()
        lines = []
        for kind, label in (("daily", "📅 デイリー"), ("normal", "📜 通常")):
            tmpls = [t for t in quests.ALL_TEMPLATES if t.kind == kind]
            lines.append(f"__**{label}（{len(tmpls)}種）**__")
            for t in tmpls:
                lo, hi, w = quests._tiers_for(t.kind)[t.tier]
                q_lo = quests.quest_from(t.tid, lo)
                q_hi = quests.quest_from(t.tid, hi)
                n = claims.get(t.tid, 0)
                lines.append(
                    f"`{t.tid}` **{t.title}**［{t.tier}・重み{w}］{quests.DESC_FMT[t.event].format(n=f'{lo}〜{hi}')}\n"
                    f"　最小: {_reward_text(q_lo.reward)}\n"
                    f"　最大: {_reward_text(q_hi.reward)}"
                    + (f"　・受取 {n}回" if n else "")
                )
        note = f"報酬内訳は約{int(_coin_ratio()*100)}%リリー"
        return _paginate("📜 クエスト全一覧（テンプレート）", 0x2ECC71, lines, note)

    async def _build_creatures(self) -> list[discord.Embed]:
        counts = await self.db.species_owner_counts()
        # エリアごとの重み合計 → 出現率(%)を算出
        lines = []
        for hkey, hab in creatures.HABITATS.items():
            pool = creatures.species_in_habitat(hkey)
            total_w = sum(s.encounter_weight for s in pool) or 1
            lines.append(f"__**{hab.emoji} {hab.name}**（探索 {hab.base_cost}{COIN}・出現{len(pool)}種）__")
            for sp in sorted(pool, key=lambda s: -s.encounter_weight):
                ri = sp.rarity_info
                el_name, el_emoji = sp.element_info
                owners, total = counts.get(sp.species_id, (0, 0))
                lines.append(
                    f"{ri.emoji}{el_emoji} **{sp.name}** `{sp.species_id}`［{ri.label}・{el_name}］\n"
                    f"　HP{sp.base_hp}/ATK{sp.base_atk}/DEF{sp.base_def}"
                    f"　出現 {sp.encounter_weight/total_w*100:.1f}%"
                    f"　手なずけ {ri.tame_base_rate*100:.0f}% / {game.tame_cost(sp):,}{COIN}"
                    f"　逃がす {game.release_value(sp,0,0,0):,}〜{game.release_value(sp,31,31,31):,}{COIN}\n"
                    f"　所持 {owners}人 / {total}体 ・ {sp.flavor}"
                )
        limited = [s for s in creatures.CATALOG if s.limited]
        if limited:
            lines.append("__**🌟 限定個体（限定探索チケット専用・通常探索には出ない）**__")
            for sp in limited:
                ri = sp.rarity_info
                el_name, el_emoji = sp.element_info
                owners, total = counts.get(sp.species_id, (0, 0))
                lines.append(
                    f"{ri.emoji}{el_emoji} **{sp.name}** `{sp.species_id}`［{ri.label}・{el_name}］\n"
                    f"　HP{sp.base_hp}/ATK{sp.base_atk}/DEF{sp.base_def}"
                    f"　手なずけ {ri.tame_base_rate*100:.0f}% / {game.tame_cost(sp):,}{COIN}\n"
                    f"　所持 {owners}人 / {total}体 ・ {sp.flavor}"
                )
        note = f"通常{creatures.TOTAL_SPECIES}種＋限定{len(limited)}種"
        return _paginate("🐾 生き物データ（全種）", 0x1ABC9C, lines, note)

    async def _build_dex(self) -> list[discord.Embed]:
        counts = await self.db.species_owner_counts()
        users = await self.db.all_user_ids()
        g = await self.db.global_counters()

        discovered = [s for s in creatures.CATALOG if s.species_id in counts]
        undiscovered = [s for s in creatures.CATALOG if s.species_id not in counts]

        lines = [
            f"__**全体の到達状況**__",
            f"登録ユーザー: **{len(users):,}人**",
            f"発見済み種族: **{len(discovered)}/{creatures.TOTAL_ALL_SPECIES}種**"
            f"（未発見 {len(undiscovered)}種）",
            f"累計 探索{g['explores']:,}回 ・ 手なずけ{g['tames']:,}回 ・ "
            f"合体{g['merges']:,}回 ・ 逃がし{g['releases']:,}回",
            f"到達した最大深度: **{g['max_depth']}** / {game.DEPTH_MAX}",
            "",
            "__**発見済み（所持人数の多い順）**__",
        ]
        for sp in sorted(discovered, key=lambda s: -counts[s.species_id][0]):
            owners, total = counts[sp.species_id]
            mark = "🌟" if sp.limited else sp.rarity_info.emoji
            lines.append(f"{mark} **{sp.name}** — {owners}人が所持 / 累計{total}体")
        if undiscovered:
            lines.append("")
            lines.append("__**まだ誰も捕まえていない種**__")
            for sp in undiscovered:
                mark = "🌟" if sp.limited else sp.rarity_info.emoji
                hab = sp.habitat_info
                lines.append(f"{mark} **{sp.name}**（{hab.emoji}{hab.name}・{sp.rarity_info.label}）")

        milestones = "　".join(f"{n}種→{r:,}{COIN}" for n, r in game.DEX_MILESTONES)
        return _paginate("📖 図鑑の収集状況（全体）", 0x3498DB, lines,
                         f"マイルストーン: {milestones}")

    async def _build_badges(self) -> list[discord.Embed]:
        counts = await self.db.badge_owner_counts()
        lines = ["※ 隠しバッジ（🔒）はプレイヤーには条件が伏せられている。", ""]
        for b in game.BADGE_LIST:
            got = counts.get(b.id, 0)
            lock = "🔒 " if b.secret else ""
            lines.append(
                f"{lock}{b.name} `{b.id}`\n"
                f"　条件: **{b.desc}**"
                + (f"\n　ヒント: {b.hint}" if b.hint else "")
                + f"\n　獲得者: {got}人"
            )
        n_secret = sum(1 for b in game.BADGE_LIST if b.secret)
        return _paginate("🎖️ バッジ全条件", 0xF1C40F, lines,
                         f"全{len(game.BADGE_LIST)}種（うち隠し{n_secret}種）")

    async def _build_areas(self) -> list[discord.Embed]:
        lines = ["__**🗺️ エリア（探索先）**__"]
        for hab in creatures.HABITATS.values():
            pool = creatures.species_in_habitat(hab.key)
            if hab.unlock[0] == "start":
                unlock = "最初から解放"
            elif hab.unlock[0] == "dex":
                unlock = f"図鑑{hab.unlock[1]}種で解放"
            else:
                unlock = f"🗺️ エリア解放チケット（{SHOP_ITEMS['area_ticket']['price_gems']}{GEM}）が必要"
            costs = "／".join(
                f"深度{d}:{game.explore_cost(hab.key, d):,}" for d in range(game.DEPTH_MAX + 1))
            lines.append(
                f"{hab.emoji} **{hab.name}** `{hab.key}` — 基本 {hab.base_cost:,}{COIN}・出現{len(pool)}種\n"
                f"　解放: {unlock}\n　コスト: {costs}"
            )

        lines += [
            "",
            "__**🕳️ 深度（同じエリアを連続探索すると上がる）**__",
            f"最大深度 **{game.DEPTH_MAX}**"
            f"　コスト +{game.DEPTH_COST_STEP*100:.0f}%/深度"
            f"　レア遭遇 +{game.DEPTH_RARE_STEP*100:.0f}%/深度",
            f"別エリアに移動、または **{game.DEPTH_RESET_SECONDS//60}分** 空けるとリセット。",
            "",
            "__**🌤️ 天候（毎日7:00 JSTに日付から決定的に決まる）**__",
        ]
        for w in game.WEATHERS:
            fav = creatures.HABITATS[w.favored_habitat].name if w.favored_habitat else "—"
            lines.append(
                f"{w.label} `{w.key}` — 有利エリア: {fav}"
                f"　遭遇 +{w.encounter_bonus*100:.0f}%　レア +{w.rare_bonus*100:.0f}%"
            )
        today = quests.daily_period()
        lines.append(f"\n本日({today})の天候: **{game.weather_for(today).label}**")
        return _paginate("🗺️ エリア・深度・天候", 0x16A085, lines,
                         f"基本遭遇率 {game.ENCOUNTER_CHANCE*100:.0f}%")

    async def _build_items(self) -> list[discord.Embed]:
        totals = await self.db.item_totals()
        lines = []
        for iid, it in SHOP_ITEMS.items():
            price = (f"{it['price_gems']}{GEM}" if it["price_gems"]
                     else f"{it['price_coins']:,}{COIN}")
            reward_val = quests.REWARD_ITEM_VALUE.get(iid)
            drop = (f"クエスト報酬あり（1回 最大{quests.REWARD_ITEM_MAX_QTY[iid]}個・"
                    f"価値{reward_val:,}{COIN}換算）" if reward_val else "クエストでは配られない")
            lines.append(
                f"{it['name']} `{iid}` — **{price}**\n"
                f"　{it['desc']}\n"
                f"　{drop}\n"
                f"　全ユーザー保有: {totals.get(iid, 0):,}個"
            )
        lines.append(
            f"\n💎 **ジェム** — {self.bot.cfg.gem_price_coins:,}{COIN}/個（ショップで購入）\n"
            f"　クエストでは配られない。資料上の価値換算は {quests.GEM_VALUE_COINS:,}{COIN}/個。"
        )
        return _paginate("🏪 アイテム・ショップ", 0xE67E22, lines,
                         f"全{len(SHOP_ITEMS)}種")

    async def _build_economy(self) -> list[discord.Embed]:
        cfg = self.bot.cfg
        s = await self.db.economy_summary(50000)
        lines = [
            "__**🏦 現在の会計**__",
            f"準備金 {s['reserve']:,} ・ 負債 {s['liabilities']:,} ・ "
            f"**純資産 {s['equity']:,}**（目標まで {s['goal_remaining']:,}）",
            f"利益源: ジェム {s['gem_sales']:,} ／ ゲーム消費 {s['game_sink']:,} ／ 手数料 {s['fees']:,}",
            f"配布: クエスト {s['faucet_quest']:,} ／ ログイン {s['faucet_login']:,} ／ "
            f"マイルストーン {s['faucet_milestone']:,} ／ 逃がし {s['faucet_release']:,}",
            "",
            "__**💸 消費（sink：会社の利益になる）**__",
            f"探索: エリア {min(h.base_cost for h in creatures.HABITATS.values()):,}〜"
            f"{max(h.base_cost for h in creatures.HABITATS.values()):,}{COIN}"
            f"（深度で最大 +{game.DEPTH_COST_STEP*game.DEPTH_MAX*100:.0f}%）",
            f"手なずけ: 基本 {game.TAME_BASE_COST:,}{COIN} × レア倍率 "
            + "／".join(f"{r.label}{r.tame_cost_mult:g}倍" for r in creatures.RARITIES.values()),
            f"合体: {game.MERGE_COST_COINS:,}{COIN}（IV +{game.MERGE_IV_BOOST}）",
            f"クエストリロール: 1日1回無料 → 以降 リロール券 or {game.REROLL_COST_COINS:,}{COIN}",
            f"命名: なまえ札 or {game.NICKNAME_COST_COINS:,}{COIN}",
            f"ジェム: {cfg.gem_price_coins:,}{COIN}/個",
            "",
            "__**🎁 配布（faucet：有界にして会社が損しない設計）**__",
            f"クエスト 1回あたり: "
            + "／".join(f"{k} {v:,}{COIN}" for k, v in quests.REWARD_PER_ACTION.items()),
            f"　→ 各イベントの最低消費/回より必ず小さい＝常に純シンク",
            f"ログイン: {game.LOGIN_REWARDS}（7日周期・最大 {game.max_daily_login():,}{COIN}）",
            f"図鑑マイルストーン: "
            + "／".join(f"{n}種 {r:,}{COIN}" for n, r in game.DEX_MILESTONES),
            f"逃がす還元: 手なずけコストの "
            f"{game.RELEASE_BASE_FRAC*100:.0f}〜{(game.RELEASE_BASE_FRAC+game.RELEASE_IV_FRAC)*100:.0f}%"
            f"（IV依存・必ず入手コスト未満）",
            "",
            "__**💱 換金（リリー → よあコイン）**__",
            f"手数料 {cfg.withdraw_fee_bps/100:g}% ・ 最低 {cfg.min_withdraw:,} ・ "
            f"クールダウン {cfg.withdraw_cooldown}秒",
            f"準備金フロア {cfg.reserve_floor:,}（割る払い出しは拒否）",
            f"**換金できるのは入金した分まで**（無料リリーは換金不可＝farmer対策）",
            f"自動送金(PAYOUT_ENABLED): {'🟢 有効' if cfg.payout_enabled else '🔴 無効（申請キュー）'}",
            "",
            "__**🎲 確率**__",
            f"基本遭遇率 {game.ENCOUNTER_CHANCE*100:.0f}%"
            f"（餌 +20% / 金の餌 +40%・上限95%）",
            "手なずけ成功率: "
            + "／".join(f"{r.label} {r.tame_base_rate*100:.0f}%" for r in creatures.RARITIES.values())
            + "（なつき薬 +20%・上限95%）",
            f"個体値: 各 0〜{game.IV_MAX}（HP/ATK/DEF）",
            "",
            "__**📊 クエスト報酬の構成**__",
            f"リリー 約{_coin_ratio()*100:.0f}% ＋ アイテム 約{(1-_coin_ratio())*100:.0f}%",
            "アイテム上限/回: "
            + "／".join(f"{SHOP_ITEMS[i]['name']}{n}" for i, n in quests.REWARD_ITEM_MAX_QTY.items()),
        ]
        return _paginate("⚙️ 経済パラメータ（全定数）", 0x34495E, lines)

    # ---- コマンド ---------------------------------------------------------
    @app_commands.command(
        name="codex",
        description="【社長用】ゲームの仕組み・データ・攻略情報をすべて閲覧します")
    @app_commands.default_permissions(administrator=True)
    @is_owner()
    async def codex(self, interaction: discord.Interaction):
        view = CodexView(self, interaction.user.id)
        embed = discord.Embed(
            title="📚 資料室（社長専用）",
            description=(
                "このBotの**すべての内部データ**を閲覧できます。\n"
                "下のメニューから資料を選んでください。\n\n"
                "📜 クエスト … 全80テンプレの目標範囲・報酬・受取実績\n"
                "🐾 生き物 … 全種の能力・**出現率**・**手なずけ成功率**・コスト\n"
                "📖 図鑑 … 全体の収集状況・まだ誰も捕まえていない種\n"
                "🎖️ バッジ … **隠しバッジを含む全条件**と獲得者数\n"
                "🗺️ エリア … 解放条件・深度・天候の補正値\n"
                "🏪 アイテム … 価格・効果・全ユーザーの保有総数\n"
                "⚙️ 経済 … コスト/報酬/確率などの全パラメータ"
            ),
            color=0x9B59B6,
        )
        embed.set_footer(text="⚠️ 攻略の核心を含みます。あなた以外には表示されません。")
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @codex.error
    async def codex_error(self, interaction: discord.Interaction, error):
        if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
            msg = "このコマンドは社長本人専用です。"
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        else:
            raise error


def _coin_ratio() -> float:
    """全テンプレ平均で、報酬総価値のうちリリーが占める割合。"""
    tc = tv = 0
    for t in quests.ALL_TEMPLATES:
        lo, hi, _ = quests._tiers_for(t.kind)[t.tier]
        for target in (lo, (lo + hi) // 2, hi):
            q = quests.quest_from(t.tid, target)
            tc += q.reward.coins
            tv += q.reward.total_value
    return tc / tv if tv else 1.0


async def setup(bot: commands.Bot):
    await bot.add_cog(Codex(bot))
