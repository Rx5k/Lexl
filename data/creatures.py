"""生き物（動物・キャラクター）の種族カタログ。

各種族はレア度・出現重み・**生息エリア(habitat)** を持つ。探索はエリアを選んで行い、
そのエリアの出現プールから重み抽選される。図鑑(dex)は通常種の収集率で進捗を表示する。

新しい生き物を追加するときはここに1エントリ足すだけ（habitat を指定）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rarity:
    key: str
    label: str
    emoji: str
    tame_base_rate: float   # 手なずけの基本成功率（0-1）
    tame_cost_mult: float   # 手なずけ1回の追加コスト倍率（レアほど高コスト）


RARITIES: dict[str, Rarity] = {
    "common":    Rarity("common",    "ノーマル",     "⚪", 0.75, 1.0),
    "uncommon":  Rarity("uncommon",  "レア",         "🟢", 0.55, 1.4),
    "rare":      Rarity("rare",      "スーパーレア", "🔵", 0.38, 2.0),
    "epic":      Rarity("epic",      "ウルトラレア", "🟣", 0.22, 3.0),
    "legendary": Rarity("legendary", "レジェンド",   "🟡", 0.10, 5.0),
}


# 属性（タイプ）。表示の彩り＋将来のPvP相性の土台。
ELEMENTS: dict[str, tuple[str, str]] = {
    "grass":   ("草", "🌿"),
    "fire":    ("炎", "🔥"),
    "water":   ("水", "💧"),
    "ice":     ("氷", "❄️"),
    "thunder": ("雷", "⚡"),
    "wind":    ("風", "🌪️"),
    "earth":   ("地", "⛰️"),
    "light":   ("光", "✨"),
    "dark":    ("闇", "🌑"),
}


@dataclass(frozen=True)
class Habitat:
    key: str
    name: str
    emoji: str
    base_cost: int          # このエリアの基本探索コスト（リリー）
    unlock: tuple           # ("start",) | ("dex", n) | ("ticket",)


# 生息エリア（探索先）。unlock: start=常時 / ("dex",n)=図鑑n種で解放 / ticket=解放チケット必要
HABITATS: dict[str, Habitat] = {
    "grassland": Habitat("grassland", "草原", "🌾", 250, ("start",)),
    "forest":    Habitat("forest",    "森",   "🌳", 300, ("start",)),
    "water":     Habitat("water",     "水辺", "💧", 350, ("dex", 6)),
    "snow":      Habitat("snow",      "雪山", "❄️", 400, ("dex", 10)),
    "cave":      Habitat("cave",      "洞窟", "🕳️", 400, ("ticket",)),
    "sky":       Habitat("sky",       "空",   "☁️", 500, ("ticket",)),
}
DEFAULT_HABITAT = "grassland"


@dataclass(frozen=True)
class Species:
    species_id: str
    name: str
    rarity: str
    base_hp: int
    base_atk: int
    base_def: int
    encounter_weight: int   # エリア内での相対出現率
    flavor: str
    habitat: str = DEFAULT_HABITAT
    limited: bool = False    # True=通常探索に出ない（限定探索チケット専用）
    element: str = "grass"   # 属性（表示＋将来のPvP相性）

    @property
    def rarity_info(self) -> Rarity:
        return RARITIES[self.rarity]

    @property
    def habitat_info(self) -> Habitat:
        return HABITATS.get(self.habitat, HABITATS[DEFAULT_HABITAT])

    @property
    def element_info(self) -> tuple[str, str]:
        return ELEMENTS.get(self.element, ELEMENTS["grass"])

    @property
    def base_total(self) -> int:
        return self.base_hp + self.base_atk + self.base_def

    @property
    def dex_no(self) -> int:
        return _DEX_NO.get(self.species_id, 0)


# --- カタログ本体 -----------------------------------------------------------
CATALOG: list[Species] = [
    # 草原
    Species("kusa_usagi", "クサウサギ", "common",   26, 10, 12, 100, "草を食む臆病なうさぎ。臆病だが足がとても速い。", "grassland", element="grass"),
    Species("hi_nezumi",  "ヒネズミ",   "common",   24, 14,  8,  90, "ちょろちょろ動く素早い鼠。尾の先に小さな火を灯す。", "grassland", element="fire"),
    Species("hana_hitsuji","ハナヒツジ","uncommon", 36, 14, 18,  50, "花畑をのんびり歩く羊。毛には花の香りが染みつく。", "grassland", element="grass"),
    # 森
    Species("mori_neko",  "モリネコ",   "common",   30, 12, 10, 100, "森でよく見かける人懐こい猫。気に入った相手を離さない。", "forest", element="grass"),
    Species("ki_zaru",    "キザル",     "common",   28, 15, 10,  90, "木々を軽やかに渡る猿。群れで木の実を分け合う。", "forest", element="grass"),
    Species("mori_ou",    "モリオウ",   "rare",     54, 22, 26,  20, "森の主とされる大鹿。立派な角は森の年輪を刻む。", "forest", element="grass"),
    Species("gen_hukurou","ゲンフクロウ","epic",     60, 30, 34,   8, "時を見通すという梟。夜にだけ真の姿を現す。", "forest", element="light"),
    # 水辺
    Species("ike_kaeru",  "イケガエル", "common",   28, 11, 11, 100, "池のほとりで鳴いている蛙。鳴き声で天気を告げる。", "water", element="water"),
    Species("mizu_kame",  "ミズガメ",   "common",   34, 10, 18,  85, "のんびり泳ぐ穏やかな亀。甲羅は硬く長寿の象徴。", "water", element="water"),
    Species("umi_hebi",   "ウミヘビ",   "rare",     48, 24, 22,  20, "深い水底に潜む大蛇。渦を巻いて獲物を捕らえる。", "water", element="water"),
    Species("kyo_kujira", "キョウクジラ","legendary",90, 40, 46,   2, "水を割って現れる巨鯨。その歌は海全体に響く。", "water", element="water"),
    # 雪山
    Species("yuki_gitsune","ユキギツネ","uncommon", 32, 20, 14,  50, "雪原に棲む白い狐。吹雪に紛れて姿を消す。", "snow", element="ice"),
    Species("koori_kuma", "コオリグマ", "uncommon", 44, 22, 20,  45, "氷原を悠然と歩く熊。一撃で氷を砕く豪腕。", "snow", element="ice"),
    Species("sei_kirin",  "セイキリン", "legendary",80, 44, 40,   3, "現れると吉兆とされる麒麟。足跡から花が咲く。", "snow", element="light"),
    # 洞窟
    Species("yami_koumori","ヤミコウモリ","common",  24, 16,  9,  95, "洞窟の闇に潜む蝙蝠。超音波で暗闇を見通す。", "cave", element="dark"),
    Species("iwa_inu",    "イワイヌ",   "uncommon", 40, 16, 18,  50, "岩場を守る頑丈な犬。岩と見分けがつかない。", "cave", element="earth"),
    Species("honoo_ryu",  "ホノオリュウ","rare",     50, 26, 20,  20, "小さな炎を吐く幼竜。成長すると山をも焼くという。", "cave", element="fire"),
    # 空
    Species("sora_washi", "ソラワシ",   "common",   28, 16, 10,  90, "高空を悠々と舞う鷲。地上の獲物を見逃さない。", "sky", element="wind"),
    Species("kaze_taka",  "カゼタカ",   "uncommon", 34, 18, 12,  48, "風に乗って舞う鷹。上昇気流を自在に操る。", "sky", element="wind"),
    Species("rai_shishi", "ライシシ",   "epic",     64, 34, 28,   8, "雷をまとう伝説の獅子。咆哮とともに稲妻が走る。", "sky", element="thunder"),
    # ---- 限定個体（ジェムの限定探索でのみ出現） ----
    Species("gen_hououou", "ゲンホウオウ", "legendary", 95, 48, 44, 3,
            "限定：数百年に一度だけ姿を見せる鳳凰。焼けても灰から蘇る。", "sky", limited=True, element="fire"),
    Species("sora_tatsu",  "ソラタツ",     "epic",      70, 40, 36, 5,
            "限定：天候を操るという幻の龍。雲を纏い空を統べる。", "sky", limited=True, element="wind"),
]

_DEX_NO: dict[str, int] = {s.species_id: i + 1 for i, s in enumerate(CATALOG)}
BY_ID: dict[str, Species] = {s.species_id: s for s in CATALOG}

NORMAL_SPECIES: list[Species] = [s for s in CATALOG if not s.limited]
LIMITED_SPECIES: list[Species] = [s for s in CATALOG if s.limited]
TOTAL_SPECIES = len(NORMAL_SPECIES)
TOTAL_ALL_SPECIES = len(CATALOG)


def get(species_id: str) -> Species | None:
    return BY_ID.get(species_id)


def species_in_habitat(habitat: str) -> list[Species]:
    """指定エリアの通常出現プール。"""
    return [s for s in NORMAL_SPECIES if s.habitat == habitat]
