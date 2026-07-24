"""ゲーム経済の定数とロジック（純粋関数中心・テスト可能）。v2: よあコイン建て。

価格スケールは公式デイリーログイン報酬 1,000 よあコインに合わせている。

設計不変条件（会社が必ず利益／インフラを崩さない）:
- リリーコイン＝よあコイン。クエスト(faucet)のみがリリーコインを発行し、上限が有界。
- 探索・手なずけ・購入・ジェム購入(sink)はリリーコインを消すだけ。
- 会社純資産 Equity は「sink − faucet ＋ ジェム売上 ＋ 出金手数料」で増える。
  → ジェム販売と出金手数料が安全な主利益源。詳細は db.py の会計コメント参照。
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from data.creatures import CATALOG, HABITATS, Species, species_in_habitat

# ---- 個体値 -----------------------------------------------------------------
IV_MAX = 31  # 個体値の最大（HP/ATK/DEF 各 0..31）

# ---- リリーコイン経済（よあコイン建て） ------------------------------------------
# 探索(sink)。EXPLORE_COST はエリア未指定時の名目値／各エリアは HABITATS.base_cost。
EXPLORE_COST = 300
ENCOUNTER_CHANCE = 0.70
MIN_EXPLORE_COST = min(h.base_cost for h in HABITATS.values())

# 手なずけ(sink)
TAME_BASE_COST = 400

# クエスト報酬（リリーコイン額）は quests.py の QuestDef.reward で定義。
# 目標達成型クエストは「達成に必要な消費 > 報酬」を保ち、常に純シンクにする
# （下限見積り: quest_completion_cost_lb）。

WORK_BASE_REWARD = 100      # 無制限クエスト /work の基本報酬
WORK_COOLDOWN = 60          # 秒
WORK_DECAY = 0.80           # 連続実行ごとに報酬 * 0.80
WORK_MIN_REWARD = 20        # 逓減下限
WORK_STREAK_RESET = 1800    # 30分空くと streak リセット


# ---- 個体値・遭遇・手なずけ --------------------------------------------------
def roll_ivs(rng: random.Random | None = None) -> tuple[int, int, int]:
    r = rng or random
    return (r.randint(0, IV_MAX), r.randint(0, IV_MAX), r.randint(0, IV_MAX))


def iv_percent(iv_hp: int, iv_atk: int, iv_def: int) -> float:
    return (iv_hp + iv_atk + iv_def) / (IV_MAX * 3) * 100.0


def effective_stats(sp, iv_hp: int, iv_atk: int, iv_def: int) -> tuple[int, int, int]:
    """種族の基礎ステータスに個体値を上乗せした実効ステータス。"""
    return (sp.base_hp + iv_hp, sp.base_atk + iv_atk, sp.base_def + iv_def)


def power(sp, iv_hp: int, iv_atk: int, iv_def: int) -> int:
    """総合力（実効ステータスの合計）。"""
    h, a, d = effective_stats(sp, iv_hp, iv_atk, iv_def)
    return h + a + d


def iv_grade(pct: float) -> str:
    """個体値の総合%を S〜D のランクに変換（見やすさ用）。"""
    if pct >= 90:
        return "SS"
    if pct >= 75:
        return "S"
    if pct >= 60:
        return "A"
    if pct >= 40:
        return "B"
    if pct >= 20:
        return "C"
    return "D"


def progress_bar(current: int, total: int, width: int = 10) -> str:
    """テキストの進捗バー（■/□）。"""
    if total <= 0:
        return "□" * width
    filled = round(current / total * width)
    filled = max(0, min(width, filled))
    return "■" * filled + "□" * (width - filled)


_RARE_TIERS = ("rare", "epic", "legendary")


def weighted_encounter(
    pool: list[Species] | None = None, rng: random.Random | None = None,
    rare_boost: float = 0.0,
) -> Species:
    """出現重み抽選。rare_boost>0 でレア以上の重みを (1+rare_boost) 倍にする。"""
    r = rng or random
    species = pool if pool is not None else [s for s in CATALOG if not s.limited]
    weights = []
    for s in species:
        w = float(s.encounter_weight)
        if rare_boost and s.rarity in _RARE_TIERS:
            w *= (1.0 + rare_boost)
        weights.append(w)
    return r.choices(species, weights=weights, k=1)[0]


def tame_cost(species: Species) -> int:
    return int(TAME_BASE_COST * species.rarity_info.tame_cost_mult)


def tame_success_rate(species: Species, bonus: float = 0.0) -> float:
    base = species.rarity_info.tame_base_rate
    return max(0.02, min(0.95, base + bonus))


def try_tame(species: Species, bonus: float = 0.0, rng: random.Random | None = None) -> bool:
    r = rng or random
    return r.random() < tame_success_rate(species, bonus)


def try_encounter(bonus: float = 0.0, rng: random.Random | None = None) -> bool:
    r = rng or random
    return r.random() < min(0.95, ENCOUNTER_CHANCE + bonus)


# ---- クエスト報酬（無制限・逓減） -------------------------------------------
def work_reward(streak: int) -> int:
    reward = WORK_BASE_REWARD * (WORK_DECAY ** max(0, streak))
    return max(WORK_MIN_REWARD, int(round(reward)))


def next_streak(last_done_at: int, now: int, prev_streak: int) -> int:
    if now - last_done_at >= WORK_STREAK_RESET:
        return 0
    return prev_streak + 1


# ---- 出金・ジェム（会計） ---------------------------------------------------
def withdraw_split(gross: int, fee_bps: int) -> tuple[int, int]:
    """出金 gross リリーコインを (net_payout, fee) に分解。fee = gross * bps/10000。"""
    fee = gross * fee_bps // 10000
    return gross - fee, fee


# ---- 期待値ヘルパ（経済監査・テスト用） -------------------------------------
def expected_explore_sink() -> float:
    return float(EXPLORE_COST)


def expected_cost_per_catch(species: Species) -> float:
    explore_tries = 1.0 / ENCOUNTER_CHANCE
    tame_tries = 1.0 / tame_success_rate(species)
    return explore_tries * EXPLORE_COST + tame_tries * tame_cost(species)


# 目標達成型クエストを完了するのに最低限かかるリリーコインの下限見積り。
# 最安エリア(MIN_EXPLORE_COST)基準の保守的な下限（実際はこれより高い）。
def quest_completion_cost_lb(event: str, target: int) -> float:
    base = MIN_EXPLORE_COST
    per_event = {
        "explore": base,
        "encounter": base / ENCOUNTER_CHANCE,
        "tame_success": base / ENCOUNTER_CHANCE + TAME_BASE_COST,
    }
    return per_event[event] * target


# ============================================================================
# 探索の深化: エリア・深度・天候
# ============================================================================
DEPTH_MAX = 6
DEPTH_COST_STEP = 0.15      # 深度1ごとに探索コスト +15%（sink増）
DEPTH_RARE_STEP = 0.06      # 深度1ごとにレア遭遇の底上げ
DEPTH_RESET_SECONDS = 900   # 15分空く/別エリアで深度リセット


def explore_cost(habitat_key: str, depth: int) -> int:
    """エリアの基本コスト × 深度倍率。深いほど高コスト。"""
    base = HABITATS[habitat_key].base_cost
    return int(round(base * (1.0 + max(0, depth) * DEPTH_COST_STEP)))


def next_depth(last_habitat: str | None, habitat: str, prev_depth: int,
               last_at: int, now: int) -> int:
    """今回の探索での深度。別エリア or 一定時間経過でリセット、同エリア連続で+1。"""
    if last_habitat != habitat or now - last_at >= DEPTH_RESET_SECONDS:
        return 0
    return min(DEPTH_MAX, prev_depth + 1)


@dataclass(frozen=True)
class Weather:
    key: str
    label: str
    favored_habitat: str | None  # このエリアで遭遇率+
    encounter_bonus: float
    rare_bonus: float            # 全エリアでレア底上げ


WEATHERS = [
    Weather("sunny", "☀️ 晴れ",     "grassland", 0.10, 0.00),
    Weather("rain",  "🌧️ 雨",       "water",     0.10, 0.03),
    Weather("wind",  "🌬️ 風",       "sky",       0.10, 0.03),
    Weather("fog",   "🌫️ 霧",       "cave",      0.08, 0.05),
    Weather("snow",  "🌨️ 雪",       "snow",      0.10, 0.03),
    Weather("aurora","🌌 オーロラ", None,        0.00, 0.08),  # レア日
]


def weather_for(period: str) -> Weather:
    """日付(7時境界の period 文字列)から決定的に today の天候を選ぶ。"""
    idx = sum(ord(c) for c in period) % len(WEATHERS)
    return WEATHERS[idx]


def encounter_bonus(weather: Weather, habitat: str) -> float:
    return weather.encounter_bonus if weather.favored_habitat == habitat else 0.0


# ============================================================================
# クエスト以外のリリーコイン獲得（すべて有界／還元<入手コスト）
# ============================================================================
# 生き物を逃がす際の還元。必ず tame_cost 未満（＝入手コストを下回る＝純シンク維持）。
RELEASE_BASE_FRAC = 0.15
RELEASE_IV_FRAC = 0.15


def release_value(species: Species, iv_hp: int, iv_atk: int, iv_def: int) -> int:
    """逃がしたときのリリーコイン還元。tame_cost の 15〜30%（IV依存）。"""
    pct = iv_percent(iv_hp, iv_atk, iv_def) / 100.0
    frac = RELEASE_BASE_FRAC + RELEASE_IV_FRAC * pct
    return int(tame_cost(species) * frac)


# 図鑑マイルストーン（通常種の収集数 → 一度きり報酬）。合計は有界。
def _dex_milestones():
    from data.creatures import TOTAL_SPECIES
    return [(3, 400), (6, 900), (10, 1800), (15, 3000), (TOTAL_SPECIES, 5000)]


DEX_MILESTONES = _dex_milestones()


def dex_milestones_reached(owned_count: int) -> list[tuple[int, int]]:
    """達成済みのマイルストーン (必要数, 報酬) のリスト。"""
    return [(n, r) for (n, r) in DEX_MILESTONES if owned_count >= n]


# デイリーログインボーナス（1日1回・連続で少し増える・上限固定）。
LOGIN_REWARDS = [100, 120, 140, 160, 180, 200, 300]  # 連続1〜7日目、以降ループ


def login_reward(streak_day: int) -> int:
    """連続ログイン streak_day 日目（1始まり）の報酬。7日周期。"""
    idx = (max(1, streak_day) - 1) % len(LOGIN_REWARDS)
    return LOGIN_REWARDS[idx]


def max_daily_login() -> int:
    return max(LOGIN_REWARDS)


# ============================================================================
# クエストリロール・合体・命名・インベントリ拡張
# ============================================================================
REROLL_COST_COINS = 500      # 無料枠使用後の通常クエストリロール（sink）
MERGE_COST_COINS = 300       # 合体1回のコスト（sink）
NICKNAME_COST_COINS = 100    # なまえ札が無い場合のリリー命名コスト（sink）
CAP_EXPANSION_STEP = 10      # インベントリ拡張アイテム1個で +10 スロット
DEFAULT_CREATURE_CAP = 50
MERGE_IV_BOOST = 2           # 合体時の各IV上昇


def merge_ivs(a: tuple[int, int, int], b: tuple[int, int, int]) -> tuple[int, int, int]:
    """同種2体の合体後IV：各ステータス max(親) ＋ 上昇（IV_MAX上限）。"""
    return tuple(min(IV_MAX, max(x, y) + MERGE_IV_BOOST) for x, y in zip(a, b))


# ============================================================================
# バッジ・称号（条件ベース。stats dict を満たすと獲得。secret=隠し要素）
# ============================================================================
@dataclass(frozen=True)
class Badge:
    id: str
    name: str
    desc: str
    hint: str          # 未獲得時に表示するヒント（secretは非表示）
    secret: bool
    cond: object       # Callable[[dict], bool]


def _build_badges() -> list[Badge]:
    from data.creatures import TOTAL_SPECIES
    n_habitats = len(HABITATS)
    B = Badge
    return [
        # --- 収集 ---
        B("collector_3", "🌱 かけだし収集家", "図鑑を3種集める", "小さな一歩が、図鑑の扉を開く。", False,
          lambda s: s["species"] >= 3),
        B("collector_10", "🏅 コレクター", "図鑑を10種集める", "集める喜びを知る者に、ふさわしい称号がある。", False,
          lambda s: s["species"] >= 10),
        B("collector_all", "👑 図鑑マスター", "全種コンプリート", "すべてを知る者だけがたどり着ける頂がある。", False,
          lambda s: s["species"] >= TOTAL_SPECIES),
        B("world_traveler", "🧭 世界の旅人", "全エリアで捕獲する", "同じ場所に留まる者には、見えない景色がある。", False,
          lambda s: s["habitats"] >= n_habitats),
        # --- レア/厳選 ---
        B("first_legendary", "✨ 伝説との遭遇", "レジェンド級を手なずける", "噂に聞く最強格が、どこかで息をひそめている。", False,
          lambda s: s["legendary"] >= 1),
        B("perfectionist", "💯 完璧主義者", "個体値100%の個体を持つ", "妥協なき厳選の果てに、完璧という言葉がある。", False,
          lambda s: s["perfect"] >= 1),
        # --- 活動 ---
        B("veteran", "🥾 歩き続ける者", "探索を100回おこなう", "歩き続けた者だけが、辿り着ける境地がある。", False,
          lambda s: s["explores"] >= 100),
        B("master_tamer", "🤝 テイマーマスター", "手なずけ50回", "数えきれない出会いが、いつか絆の証になる。", False,
          lambda s: s["tames"] >= 50),
        B("rich", "💰 大富豪", "10万リリーを所持", "貯め込んだ財は、いつか大きな意味を持つ。", False,
          lambda s: s["coins"] >= 100000),
        # --- 隠し要素（secret：ヒントなし・???表示） ---
        B("alchemist", "⚗️ 錬成術師", "合体を10回おこなう", "", True,
          lambda s: s["merges"] >= 10),
        B("liberator", "🕊️ 解き放つ者", "50体を逃がす", "", True,
          lambda s: s["releases"] >= 50),
        B("phantom_hunter", "🌌 幻を追う者", "限定個体を手なずける", "", True,
          lambda s: s["has_limited"]),
        B("loyal", "📅 皆勤賞", "7日連続ログイン", "", True,
          lambda s: s["streak"] >= 7),
        B("hoarder", "🏠 コレクター魂", "生き物を100体所持", "", True,
          lambda s: s["creatures"] >= 100),
        B("depth_diver", "🕳️ 深淵の探索者", "深度6に到達", "", True,
          lambda s: s["max_depth"] >= 6),
    ]


BADGE_LIST: list[Badge] = _build_badges()
BADGES: dict[str, Badge] = {b.id: b for b in BADGE_LIST}


def earned_badges(stats: dict) -> list[str]:
    """statsを満たすバッジIDのリスト。"""
    return [b.id for b in BADGE_LIST if b.cond(stats)]
