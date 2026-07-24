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

# アイテムの価値（報酬計算・total_value 用）。ジェムは配らない＝ここに載る物のみ。
REWARD_ITEM_VALUE = {
    "bait": 200, "charm": 600, "gold_bait": 400,
    "limited_ticket": 1000, "reroll_ticket": 800, "name_tag": 300,
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
    coins: int
    item_id: str | None = None
    item_qty: int = 0

    @property
    def item_value(self) -> int:
        if not self.item_id:
            return 0
        return REWARD_ITEM_VALUE.get(self.item_id, 0) * self.item_qty

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
    item: str | None = None   # 設定時は報酬がこのアイテム（それ以外はリリー）


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


def reward_for(event: str, target: int, item: str | None) -> Reward:
    """目標回数に比例した報酬。item指定時はアイテム、なければリリー。"""
    base = target * REWARD_PER_ACTION[event]
    if item:
        unit = REWARD_ITEM_VALUE[item]
        qty = max(1, base // unit)   # 切り捨て → 価値は base 以下 → 常に消費未満
        return Reward(0, item, qty)
    return Reward(base)


def _tiers_for(kind: str) -> dict:
    return DAILY_TIERS if kind == "daily" else NORMAL_TIERS


def quest_from(tid: str, target: int) -> QuestDef | None:
    """テンプレIDと確定目標から実クエストを組み立てる（表示・受取用）。"""
    t = TEMPLATE_BY_ID.get(tid)
    if t is None:
        return None
    return QuestDef(tid, t.title, DESC_FMT[t.event].format(n=target), t.event,
                    target, reward_for(t.event, target, t.item), t.kind, t.tier)


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

_NORMAL_SPEC = [
    # explore（18）
    ("explore", "探索行", "やさしい", None),
    ("explore", "野外調査", "やさしい", None),
    ("explore", "地図を埋めて", "やさしい", "bait"),
    ("explore", "漫遊", "やさしい", None),
    ("explore", "踏査", "やさしい", None),
    ("explore", "遠征", "ふつう", None),
    ("explore", "探検", "ふつう", None),
    ("explore", "縦断の旅", "ふつう", None),
    ("explore", "跋渉", "ふつう", "reroll_ticket"),
    ("explore", "踏破", "ふつう", None),
    ("explore", "秘境へ", "ふつう", None),
    ("explore", "大遠征", "むずかしい", None),
    ("explore", "未踏の地", "むずかしい", None),
    ("explore", "大踏破", "むずかしい", "gold_bait"),
    ("explore", "極地探索", "むずかしい", None),
    ("explore", "果てなき道", "むずかしい", None),
    ("explore", "世界の果て", "むずかしい", "limited_ticket"),
    ("explore", "探究者", "むずかしい", None),
    # encounter（16）
    ("encounter", "観察", "やさしい", None),
    ("encounter", "発見の喜び", "やさしい", None),
    ("encounter", "目撃", "やさしい", "bait"),
    ("encounter", "探し物", "やさしい", None),
    ("encounter", "観測", "ふつう", None),
    ("encounter", "尾行", "ふつう", None),
    ("encounter", "追跡調査", "ふつう", None),
    ("encounter", "生き物図鑑", "ふつう", "charm"),
    ("encounter", "まなざし", "ふつう", None),
    ("encounter", "記録者", "ふつう", None),
    ("encounter", "大観察", "むずかしい", None),
    ("encounter", "たくさんの出会い", "むずかしい", None),
    ("encounter", "観察の達人", "むずかしい", "reroll_ticket"),
    ("encounter", "見晴らし", "むずかしい", None),
    ("encounter", "遭遇録", "むずかしい", None),
    ("encounter", "生態の探究", "むずかしい", "name_tag"),
    # tame_success（16）
    ("tame_success", "手なずけ", "やさしい", None),
    ("tame_success", "なかよし作戦", "やさしい", None),
    ("tame_success", "信頼を得る", "やさしい", "bait"),
    ("tame_success", "相棒集め", "やさしい", None),
    ("tame_success", "調教", "ふつう", None),
    ("tame_success", "手練", "ふつう", None),
    ("tame_success", "絆", "ふつう", None),
    ("tame_success", "心の絆", "ふつう", "charm"),
    ("tame_success", "仲間集め", "ふつう", None),
    ("tame_success", "信頼の輪", "ふつう", None),
    ("tame_success", "熟練の調教", "むずかしい", None),
    ("tame_success", "名テイマー", "むずかしい", None),
    ("tame_success", "超調教", "むずかしい", "reroll_ticket"),
    ("tame_success", "手なずけ名人", "むずかしい", None),
    ("tame_success", "絆の証", "むずかしい", None),
    ("tame_success", "テイマーの道", "むずかしい", "limited_ticket"),
]


def _build(kind: str, spec: list) -> list[QuestTemplate]:
    p = kind[0]
    return [QuestTemplate(f"{p}{i:02d}", ev, title, tier, kind, item)
            for i, (ev, title, tier, item) in enumerate(spec)]


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
