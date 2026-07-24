"""会社純資産シミュレーション。

「入金 → プレイ（クエストで稼ぎ、探索/手なずけ/ジェムで使う）→ 出金」を
多数のプレイヤーで回し、会社の純資産(Equity)が初期資本30,000から
増加する（会社が黒字に向かう）ことを検証する。

`python -m tests.test_simulation` で実行。pytest でも拾われる。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import random

import game
import quests
from db import Database

START_RESERVE = 30000
TARGET = 50000
FEE_BPS = 1000
RESERVE_FLOOR = 5000


async def simulate(seed: int = 12345, players: int = 60, rounds: int = 8) -> dict:
    rng = random.Random(seed)
    path = os.path.join(tempfile.gettempdir(), f"yoacoin_sim_{os.getpid()}_{seed}.db")
    db = Database(path, start_reserve=START_RESERVE)
    await db.connect()
    try:
        for pid in range(1, players + 1):
            uid = 10_000 + pid
            # 入金 → 同額のリリーコインが付与される（1:1）
            dep = rng.choice([1000, 2000, 3000, 5000])
            await db.deposit_yoacoin(uid, dep)

            # 図鑑マイルストーン相当（一度きり・有界faucet）
            await db.add_coins(uid, 400, "milestone")

            for day in range(rounds):
                # ログインボーナス（純faucet・有界）
                await db.add_coins(uid, game.login_reward(day + 1), "login")

                # クエストを「完了」する: 達成に必要な消費(sink) > 報酬(faucet) なので
                # 1件ごとに必ず純シンク。資金が足りるクエストのみ実施（現実的）。
                period = f"2026-06-{(day % 28) + 1:02d}"
                for dq in quests.daily_quests_for(period):
                    bal = (await db.get_balance(uid)).coins
                    cost = int(game.quest_completion_cost_lb(dq.event, dq.target))
                    if bal < cost:
                        continue
                    await db.try_spend_coins(uid, cost, "explore")       # 完了に要する消費
                    await db.add_coins(uid, dq.reward.coins, "daily:rep")  # 報酬(< cost)

                # 追加の探索/手なずけ（sink）
                for _ in range(rng.randint(1, 4)):
                    if (await db.get_balance(uid)).coins >= game.EXPLORE_COST:
                        await db.try_spend_coins(uid, game.EXPLORE_COST, "explore")
                        if game.try_encounter(rng=rng):
                            sp = game.weighted_encounter(rng=rng)
                            cost = game.tame_cost(sp)
                            if (await db.get_balance(uid)).coins >= cost:
                                await db.try_spend_coins(uid, cost, "tame")

                # たまに逃がし還元（有界・純シンク維持）
                if rng.random() < 0.3:
                    await db.add_coins(uid, rng.randint(30, 120), "release")

                # 課金導線: 一定確率でジェム購入（純利益）
                if rng.random() < 0.3 and (await db.get_balance(uid)).coins >= 600:
                    await db.buy_gems(uid, rng.randint(1, 5), 200)

            # 一部プレイヤーは残リリーコインをよあコインに換金（手数料が会社に残る）
            if rng.random() < 0.5:
                bal = await db.get_balance(uid)
                if bal.coins >= 100:
                    gross = bal.coins
                    net, fee = game.withdraw_split(gross, FEE_BPS)
                    await db.withdraw_to_yoacoin(uid, gross, net, fee, RESERVE_FLOOR, queue_only=False)

        summary = await db.economy_summary(TARGET)
        return summary
    finally:
        await db.close()
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(path + suf)
            except OSError:
                pass


def test_company_stays_profitable():
    s = asyncio.run(simulate())
    # 会社純資産は初期資本以上を保つ（会社が損をしない）
    assert s["equity"] >= START_RESERVE, s
    # 利益源（ジェム売上＋ゲーム消費＋出金手数料）がクエスト配布を上回る
    profit_sources = s["gem_sales"] + s["game_sink"] + s["fees"]
    assert profit_sources > s["faucet"], s
    # 準備金はフロアを常に上回っている
    assert s["reserve"] >= RESERVE_FLOOR, s


def test_multiple_seeds_never_lose_money():
    for seed in (1, 7, 42, 100, 2026):
        s = asyncio.run(simulate(seed=seed, players=40, rounds=6))
        assert s["equity"] >= START_RESERVE, (seed, s)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    # 参考数値を1つ表示
    s = asyncio.run(simulate())
    print(f"\n例: equity={s['equity']:,} (start {START_RESERVE:,}, target {TARGET:,}) "
          f"gem_sales={s['gem_sales']:,} game_sink={s['game_sink']:,} "
          f"fees={s['fees']:,} faucet={s['faucet']:,}")
    print(f"{len(fns)}/{len(fns)} simulation tests passed")


if __name__ == "__main__":
    _run_all()
