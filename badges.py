"""バッジ判定の共有ヘルパー（cogをまたいで使う）。

stats収集はDB問い合わせを並列化し、達成チェックは各アクション（探索/手なずけ/合体/
逃がす/ログイン/図鑑マイルストーン）の直後に呼び出して即時通知する。
"""
from __future__ import annotations

import asyncio

import discord

import game
from data import creatures


async def gather_stats(db, user_id: int) -> dict:
    rows, login, bal, counters = await asyncio.gather(
        db.list_creatures(user_id),
        db.get_login(user_id),
        db.get_balance(user_id),
        db.get_stats(user_id),
    )
    species_owned: set[str] = set()
    habitats: set[str] = set()
    legendary = 0
    perfect = 0
    has_limited = False
    for row in rows:
        sp = creatures.get(row["species_id"])
        if sp is None:
            continue
        species_owned.add(sp.species_id)
        habitats.add(sp.habitat)
        if sp.rarity == "legendary":
            legendary += 1
        if sp.limited:
            has_limited = True
        if game.iv_percent(row["iv_hp"], row["iv_atk"], row["iv_def"]) >= 100.0:
            perfect += 1
    return {
        "species": len(species_owned),
        "habitats": len(habitats),
        "legendary": legendary,
        "perfect": perfect,
        "explores": counters["explores"],
        "tames": counters["tames"],
        "coins": bal.coins,
        "merges": counters["merges"],
        "releases": counters["releases"],
        "has_limited": has_limited,
        "streak": login["streak"] if login else 0,
        "creatures": len(rows),
        "max_depth": counters["max_depth"],
    }


async def sync(db, user_id: int) -> list[str]:
    """条件を満たした未獲得バッジを付与し、新規付与IDのリストを返す。"""
    stats = await gather_stats(db, user_id)
    have = await db.get_badges(user_id)
    new = []
    for bid in game.earned_badges(stats):
        if bid not in have and await db.grant_badge(user_id, bid):
            new.append(bid)
    return new


async def notify(interaction: discord.Interaction, new_ids: list[str]) -> None:
    """新規獲得バッジがあれば控えめに通知（アクション実行者にephemeral）。"""
    if not new_ids:
        return
    names = "・".join(game.BADGES[bid].name for bid in new_ids)
    try:
        await interaction.followup.send(f"🎖️ 新しいバッジを獲得: **{names}**！ `/badges` で確認できます。",
                                        ephemeral=True)
    except discord.HTTPException:
        pass
