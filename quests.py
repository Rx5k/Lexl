"""クエスト定義と、Cog横断の進捗イベント記録（v4: テンプレ＋ランダム目標）。

- **テンプレ制**: デイリー30種・通常50種の「テンプレート」を用意。割当時に難易度帯の
  範囲から**目標回数(N)をランダムに決定**し、Nに比例した報酬を自動計算する。
- **デイリー(3枠)**: 毎朝7:00(JST)更新・やさしめ。**その日のテンプレ3種はperiodから
  決定的に選ばれる**（全員同じ・目標もperiodシード）。進捗は db.quest_progress。
- **通常(3枠)**: 各ユーザーにテンプレを難易度重みで割当（db.user_quests、目標を保存）。
  進捗は完了/リロールまで継続。完了→受取→枠は新クエストで補充。1日1回無料リロール。
- 報酬は「約8割リリー＋約2割アイテム」。難易度・目標回数で増減。ジェムは配らない。
- どのクエストも「達成に必要な消費 > 報酬」を維持（game.quest_completion_cost_lb）。
  REWARD_PER_ACTION < 各イベントの最低消費/回 なので常に純シンク。
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import game

JST = timezone(timedelta(hours=9))
RESET_HOUR = 7

NORMAL_SLOTS = 3   # 通常クエストの枠数
DAILY_SLOTS = 3    # デイリークエストの枠数

# ジェム1個のリリー建て価値。報酬の過剰配布を防ぐため、実売価より高めに見積もる。
GEM_VALUE_COINS = 200

# アイテムの価値（報酬計算・total_value 用）。ショップの実売価と一致させること。
# ジェム建ての商品は GEM_VALUE_COINS 換算（＝タダで配る量を絞る）。
REWARD_ITEM_VALUE = {
    "bait": 200,                             # ショップ 200 リリー
    "charm": 600,                            # ショップ 600 リリー
    "name_tag": 300,                         # ショップ 300 リリー
    "reroll_ticket": 400,                    # ショップ 400 リリー
    "gold_bait": 2 * GEM_VALUE_COINS,        # ショップ 2 ジェム
    "limited_ticket": 5 * GEM_VALUE_COINS,   # ショップ 5 ジェム
}

# 1クエストで配れる個数の上限。価値が高い・希少なものほど少なく。
# これが無いと目標回数に比例して「なまえ札×40」のような非現実的な数量になる。
REWARD_ITEM_MAX_QTY = {
    "bait": 5,             # 消耗品・安価
    "charm": 3,
    "name_tag": 2,
    "reroll_ticket": 2,
    "gold_bait": 2,        # ジェム建て
    "limited_ticket": 1,   # ジェム建ての最上位。1個までの特別枠
}

# 1回(1アクション)あたりの報酬リリー。各イベントの「最低消費/回」より必ず小さい
# （explore≥250, encounter≥357, tame_success≥757）＝常に純シンク。
REWARD_PER_ACTION = {"explore": 150, "encounter": 200, "tame_success": 430}

# 難易度帯: (目標の最小, 最大, 選ばれる重み)
NORMAL_TIERS = {
    "やさしい":   (5, 12, 45),
    "ふつう":     (13, 28, 38),
    "むずかしい": (30, 60, 17),
}
DAILY_TIERS = {
    "やさしい": (2, 5, 70),
    "ふつう":   (6, 12, 30),
}

DESC_FMT = {
    "explore": "探索を{n}回おこなう",
    "encounter": "生き物に{n}回遭遇する",
    "tame_success": "手なずけに{n}回成功する",
}


@dataclass(frozen=True)
class Reward:
    """クエスト報酬。リリーコインとアイテム（複数種可）の組み合わせ。

    items は ((アイテムID, 個数), ...)。「餌×3 と なつき薬×1 と 1,200リリー」のように
    複数を組み合わせられる。
    """
    coins: int
    items: tuple[tuple[str, int], ...] = ()

    @property
    def item_value(self) -> int:
        return sum(REWARD_ITEM_VALUE.get(iid, 0) * qty for iid, qty in self.items)

    @property
    def total_value(self) -> int:
        return self.coins + self.item_value


@dataclass(frozen=True)
class QuestTemplate:
    tid: str
    event: str          # explore / encounter / tame_success
    title: str
    tier: str           # 難易度（やさしい/ふつう/むずかしい）
    kind: str           # 'daily' | 'normal'
    items: tuple[str, ...] = ()   # 報酬アイテム（先頭から優先配分・残りはリリー）


@dataclass(frozen=True)
class QuestDef:
    """テンプレ＋確定した目標回数＝実際に遊ぶ1クエスト。"""
    quest_id: str
    title: str
    desc: str
    event: str
    target: int
    reward: Reward
    kind: str
    difficulty: str = ""


def reward_for(event: str, target: int, items: tuple[str, ...]) -> Reward:
    """目標回数に比例した価値(base)を、アイテム（個数上限つき）と残りリリーに配分する。

    アイテムは REWARD_ITEM_MAX_QTY を超えて配らない。上限で余った価値はリリーで支払う
    ので、報酬の総価値は base のままブレない（＝「消費 > 報酬」の不変条件を維持）。
    """
    base = target * REWARD_PER_ACTION[event]
    granted: list[tuple[str, int]] = []
    remaining = base
    for iid in items:
        unit = REWARD_ITEM_VALUE[iid]
        qty = min(REWARD_ITEM_MAX_QTY[iid], remaining // unit)
        if qty > 0:
            granted.append((iid, qty))
            remaining -= unit * qty
    if not granted:
        # 目標が小さくアイテム1個分の価値にも満たない → 全額リリーで支払う
        return Reward(base)
    return Reward(remaining, tuple(granted))


def _tiers_for(kind: str) -> dict:
    return DAILY_TIERS if kind == "daily" else NORMAL_TIERS


def quest_from(tid: str, target: int) -> QuestDef | None:
    """テンプレIDと確定目標から実クエストを組み立てる（表示・受取用）。"""
    t = TEMPLATE_BY_ID.get(tid)
    if t is None:
        return None
    return QuestDef(tid, t.title, DESC_FMT[t.event].format(n=target), t.event,
                    target, reward_for(t.event, target, t.items), t.kind, t.tier)


def _roll_target(t: QuestTemplate, rng: random.Random) -> int:
    lo, hi, _ = _tiers_for(t.kind)[t.tier]
    return rng.randint(lo, hi)


def _instantiate(t: QuestTemplate, rng: random.Random) -> QuestDef:
    return quest_from(t.tid, _roll_target(t, rng))


# ---- テンプレート定義（デイリー30・通常50） --------------------------------
# (event, title, tier, item)。desc は DESC_FMT から自動生成。item指定=報酬アイテム。
_DAILY_SPEC = [
    # explore（12）
    ("explore", "散歩", "やさしい", None),
    ("explore", "朝の探索", "やさしい", None),
    ("explore", "野歩き", "やさしい", None),
    ("explore", "見回り", "やさしい", None),
    ("explore", "気ままな散策", "やさしい", None),
    ("explore", "日課の探索", "やさしい", "bait"),
    ("explore", "近所の探索", "やさしい", None),
    ("explore", "探索の習慣", "やさしい", None),
    ("explore", "小さな冒険", "ふつう", None),
    ("explore", "遠出", "ふつう", None),
    ("explore", "フィールドワーク", "ふつう", "bait"),
    ("explore", "巡検", "ふつう", None),
    # encounter（9）
    ("encounter", "出会い", "やさしい", None),
    ("encounter", "ひと目会う", "やさしい", None),
    ("encounter", "観察入門", "やさしい", "bait"),
    ("encounter", "生き物観察", "やさしい", None),
    ("encounter", "気配を追う", "やさしい", None),
    ("encounter", "観察日記", "やさしい", None),
    ("encounter", "バードウォッチング", "ふつう", None),
    ("encounter", "追跡", "ふつう", None),
    ("encounter", "生態調査", "ふつう", "charm"),
    # tame_success（9）
    ("tame_success", "はじめての友達", "やさしい", None),
    ("tame_success", "なつき入門", "やさしい", None),
    ("tame_success", "ふれあい", "やさしい", None),
    ("tame_success", "なかよし", "やさしい", None),
    ("tame_success", "絆づくり", "やさしい", "charm"),
    ("tame_success", "心を通わせる", "やさしい", None),
    ("tame_success", "調教入門", "ふつう", None),
    ("tame_success", "手なずけの技", "ふつう", None),
    ("tame_success", "テイム", "ふつう", None),
]

# 難易度が上がるほど「アイテム＋リリー」の複合報酬になる。探索系は餌、手なずけ系は
# なつき薬…とテーマを合わせ、最上位だけジェム建ての品（金の餌・限定チケット）を1個。
_NORMAL_SPEC = [
    # explore（18）
    ("explore", "探索行", "やさしい", None),
    ("explore", "野外調査", "やさしい", None),
    ("explore", "地図を埋めて", "やさしい", "bait"),
    ("explore", "漫遊", "やさしい", None),
    ("explore", "踏査", "やさしい", "bait"),
    ("explore", "遠征", "ふつう", "bait"),
    ("explore", "探検", "ふつう", ("bait", "charm")),
    ("explore", "縦断の旅", "ふつう", None),
    ("explore", "跋渉", "ふつう", "reroll_ticket"),
    ("explore", "踏破", "ふつう", ("gold_bait", "bait")),
    ("explore", "秘境へ", "ふつう", ("charm", "bait")),
    ("explore", "大遠征", "むずかしい", ("gold_bait", "bait")),
    ("explore", "未踏の地", "むずかしい", ("bait", "charm")),
    ("explore", "大踏破", "むずかしい", ("gold_bait", "bait")),
    ("explore", "極地探索", "むずかしい", ("gold_bait", "charm")),
    ("explore", "果てなき道", "むずかしい", ("bait", "charm")),
    ("explore", "世界の果て", "むずかしい", ("limited_ticket", "charm")),
    ("explore", "探究者", "むずかしい", ("reroll_ticket", "bait")),
    # encounter（16）
    ("encounter", "観察", "やさしい", None),
    ("encounter", "発見の喜び", "やさしい", "bait"),
    ("encounter", "目撃", "やさしい", "bait"),
    ("encounter", "探し物", "やさしい", None),
    ("encounter", "観測", "ふつう", "bait"),
    ("encounter", "尾行", "ふつう", ("charm", "bait")),
    ("encounter", "追跡調査", "ふつう", ("bait", "charm")),
    ("encounter", "生き物図鑑", "ふつう", "charm"),
    ("encounter", "まなざし", "ふつう", "name_tag"),
    ("encounter", "記録者", "ふつう", ("charm", "bait")),
    ("encounter", "大観察", "むずかしい", ("charm", "bait")),
    ("encounter", "たくさんの出会い", "むずかしい", ("bait", "charm")),
    ("encounter", "観察の達人", "むずかしい", ("reroll_ticket", "bait")),
    ("encounter", "見晴らし", "むずかしい", ("name_tag", "bait")),
    ("encounter", "遭遇録", "むずかしい", ("charm", "bait")),
    ("encounter", "生態の探究", "むずかしい", ("name_tag", "charm")),
    # tame_success（16）
    ("tame_success", "手なずけ", "やさしい", None),
    ("tame_success", "なかよし作戦", "やさしい", "charm"),
    ("tame_success", "信頼を得る", "やさしい", "bait"),
    ("tame_success", "相棒集め", "やさしい", None),
    ("tame_success", "調教", "ふつう", "charm"),
    ("tame_success", "手練", "ふつう", ("charm", "bait")),
    ("tame_success", "絆", "ふつう", "charm"),
    ("tame_success", "心の絆", "ふつう", ("charm", "bait")),
    ("tame_success", "仲間集め", "ふつう", ("charm", "bait")),
    ("tame_success", "信頼の輪", "ふつう", "name_tag"),
    ("tame_success", "熟練の調教", "むずかしい", ("charm", "bait")),
    ("tame_success", "名テイマー", "むずかしい", ("reroll_ticket", "charm")),
    ("tame_success", "超調教", "むずかしい", ("reroll_ticket", "charm")),
    ("tame_success", "手なずけ名人", "むずかしい", ("charm", "bait")),
    ("tame_success", "絆の証", "むずかしい", ("name_tag", "charm")),
    ("tame_success", "テイマーの道", "むずかしい", ("limited_ticket", "charm")),
]


def _build(kind: str, spec: list) -> list[QuestTemplate]:
    """spec の item は None / "アイテムID" / ("ID1", "ID2") のいずれでも書ける。"""
    p = kind[0]
    out = []
    for i, (ev, title, tier, item) in enumerate(spec):
        if item is None:
            items: tuple[str, ...] = ()
        elif isinstance(item, str):
            items = (item,)
        else:
            items = tuple(item)
        out.append(QuestTemplate(f"{p}{i:02d}", ev, title, tier, kind, items))
    return out


DAILY_TEMPLATES: list[QuestTemplate] = _build("daily", _DAILY_SPEC)
NORMAL_TEMPLATES: list[QuestTemplate] = _build("normal", _NORMAL_SPEC)
ALL_TEMPLATES: list[QuestTemplate] = DAILY_TEMPLATES + NORMAL_TEMPLATES
TEMPLATE_BY_ID: dict[str, QuestTemplate] = {t.tid: t for t in ALL_TEMPLATES}


# ---- 時刻・period ----------------------------------------------------------
def daily_period(now: datetime | None = None) -> str:
    dt = (now.astimezone(JST) if now else datetime.now(JST)) - timedelta(hours=RESET_HOUR)
    return dt.strftime("%Y-%m-%d")


# ---- 選抜（難易度重み付き） ------------------------------------------------
def _weighted_pick_distinct(templates: list[QuestTemplate], k: int,
                            rng: random.Random, exclude: set[str] | None = None) -> list[QuestTemplate]:
    pool = [t for t in templates if not exclude or t.tid not in exclude]
    if not pool:
        pool = list(templates)
    chosen: list[QuestTemplate] = []
    pool = list(pool)
    for _ in range(min(k, len(pool))):
        weights = [_tiers_for(t.kind)[t.tier][2] for t in pool]
        t = rng.choices(pool, weights=weights, k=1)[0]
        chosen.append(t)
        pool.remove(t)
    return chosen


def daily_quests_for(period: str | None = None) -> list[QuestDef]:
    """その日(period)のデイリー3種。periodから決定的（全員同じ・目標もシード）。"""
    period = period or daily_period()
    seed = sum(ord(c) for c in period)
    rng = random.Random(seed)
    picks = _weighted_pick_distinct(DAILY_TEMPLATES, DAILY_SLOTS, rng)
    return [_instantiate(t, rng) for t in picks]


def roll_normal(exclude: set[str], rng: random.Random | None = None) -> QuestDef:
    """通常クエストを1つ抽選（難易度重み＋目標ランダム）。"""
    r = rng or random
    t = _weighted_pick_distinct(NORMAL_TEMPLATES, 1, r, exclude)[0]
    return _instantiate(t, r)


# ---- 通常クエスト（プール制・per-userスロット・目標をDB保存） ---------------
async def ensure_normal_quests(db, user_id: int) -> None:
    rows = await db.get_user_quests(user_id)
    active = {r["quest_id"] for r in rows if not r["claimed"]}
    have_slots = {r["slot"] for r in rows}
    for slot in range(NORMAL_SLOTS):
        if slot not in have_slots:
            q = roll_normal(active)
            active.add(q.quest_id)
            await db.upsert_user_quest(user_id, slot, q.quest_id, q.target, 0, 0)


async def refill_slot(db, user_id: int, slot: int) -> None:
    rows = await db.get_user_quests(user_id)
    active = {r["quest_id"] for r in rows if not r["claimed"] and r["slot"] != slot}
    q = roll_normal(active)
    await db.upsert_user_quest(user_id, slot, q.quest_id, q.target, 0, 0)


async def bump_normal(db, user_id: int, event: str, amount: int) -> list[QuestDef]:
    completed = []
    for r in await db.get_user_quests(user_id):
        if r["claimed"]:
            continue
        q = quest_from(r["quest_id"], r["target"])
        if q is None or q.event != event:
            continue
        before = r["progress"]
        new = min(q.target, before + amount)
        if new != before:
            await db.upsert_user_quest(user_id, r["slot"], q.quest_id, q.target, new, 0)
        if before < q.target <= new:
            completed.append(q)
    return completed


async def record_event(db, user_id: int, event: str, amount: int = 1) -> list[QuestDef]:
    """デイリー(period)＋通常(枠)の進捗を加算。達成した定義を返す。"""
    completed: list[QuestDef] = []
    period = daily_period()
    for q in daily_quests_for(period):
        if q.event != event:
            continue
        _, done = await db.bump_quest(user_id, q.quest_id, period, q.target, amount)
        if done:
            completed.append(q)
    completed += await bump_normal(db, user_id, event, amount)
    return completed
