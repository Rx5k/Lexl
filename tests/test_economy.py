"""経済不変条件のテスト（v2: よあコイン建て）。

目的:「よあコインの価値を維持し、会社が必ず利益（30,000→50,000）」を数値で担保する。

pytest でも、`python -m tests.test_economy` でも実行できる。
"""
from __future__ import annotations

import random

import game
import quests
from data import creatures


# ---- シンク > ファーセット --------------------------------------------------
def test_explore_is_pure_sink():
    assert game.EXPLORE_COST > 0
    assert game.expected_explore_sink() == game.EXPLORE_COST


def test_tame_cost_positive_and_scales_with_rarity():
    costs = {}
    for sp in creatures.CATALOG:
        c = game.tame_cost(sp)
        assert c > 0
        costs.setdefault(sp.rarity, c)
    assert costs["common"] < costs["rare"] < costs["legendary"]


def _rep_quests():
    """各テンプレを難易度帯の最小・中央・最大の目標で実体化した代表クエスト群。"""
    out = []
    for t in quests.ALL_TEMPLATES:
        lo, hi, _ = quests._tiers_for(t.kind)[t.tier]
        for target in (lo, (lo + hi) // 2, hi):
            out.append(quests.quest_from(t.tid, target))
    return out


def test_reward_per_action_below_min_cost():
    # 1アクションあたりの報酬は、各イベントの最低消費/回を必ず下回る＝常に純シンク。
    base = game.MIN_EXPLORE_COST
    assert quests.REWARD_PER_ACTION["explore"] < base
    assert quests.REWARD_PER_ACTION["encounter"] < base / game.ENCOUNTER_CHANCE
    assert quests.REWARD_PER_ACTION["tame_success"] < base / game.ENCOUNTER_CHANCE + game.TAME_BASE_COST


STARTER_DEPOSIT = 5000  # 「よあコイン5,000の入金で十分に遊べる」ことを保証する基準額


def test_starter_budget_is_playable():
    """5,000リリー（＝よあコイン5,000の入金）で十分に遊べること。

    コストを上げる調整をしたときに、初心者が何もできなくなるのを防ぐ。
    """
    grassland = game.explore_cost("grassland", 0)
    assert STARTER_DEPOSIT // grassland >= 35, f"草原の探索が{STARTER_DEPOSIT // grassland}回しかできない"

    common = next(s for s in creatures.CATALOG if s.rarity == "common" and not s.limited)
    catches = STARTER_DEPOSIT / game.expected_cost_per_catch(common)
    assert catches >= 8, f"ノーマルを{catches:.1f}体しか捕まえられない"

    # 最初の図鑑マイルストーン(3種)には無理なく届くこと
    assert game.expected_cost_per_catch(common) * 3 < STARTER_DEPOSIT * 0.5

    # 全エリアが開始資金で複数回は探索できること（極端に高いエリアを作らない）
    for key, hab in creatures.HABITATS.items():
        assert STARTER_DEPOSIT // game.explore_cost(key, 0) >= 10, (key, hab.base_cost)


def test_shop_prices_are_affordable_vs_actions():
    """消耗品が「その行動そのもの」より高くならないこと（買う意味がなくなる）。"""
    from cogs.shop import SHOP_ITEMS
    # 餌は探索を補助する道具なので、探索1回のコストを大きく超えない
    assert SHOP_ITEMS["bait"]["price_coins"] <= game.explore_cost("forest", 0)
    # なつき薬は手なずけ1回のコストを大きく超えない
    assert SHOP_ITEMS["charm"]["price_coins"] <= game.TAME_BASE_COST * 2


def test_rare_costs_more_than_common_to_catch():
    common = next(s for s in creatures.CATALOG if s.rarity == "common" and not s.limited)
    legendary = next(s for s in creatures.CATALOG if s.rarity == "legendary" and not s.limited)
    assert game.expected_cost_per_catch(legendary) > game.expected_cost_per_catch(common)


# ---- クエスト報酬（逓減） ---------------------------------------------------
def test_work_reward_decays_and_has_floor():
    assert game.work_reward(0) == game.WORK_BASE_REWARD
    assert game.work_reward(1) < game.work_reward(0)
    assert game.work_reward(50) == game.WORK_MIN_REWARD


def test_streak_resets_after_window():
    assert game.next_streak(0, game.WORK_STREAK_RESET, 5) == 0
    assert game.next_streak(100, 120, 3) == 4


def test_quest_slots_counts():
    assert len(quests.DAILY_TEMPLATES) >= 30
    assert len(quests.NORMAL_TEMPLATES) >= 50
    assert 2 <= quests.NORMAL_SLOTS <= 3
    assert quests.DAILY_SLOTS == 3


def test_quest_rewards_below_completion_cost():
    # どのテンプレも、どの目標回数でも「達成に必要な消費 > 報酬総価値」＝常に純シンク。
    for q in _rep_quests():
        lb = game.quest_completion_cost_lb(q.event, q.target)
        assert q.reward.total_value < lb, (q.quest_id, q.target, q.reward.total_value, lb)


def test_reward_coin_ratio_about_80_percent():
    reps = _rep_quests()
    total_coins = sum(q.reward.coins for q in reps)
    total_value = sum(q.reward.total_value for q in reps)
    ratio = total_coins / total_value
    assert 0.72 <= ratio <= 0.86, ratio  # 全体で約8割リリーコイン


def test_quests_never_award_gems():
    # ジェムは課金通貨。クエスト報酬で無料配布しない（extraはアイテムのみ）。
    for q in _rep_quests():
        for iid, _ in q.reward.items:
            assert iid in quests.REWARD_ITEM_VALUE


def test_reward_item_qty_is_reasonable():
    """報酬アイテムの個数が現実的な範囲に収まること（なまえ札×40 のような暴走を防ぐ）。"""
    for q in _rep_quests():
        for iid, qty in q.reward.items:
            cap = quests.REWARD_ITEM_MAX_QTY[iid]
            assert 1 <= qty <= cap, (q.quest_id, q.target, iid, qty, cap)
            assert qty <= 5, f"{q.quest_id}: {iid}×{qty} は多すぎる"


def test_reward_item_value_matches_shop_price():
    """報酬計算に使うアイテム価値がショップの実売価と一致すること。

    ずれていると、安く売っている物を高く見積もって配布量が不当に増減する。
    """
    from cogs.shop import SHOP_ITEMS

    for iid, value in quests.REWARD_ITEM_VALUE.items():
        it = SHOP_ITEMS[iid]
        expected = (it["price_gems"] * quests.GEM_VALUE_COINS
                    if it["price_gems"] else it["price_coins"])
        assert value == expected, (iid, value, expected)


def test_hard_quests_give_multiple_reward_kinds():
    """むずかしいクエストは「アイテム＋リリー」など複数種の報酬になること。"""
    hard_with_items = [
        q for q in _rep_quests()
        if q.difficulty == "むずかしい" and q.reward.items
    ]
    assert hard_with_items, "アイテム報酬のあるむずかしいクエストが無い"
    for q in hard_with_items:
        kinds = len(q.reward.items) + (1 if q.reward.coins else 0)
        assert kinds >= 2, (q.quest_id, q.reward)


# ---- 出金手数料（ハウスエッジ） ---------------------------------------------
def test_withdraw_split_charges_fee():
    net, fee = game.withdraw_split(1000, 1000)  # 10%
    assert fee == 100
    assert net == 900
    assert net + fee == 1000


def test_withdraw_fee_is_positive_for_meaningful_amounts():
    net, fee = game.withdraw_split(10000, 1000)  # 10%
    assert fee > 0
    assert net < 10000  # 出金は必ず目減り＝会社に手数料が残る


# ---- 個体値・遭遇・手なずけ --------------------------------------------------
def test_ivs_in_range():
    rng = random.Random(42)
    for _ in range(1000):
        h, a, d = game.roll_ivs(rng)
        assert 0 <= h <= game.IV_MAX and 0 <= a <= game.IV_MAX and 0 <= d <= game.IV_MAX
    assert game.iv_percent(0, 0, 0) == 0
    assert abs(game.iv_percent(game.IV_MAX, game.IV_MAX, game.IV_MAX) - 100.0) < 1e-9


def test_normal_encounter_excludes_limited():
    rng = random.Random(1)
    for _ in range(500):
        sp = game.weighted_encounter(rng=rng)
        assert not sp.limited, "通常探索に限定個体が出てはいけない"


def test_limited_pool_only_limited():
    rng = random.Random(2)
    for _ in range(200):
        sp = game.weighted_encounter(pool=creatures.LIMITED_SPECIES, rng=rng)
        assert sp.limited


def test_tame_rates_are_probabilities():
    for sp in creatures.CATALOG:
        assert 0.0 < game.tame_success_rate(sp) <= 0.95
    common = next(s for s in creatures.CATALOG if s.rarity == "common")
    legendary = next(s for s in creatures.CATALOG if s.rarity == "legendary")
    assert game.tame_success_rate(common) > game.tame_success_rate(legendary)


def test_encounter_distribution_favors_common():
    rng = random.Random(7)
    counts = {"common": 0, "legendary": 0}
    for _ in range(20000):
        sp = game.weighted_encounter(rng=rng)
        if sp.rarity in counts:
            counts[sp.rarity] += 1
    assert counts["common"] > counts["legendary"] * 5


# ---- クエスト定義の健全性 ---------------------------------------------------
def test_quest_defs_valid():
    for q in _rep_quests():
        assert q.target > 0
        assert q.reward.total_value > 0  # 報酬はリリー or アイテムのどちらか
        assert q.event in ("explore", "encounter", "tame_success")
        assert q.kind in ("daily", "normal")


def test_daily_quests_deterministic_and_distinct():
    a = quests.daily_quests_for("2026-07-24")
    b = quests.daily_quests_for("2026-07-24")
    assert [q.quest_id for q in a] == [q.quest_id for q in b]  # 同じ日は同じ
    assert len({q.quest_id for q in a}) == quests.DAILY_SLOTS   # 3種が別々
    # 別の日は（多くの場合）別の組み合わせ
    assert len(a) == quests.DAILY_SLOTS


def test_daily_period_boundary_is_7am():
    from datetime import datetime
    from quests import JST, daily_period
    # 7時前は前日、7時ちょうどは当日
    assert daily_period(datetime(2026, 7, 23, 6, 59, tzinfo=JST)) == "2026-07-22"
    assert daily_period(datetime(2026, 7, 23, 7, 0, tzinfo=JST)) == "2026-07-23"


# ---- Phase A/B/C の不変条件 ------------------------------------------------
def test_merge_ivs_never_below_parents():
    a = (10, 20, 5)
    b = (15, 3, 31)
    h, at, d = game.merge_ivs(a, b)
    assert h >= max(a[0], b[0]) and at >= max(a[1], b[1]) and d >= max(a[2], b[2])
    assert all(0 <= x <= game.IV_MAX for x in (h, at, d))  # 上限を超えない


def test_action_costs_positive():
    assert game.REROLL_COST_COINS > 0
    assert game.MERGE_COST_COINS > 0
    assert game.NICKNAME_COST_COINS > 0
    assert game.CAP_EXPANSION_STEP > 0


def test_badges_thresholds():
    from data import creatures
    zero_stats = {
        "species": 0, "habitats": 0, "legendary": 0, "perfect": 0, "explores": 0,
        "tames": 0, "coins": 0, "merges": 0, "releases": 0, "has_limited": False,
        "streak": 0, "creatures": 0, "max_depth": 0,
    }
    assert game.earned_badges(zero_stats) == []
    full_stats = {
        "species": creatures.TOTAL_SPECIES, "habitats": len(game.HABITATS),
        "legendary": 1, "perfect": 1, "explores": 1000, "tames": 1000,
        "coins": 10 ** 9, "merges": 1000, "releases": 1000,
        "has_limited": True, "streak": 1000, "creatures": 1000, "max_depth": game.DEPTH_MAX,
    }
    assert "collector_all" in game.earned_badges(full_stats)
    assert len(game.earned_badges(full_stats)) == len(game.BADGE_LIST)  # 全部満たせば全獲得
    assert all(b.name and b.desc for b in game.BADGE_LIST)
    assert all(b.hint or b.secret for b in game.BADGE_LIST)  # secretのみヒント省略可


# ---- 経済の作り込み（探索深化・非クエスト収入）の不変条件 -------------------
def test_release_value_below_tame_cost():
    # 逃がす還元は必ず入手コスト(tame_cost)未満 → 純シンク維持
    for sp in creatures.CATALOG:
        v = game.release_value(sp, game.IV_MAX, game.IV_MAX, game.IV_MAX)  # 最大IVでも
        assert 0 <= v < game.tame_cost(sp), (sp.name, v, game.tame_cost(sp))


def test_explore_cost_increases_with_depth():
    for key in creatures.HABITATS:
        costs = [game.explore_cost(key, d) for d in range(0, game.DEPTH_MAX + 1)]
        assert all(costs[i] <= costs[i + 1] for i in range(len(costs) - 1))
        assert costs[-1] > costs[0]  # 深いほど高い＝sink増


def test_next_depth_reset_and_cap():
    now = 100000
    # 別エリアはリセット
    assert game.next_depth("forest", "cave", 3, now - 10, now) == 0
    # 同エリア・直近は +1
    assert game.next_depth("forest", "forest", 2, now - 10, now) == 3
    # 時間が空くとリセット
    assert game.next_depth("forest", "forest", 5, now - 10000, now) == 0
    # 上限
    assert game.next_depth("forest", "forest", game.DEPTH_MAX, now - 10, now) == game.DEPTH_MAX


def test_login_and_milestones_bounded():
    assert game.max_daily_login() == max(game.LOGIN_REWARDS)
    for d in range(1, 20):
        assert game.WORK_MIN_REWARD <= game.login_reward(d) <= game.max_daily_login()
    # マイルストーンは昇順・正の報酬・有界（最終は全種）
    needs = [n for n, _ in game.DEX_MILESTONES]
    assert needs == sorted(needs)
    assert all(r > 0 for _, r in game.DEX_MILESTONES)
    assert needs[-1] == creatures.TOTAL_SPECIES


def test_weather_is_deterministic_per_day():
    a = game.weather_for("2026-07-24")
    b = game.weather_for("2026-07-24")
    assert a.key == b.key
    assert 0.0 <= a.rare_bonus <= 0.2


def test_habitats_have_pools():
    for key in creatures.HABITATS:
        assert len(creatures.species_in_habitat(key)) >= 1


# ---- 遭遇中の行動（逃走タイマー・観察） -------------------------------------
def test_flee_time_shorter_for_rarer():
    """レアなほど早く逃げる＝希少個体ほど判断を急ぐ必要がある。"""
    order = ["common", "uncommon", "rare", "epic", "legendary"]
    secs = [game.FLEE_SECONDS[r] for r in order]
    assert secs == sorted(secs, reverse=True), secs
    # 観察のペナルティを引いても行動できる余地を必ず残す
    for r in order:
        assert game.FLEE_SECONDS[r] > game.OBSERVE_FLEE_PENALTY * 2, r


def test_every_rarity_has_flee_time():
    for sp in creatures.CATALOG:
        assert game.flee_seconds(sp) > 0, sp.species_id


def test_observe_bonus_is_modest():
    """観察は無料なので、なつき薬（有料）より効果が小さいこと。"""
    assert 0 < game.OBSERVE_TAME_BONUS < 0.20
    for sp in creatures.CATALOG:
        base = game.tame_success_rate(sp)
        observed = game.tame_success_rate(sp, game.OBSERVE_TAME_BONUS)
        assert observed >= base
        assert observed <= 0.95  # 上限は超えない


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
