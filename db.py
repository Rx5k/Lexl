"""SQLite（aiosqlite）による内部台帳（v2: よあコイン建てモデル）。

通貨:
- リリーコイン = よあコイン建て（1:1入出金）。発行済みリリーコインは全額が会社の「負債」。
- ジェム = 課金通貨（非換金）。リリーコインで購入し限定アイテム等に使う。

会計（会社の純資産 Equity = Reserve − Liability）:
- Reserve(準備金 R) … 会社が実際に保有するよあコイン。company テーブルで管理。
    入金で +amount、出金の払い出しで −net_payout。
- Liability(負債 L) … 発行済みリリーコイン総額 = Σ users.coins。
- Equity(純資産 E) … R − L。目標は 30,000 → 50,000。

各操作の Equity への効果（すべて transactions に記録）:
- 入金:      R+X, L+X → E 不変（元本受け入れ）
- クエスト:  L+F      → E−F（faucet, 準備金から持ち出し）
- ゲーム消費: L−S      → E+S（sink, 会社純増）
- ジェム購入: L−G      → E+G（純利益。ジェムは非換金）
- 出金:      L−gross, R−net_payout → E+fee（手数料が利益）
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS company (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    reserve INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    coins      INTEGER NOT NULL DEFAULT 0,   -- リリーコイン（ゲーム内通貨）
    gems       INTEGER NOT NULL DEFAULT 0,   -- ジェム（課金通貨・非換金）
    yc         INTEGER NOT NULL DEFAULT 0,   -- よあコイン残高（換金可能なキャッシュ）
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS user_creatures (
    instance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    species_id  TEXT    NOT NULL,
    iv_hp       INTEGER NOT NULL,
    iv_atk      INTEGER NOT NULL,
    iv_def      INTEGER NOT NULL,
    nickname    TEXT,
    caught_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_creatures_user ON user_creatures(user_id);

CREATE TABLE IF NOT EXISTS quest_cooldowns (
    user_id      INTEGER NOT NULL,
    quest_id     TEXT    NOT NULL,
    last_done_at INTEGER NOT NULL,
    streak       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, quest_id)
);

CREATE TABLE IF NOT EXISTS quest_progress (
    user_id  INTEGER NOT NULL,
    quest_id TEXT    NOT NULL,
    period   TEXT    NOT NULL,   -- 7時境界の日付 'YYYY-MM-DD'
    progress INTEGER NOT NULL DEFAULT 0,
    target   INTEGER NOT NULL,
    claimed  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, quest_id, period)
);

CREATE TABLE IF NOT EXISTS user_items (
    user_id INTEGER NOT NULL,
    item_id TEXT    NOT NULL,
    qty     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, item_id)
);

CREATE TABLE IF NOT EXISTS withdraw_state (
    user_id      INTEGER PRIMARY KEY,
    last_at      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS withdraw_requests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    gross      INTEGER NOT NULL,
    net_payout INTEGER NOT NULL,
    fee        INTEGER NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'pending',  -- pending|paid
    ts         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL,
    currency TEXT    NOT NULL,   -- 'coins' | 'gems' | 'reserve'
    amount   INTEGER NOT NULL,
    reason   TEXT    NOT NULL,
    ts       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tx_reason ON transactions(reason);

CREATE TABLE IF NOT EXISTS payment_cursor (
    id     INTEGER PRIMARY KEY CHECK (id = 1),
    cursor INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS processed_payments (
    payment_id INTEGER PRIMARY KEY,
    ts         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS explore_state (
    user_id INTEGER PRIMARY KEY,
    habitat TEXT    NOT NULL,
    depth   INTEGER NOT NULL DEFAULT 0,
    last_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS milestone_claims (
    user_id   INTEGER NOT NULL,
    milestone INTEGER NOT NULL,
    PRIMARY KEY (user_id, milestone)
);

CREATE TABLE IF NOT EXISTS login_state (
    user_id     INTEGER PRIMARY KEY,
    last_period TEXT,
    streak      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS unlocked_areas (
    user_id INTEGER NOT NULL,
    habitat TEXT    NOT NULL,
    PRIMARY KEY (user_id, habitat)
);

CREATE TABLE IF NOT EXISTS user_quests (
    user_id  INTEGER NOT NULL,
    slot     INTEGER NOT NULL,
    quest_id TEXT    NOT NULL,
    target   INTEGER NOT NULL DEFAULT 0,
    progress INTEGER NOT NULL DEFAULT 0,
    claimed  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, slot)
);

CREATE TABLE IF NOT EXISTS quest_reroll (
    user_id   INTEGER PRIMARY KEY,
    period    TEXT,
    free_used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS user_limits (
    user_id     INTEGER PRIMARY KEY,
    creature_cap INTEGER NOT NULL DEFAULT 50
);

CREATE TABLE IF NOT EXISTS user_badges (
    user_id  INTEGER NOT NULL,
    badge_id TEXT    NOT NULL,
    got_at   INTEGER NOT NULL,
    PRIMARY KEY (user_id, badge_id)
);

CREATE TABLE IF NOT EXISTS user_stats (
    user_id   INTEGER PRIMARY KEY,
    explores  INTEGER NOT NULL DEFAULT 0,
    tames     INTEGER NOT NULL DEFAULT 0,
    merges    INTEGER NOT NULL DEFAULT 0,
    releases  INTEGER NOT NULL DEFAULT 0,
    max_depth INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass
class Balance:
    coins: int      # リリーコイン（ゲーム内通貨）
    gems: int       # ジェム（課金通貨）
    yc: int = 0     # よあコイン残高（換金可能なキャッシュ）


@dataclass
class WithdrawResult:
    ok: bool
    reason: str = ""          # 失敗理由（"insufficient_coins"|"below_min"|"cooldown"|"reserve_floor"）
    net_payout: int = 0
    fee: int = 0
    queued: bool = False      # payout未対応で申請キューに積んだ場合 True


def _now() -> int:
    return int(time.time())


class _ReentrantLock:
    """同一タスク内での再入を許す asyncio ロック。

    aiosqlite の接続は全ユーザーで1本を共有しており、commit()/rollback() は
    「その接続で未確定の全変更」に効く。素の asyncio.Lock だと、書き込みメソッドが
    別の書き込みメソッドを内部で呼ぶ箇所（例: deposit_yoacoin → ensure_user）で
    デッドロックするため、同じタスクからの再入だけ許可する。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._depth = 0

    @property
    def depth(self) -> int:
        return self._depth

    async def __aenter__(self) -> "_ReentrantLock":
        task = asyncio.current_task()
        if self._owner is not None and self._owner is task:
            self._depth += 1
            return self
        await self._lock.acquire()
        self._owner = task
        self._depth = 1
        return self

    async def __aexit__(self, *exc) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()


class Database:
    def __init__(self, path: str, start_reserve: int = 0):
        self.path = path
        self.start_reserve = start_reserve
        self._conn: aiosqlite.Connection | None = None
        # 書き込みトランザクションを直列化するロック（_tx を参照）
        self._wlock = _ReentrantLock()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() が呼ばれていません")
        return self._conn

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        # 既存DB用マイグレーション: users.yc 列が無ければ追加
        async with self._conn.execute("PRAGMA table_info(users)") as cur:
            cols = [r["name"] for r in await cur.fetchall()]
        if "yc" not in cols:
            await self._conn.execute(
                "ALTER TABLE users ADD COLUMN yc INTEGER NOT NULL DEFAULT 0"
            )
        # 既存DB用マイグレーション: user_quests.target 列が無ければ追加
        async with self._conn.execute("PRAGMA table_info(user_quests)") as cur:
            uq_cols = [r["name"] for r in await cur.fetchall()]
        if "target" not in uq_cols:
            await self._conn.execute(
                "ALTER TABLE user_quests ADD COLUMN target INTEGER NOT NULL DEFAULT 0"
            )
        await self._conn.execute(
            "INSERT OR IGNORE INTO payment_cursor(id, cursor) VALUES (1, 0)"
        )
        await self._conn.execute(
            "INSERT OR IGNORE INTO company(id, reserve) VALUES (1, ?)",
            (self.start_reserve,),
        )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def _tx(self):
        """書き込みトランザクション。ロックを保持したまま最後に commit する。

        接続は全ユーザーで共有のため、execute と commit の間で他タスクが割り込むと
        他人の未確定の変更を巻き込んで commit / rollback してしまう（残高が戻る＝
        コイン増殖）。ここでロックを保持することで、commit / rollback が必ず
        「自分の変更だけ」に効くことを保証する。ネストした呼び出しでは最も外側の
        ブロックだけが確定させる。
        """
        async with self._wlock as lock:
            outermost = lock.depth == 1
            try:
                yield
            except BaseException:
                if outermost:
                    await self.conn.rollback()
                raise
            if outermost:
                await self.conn.commit()

    async def _log(self, user_id: int, currency: str, amount: int, reason: str) -> None:
        await self.conn.execute(
            "INSERT INTO transactions(user_id, currency, amount, reason, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, currency, amount, reason, _now()),
        )

    # ---- company reserve --------------------------------------------------
    async def get_reserve(self) -> int:
        async with self.conn.execute("SELECT reserve FROM company WHERE id = 1") as cur:
            row = await cur.fetchone()
        return row["reserve"] if row else 0

    async def _add_reserve(self, delta: int) -> None:
        await self.conn.execute(
            "UPDATE company SET reserve = reserve + ? WHERE id = 1", (delta,)
        )

    async def liabilities(self) -> int:
        """会社の負債 = 発行済みリリーコイン + よあコイン残高（どちらも準備金で裏付け）。"""
        async with self.conn.execute(
            "SELECT COALESCE(SUM(coins),0)+COALESCE(SUM(yc),0) AS l FROM users"
        ) as cur:
            return (await cur.fetchone())["l"]

    # ---- users / balances -------------------------------------------------
    async def ensure_user(self, user_id: int) -> None:
        async with self._tx():
            await self.conn.execute(
                "INSERT OR IGNORE INTO users(user_id, coins, gems, created_at) "
                "VALUES (?, 0, 0, ?)",
                (user_id, _now()),
            )

    async def get_balance(self, user_id: int) -> Balance:
        await self.ensure_user(user_id)
        async with self.conn.execute(
            "SELECT coins, gems, yc FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return Balance(coins=row["coins"], gems=row["gems"], yc=row["yc"])

    async def add_coins(self, user_id: int, amount: int, reason: str) -> Balance:
        async with self._tx():
            await self.ensure_user(user_id)
            await self.conn.execute(
                "UPDATE users SET coins = coins + ? WHERE user_id = ?", (amount, user_id)
            )
            await self._log(user_id, "coins", amount, reason)
            return await self.get_balance(user_id)

    async def add_gems(self, user_id: int, amount: int, reason: str) -> Balance:
        async with self._tx():
            await self.ensure_user(user_id)
            await self.conn.execute(
                "UPDATE users SET gems = gems + ? WHERE user_id = ?", (amount, user_id)
            )
            await self._log(user_id, "gems", amount, reason)
            return await self.get_balance(user_id)

    async def try_spend_coins(self, user_id: int, amount: int, reason: str) -> bool:
        if amount < 0:
            raise ValueError("amount must be >= 0")
        async with self._tx():
            await self.ensure_user(user_id)
            cur = await self.conn.execute(
                "UPDATE users SET coins = coins - ? WHERE user_id = ? AND coins >= ?",
                (amount, user_id, amount),
            )
            if cur.rowcount == 0:
                return False  # 残高不足。この UPDATE は何も変更していない。
            await self._log(user_id, "coins", -amount, reason)
            return True

    async def try_spend_gems(self, user_id: int, amount: int, reason: str) -> bool:
        if amount < 0:
            raise ValueError("amount must be >= 0")
        async with self._tx():
            await self.ensure_user(user_id)
            cur = await self.conn.execute(
                "UPDATE users SET gems = gems - ? WHERE user_id = ? AND gems >= ?",
                (amount, user_id, amount),
            )
            if cur.rowcount == 0:
                return False  # 残高不足。この UPDATE は何も変更していない。
            await self._log(user_id, "gems", -amount, reason)
            return True

    # ---- deposit / gem / withdraw ----------------------------------------
    async def deposit_yoacoin(self, user_id: int, amount: int) -> Balance:
        """よあコイン入金 → 準備金 +amount、リリーコイン(coins) +amount（1:1）。Equity不変。

        よあコインは全サーバー共通のグローバル通貨。入金すると同額のリリーコイン（ゲーム内通貨）
        になる。ゲームはリリーコインで遊ぶ。
        """
        async with self._tx():
            await self.ensure_user(user_id)
            await self._add_reserve(amount)
            await self._log(0, "reserve", amount, "deposit")
            await self.conn.execute(
                "UPDATE users SET coins = coins + ? WHERE user_id = ?", (amount, user_id)
            )
            await self._log(user_id, "coins", amount, "deposit")
            return await self.get_balance(user_id)

    async def buy_gems(self, user_id: int, gems: int, price_coins: int) -> bool:
        """リリーコインを消費してジェムを付与。リリーコインは消滅＝会社純利益。"""
        total = gems * price_coins
        # 消費とジェム付与を1トランザクションに（片方だけ成立するのを防ぐ）
        async with self._tx():
            if not await self.try_spend_coins(user_id, total, "buygems"):
                return False
            await self.add_gems(user_id, gems, "buygems")
            return True

    async def withdraw_to_yoacoin(
        self, user_id: int, gross: int, net: int, fee: int,
        reserve_floor: int, *, queue_only: bool,
    ) -> bool:
        """リリーコイン → よあコイン（手数料あり・/payoutで実換金）。

        gross リリーコインを焼却し、net よあコインを払い出す（準備金 −net）。fee分は
        会社の利益（Equity +fee）。queue_only=True なら準備金は動かさず申請キューへ。
        戻り値 False = リリーコイン残高不足 / 準備金フロア割れ。
        """
        async with self._tx():
            await self.ensure_user(user_id)
            # 準備金フロアは残高を引く前に確認（巻き戻しを不要にする）
            if not queue_only:
                reserve = await self.get_reserve()
                if reserve - net < reserve_floor:
                    return False

            cur = await self.conn.execute(
                "UPDATE users SET coins = coins - ? WHERE user_id = ? AND coins >= ?",
                (gross, user_id, gross),
            )
            if cur.rowcount == 0:
                return False  # リリーコイン残高不足。この UPDATE は何も変更していない。

            if queue_only:
                await self.conn.execute(
                    "INSERT INTO withdraw_requests(user_id, gross, net_payout, fee, status, ts) "
                    "VALUES (?, ?, ?, ?, 'pending', ?)",
                    (user_id, gross, net, fee, _now()),
                )
            else:
                await self._add_reserve(-net)
                await self._log(0, "reserve", -net, "withdraw_payout")

            await self._log(user_id, "coins", -net, "withdraw")
            await self._log(user_id, "coins", -fee, "withdraw_fee")  # 会社利益（監査用）
            await self.conn.execute(
                "INSERT INTO withdraw_state(user_id, last_at) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET last_at = excluded.last_at",
                (user_id, _now()),
            )
            return True

    async def last_withdraw_at(self, user_id: int) -> int:
        async with self.conn.execute(
            "SELECT last_at FROM withdraw_state WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return row["last_at"] if row else 0

    # ---- 換金申請キュー（payout未対応時に溜まる） ------------------------
    async def list_withdraw_requests(self, status: str = "pending", limit: int = 50) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM withdraw_requests WHERE status = ? ORDER BY id LIMIT ?",
            (status, limit),
        ) as cur:
            return list(await cur.fetchall())

    async def pending_withdraw_summary(self) -> tuple[int, int]:
        """未処理の換金申請の (件数, 払い出し合計net) を返す。"""
        async with self.conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(net_payout),0) AS s "
            "FROM withdraw_requests WHERE status = 'pending'"
        ) as cur:
            r = await cur.fetchone()
        return r["c"], r["s"]

    async def mark_withdraw_paid(self, request_id: int, reserve_floor: int) -> bool:
        """申請を『支払済み』にして準備金から net を差し引く。

        申請時(queue_only)は準備金を動かしていないため、ここで実際の払い出しに合わせて減算。
        準備金フロアを割る場合・既に処理済みの場合は False（変更なし）。
        """
        async with self._tx():
            async with self.conn.execute(
                "SELECT net_payout, status FROM withdraw_requests WHERE id = ?", (request_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None or row["status"] != "pending":
                return False
            net = row["net_payout"]
            reserve = await self.get_reserve()
            if reserve - net < reserve_floor:
                return False
            await self._add_reserve(-net)
            await self._log(0, "reserve", -net, "withdraw_payout")
            await self.conn.execute(
                "UPDATE withdraw_requests SET status = 'paid' WHERE id = ?", (request_id,)
            )
            return True

    # ---- creatures --------------------------------------------------------
    async def add_creature(
        self, user_id: int, species_id: str, iv_hp: int, iv_atk: int, iv_def: int
    ) -> int:
        async with self._tx():
            cur = await self.conn.execute(
                "INSERT INTO user_creatures"
                "(user_id, species_id, iv_hp, iv_atk, iv_def, caught_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, species_id, iv_hp, iv_atk, iv_def, _now()),
            )
            return int(cur.lastrowid)

    async def list_creatures(self, user_id: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM user_creatures WHERE user_id = ? ORDER BY caught_at DESC",
            (user_id,),
        ) as cur:
            return list(await cur.fetchall())

    async def distinct_species(self, user_id: int) -> set[str]:
        async with self.conn.execute(
            "SELECT DISTINCT species_id FROM user_creatures WHERE user_id = ?",
            (user_id,),
        ) as cur:
            return {row["species_id"] for row in await cur.fetchall()}

    async def distinct_species_count(self, user_id: int) -> int:
        async with self.conn.execute(
            "SELECT COUNT(DISTINCT species_id) AS c FROM user_creatures WHERE user_id = ?",
            (user_id,),
        ) as cur:
            return (await cur.fetchone())["c"]

    async def get_creature(self, user_id: int, instance_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT * FROM user_creatures WHERE instance_id = ? AND user_id = ?",
            (instance_id, user_id),
        ) as cur:
            return await cur.fetchone()

    async def release_creature(self, user_id: int, instance_id: int) -> bool:
        """指定個体を逃がす（削除）。所有していれば True。"""
        async with self._tx():
            cur = await self.conn.execute(
                "DELETE FROM user_creatures WHERE instance_id = ? AND user_id = ?",
                (instance_id, user_id),
            )
            return cur.rowcount > 0

    async def reroll_creature_ivs(
        self, user_id: int, instance_id: int, iv_hp: int, iv_atk: int, iv_def: int
    ) -> bool:
        async with self._tx():
            cur = await self.conn.execute(
                "UPDATE user_creatures SET iv_hp=?, iv_atk=?, iv_def=? "
                "WHERE instance_id=? AND user_id=?",
                (iv_hp, iv_atk, iv_def, instance_id, user_id),
            )
            return cur.rowcount > 0

    # ---- 探索状態（エリア・深度） -----------------------------------------
    async def get_explore_state(self, user_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT habitat, depth, last_at FROM explore_state WHERE user_id = ?", (user_id,)
        ) as cur:
            return await cur.fetchone()

    async def set_explore_state(self, user_id: int, habitat: str, depth: int, last_at: int) -> None:
        async with self._tx():
            await self.conn.execute(
                "INSERT INTO explore_state(user_id, habitat, depth, last_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET habitat=excluded.habitat, "
                "depth=excluded.depth, last_at=excluded.last_at",
                (user_id, habitat, depth, last_at),
            )

    # ---- エリア解放 -------------------------------------------------------
    async def unlocked_areas(self, user_id: int) -> set[str]:
        async with self.conn.execute(
            "SELECT habitat FROM unlocked_areas WHERE user_id = ?", (user_id,)
        ) as cur:
            return {row["habitat"] for row in await cur.fetchall()}

    async def unlock_area(self, user_id: int, habitat: str) -> bool:
        """新規解放なら True。既に解放済みなら False。"""
        async with self._tx():
            try:
                await self.conn.execute(
                    "INSERT INTO unlocked_areas(user_id, habitat) VALUES (?, ?)",
                    (user_id, habitat),
                )
            except aiosqlite.IntegrityError:
                return False
            return True

    # ---- 図鑑マイルストーン ------------------------------------------------
    async def claimed_milestones(self, user_id: int) -> set[int]:
        async with self.conn.execute(
            "SELECT milestone FROM milestone_claims WHERE user_id = ?", (user_id,)
        ) as cur:
            return {row["milestone"] for row in await cur.fetchall()}

    async def claim_milestone(self, user_id: int, milestone: int) -> bool:
        """未受取なら記録して True。受取済みなら False。"""
        async with self._tx():
            try:
                await self.conn.execute(
                    "INSERT INTO milestone_claims(user_id, milestone) VALUES (?, ?)",
                    (user_id, milestone),
                )
            except aiosqlite.IntegrityError:
                return False
            return True

    # ---- ログインボーナス -------------------------------------------------
    async def get_login(self, user_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT last_period, streak FROM login_state WHERE user_id = ?", (user_id,)
        ) as cur:
            return await cur.fetchone()

    async def set_login(self, user_id: int, period: str, streak: int) -> None:
        async with self._tx():
            await self.conn.execute(
                "INSERT INTO login_state(user_id, last_period, streak) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET last_period=excluded.last_period, "
                "streak=excluded.streak",
                (user_id, period, streak),
            )

    # ---- 通常クエスト（プール制・per-userスロット） ----------------------
    async def get_user_quests(self, user_id: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT slot, quest_id, target, progress, claimed FROM user_quests "
            "WHERE user_id = ? ORDER BY slot", (user_id,)
        ) as cur:
            return list(await cur.fetchall())

    async def upsert_user_quest(self, user_id: int, slot: int, quest_id: str,
                                target: int, progress: int, claimed: int) -> None:
        async with self._tx():
            await self.conn.execute(
                "INSERT INTO user_quests(user_id, slot, quest_id, target, progress, claimed) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, slot) DO UPDATE SET quest_id=excluded.quest_id, "
                "target=excluded.target, progress=excluded.progress, claimed=excluded.claimed",
                (user_id, slot, quest_id, target, progress, claimed),
            )

    async def set_user_quest_claimed(self, user_id: int, slot: int) -> None:
        async with self._tx():
            await self.conn.execute(
                "UPDATE user_quests SET claimed = 1 WHERE user_id = ? AND slot = ?",
                (user_id, slot),
            )

    async def get_reroll(self, user_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT period, free_used FROM quest_reroll WHERE user_id = ?", (user_id,)
        ) as cur:
            return await cur.fetchone()

    async def set_reroll(self, user_id: int, period: str, free_used: int) -> None:
        async with self._tx():
            await self.conn.execute(
                "INSERT INTO quest_reroll(user_id, period, free_used) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET period=excluded.period, "
                "free_used=excluded.free_used",
                (user_id, period, free_used),
            )

    # ---- インベントリ上限 -------------------------------------------------
    async def get_creature_cap(self, user_id: int) -> int:
        async with self.conn.execute(
            "SELECT creature_cap FROM user_limits WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return row["creature_cap"] if row else 50

    async def add_creature_cap(self, user_id: int, delta: int) -> int:
        async with self._tx():
            await self.conn.execute(
                "INSERT INTO user_limits(user_id, creature_cap) VALUES (?, 50 + ?) "
                "ON CONFLICT(user_id) DO UPDATE SET creature_cap = creature_cap + ?",
                (user_id, delta, delta),
            )
            return await self.get_creature_cap(user_id)

    async def creature_count(self, user_id: int) -> int:
        async with self.conn.execute(
            "SELECT COUNT(*) AS c FROM user_creatures WHERE user_id = ?", (user_id,)
        ) as cur:
            return (await cur.fetchone())["c"]

    # ---- バッジ（キャラ以外のコレクション） -------------------------------
    async def get_badges(self, user_id: int) -> set[str]:
        async with self.conn.execute(
            "SELECT badge_id FROM user_badges WHERE user_id = ?", (user_id,)
        ) as cur:
            return {row["badge_id"] for row in await cur.fetchall()}

    async def grant_badge(self, user_id: int, badge_id: str) -> bool:
        async with self._tx():
            try:
                await self.conn.execute(
                    "INSERT INTO user_badges(user_id, badge_id, got_at) VALUES (?, ?, ?)",
                    (user_id, badge_id, _now()),
                )
            except aiosqlite.IntegrityError:
                return False
            return True

    # ---- バッジ用の累計スタッツ ---------------------------------------------
    _STAT_COLUMNS = {"explores", "tames", "merges", "releases"}

    async def bump_stat(self, user_id: int, field: str, delta: int = 1) -> None:
        if field not in self._STAT_COLUMNS:
            raise ValueError(f"invalid stat field: {field}")
        async with self._tx():
            await self.conn.execute(
                f"INSERT INTO user_stats(user_id, {field}) VALUES (?, ?) "
                f"ON CONFLICT(user_id) DO UPDATE SET {field} = {field} + ?",
                (user_id, delta, delta),
            )

    async def bump_max_depth(self, user_id: int, depth: int) -> None:
        async with self._tx():
            await self.conn.execute(
                "INSERT INTO user_stats(user_id, max_depth) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET max_depth = MAX(max_depth, ?)",
                (user_id, depth, depth),
            )

    async def get_stats(self, user_id: int) -> dict:
        async with self.conn.execute(
            "SELECT explores, tames, merges, releases, max_depth FROM user_stats WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return {"explores": 0, "tames": 0, "merges": 0, "releases": 0, "max_depth": 0}
        return dict(row)

    async def set_nickname(self, user_id: int, instance_id: int, nickname: str) -> bool:
        async with self._tx():
            cur = await self.conn.execute(
                "UPDATE user_creatures SET nickname = ? WHERE instance_id = ? AND user_id = ?",
                (nickname, instance_id, user_id),
            )
            return cur.rowcount > 0

    async def merge_creatures(self, user_id: int, keep_id: int, consume_id: int,
                              iv_hp: int, iv_atk: int, iv_def: int) -> bool:
        """consume_id を削除し keep_id の個体値を更新（合体）。"""
        async with self._tx():
            c1 = await self.get_creature(user_id, keep_id)
            c2 = await self.get_creature(user_id, consume_id)
            if c1 is None or c2 is None or keep_id == consume_id:
                return False
            if c1["species_id"] != c2["species_id"]:
                return False
            # 素材の削除が実際に成立したときだけ強化を確定（同時実行での二重合体を防ぐ）
            cur = await self.conn.execute(
                "DELETE FROM user_creatures WHERE instance_id = ? AND user_id = ?",
                (consume_id, user_id),
            )
            if cur.rowcount == 0:
                return False
            await self.conn.execute(
                "UPDATE user_creatures SET iv_hp=?, iv_atk=?, iv_def=? "
                "WHERE instance_id=? AND user_id=?",
                (iv_hp, iv_atk, iv_def, keep_id, user_id),
            )
            return True

    # ---- quest progress (目標達成型) --------------------------------------
    async def ensure_quest(self, user_id: int, quest_id: str, period: str, target: int) -> None:
        async with self._tx():
            await self.conn.execute(
                "INSERT OR IGNORE INTO quest_progress"
                "(user_id, quest_id, period, progress, target, claimed) "
                "VALUES (?, ?, ?, 0, ?, 0)",
                (user_id, quest_id, period, target),
            )

    async def bump_quest(
        self, user_id: int, quest_id: str, period: str, target: int, delta: int = 1
    ) -> tuple[int, bool]:
        """進捗を加算。戻り値 (progress, just_completed)。"""
        # 読み取り→加算→書き戻しを1トランザクションに（進捗の取りこぼしを防ぐ）
        async with self._tx():
            await self.ensure_quest(user_id, quest_id, period, target)
            async with self.conn.execute(
                "SELECT progress, claimed FROM quest_progress "
                "WHERE user_id=? AND quest_id=? AND period=?",
                (user_id, quest_id, period),
            ) as cur:
                row = await cur.fetchone()
            before = row["progress"]
            new = min(target, before + delta)
            await self.conn.execute(
                "UPDATE quest_progress SET progress=? "
                "WHERE user_id=? AND quest_id=? AND period=?",
                (new, user_id, quest_id, period),
            )
            just_completed = before < target <= new
            return new, just_completed

    async def get_quest(self, user_id: int, quest_id: str, period: str):
        async with self.conn.execute(
            "SELECT progress, target, claimed FROM quest_progress "
            "WHERE user_id=? AND quest_id=? AND period=?",
            (user_id, quest_id, period),
        ) as cur:
            return await cur.fetchone()

    async def try_claim_quest(self, user_id: int, quest_id: str, period: str) -> bool:
        """達成済みかつ未受取なら受取済みにして True。"""
        async with self._tx():
            cur = await self.conn.execute(
                "UPDATE quest_progress SET claimed=1 "
                "WHERE user_id=? AND quest_id=? AND period=? AND claimed=0 AND progress>=target",
                (user_id, quest_id, period),
            )
            return cur.rowcount > 0

    # ---- work（無制限クエスト・逓減） -------------------------------------
    async def get_cooldown(self, user_id: int, quest_id: str):
        async with self.conn.execute(
            "SELECT last_done_at, streak FROM quest_cooldowns "
            "WHERE user_id = ? AND quest_id = ?",
            (user_id, quest_id),
        ) as cur:
            return await cur.fetchone()

    async def set_cooldown(
        self, user_id: int, quest_id: str, last_done_at: int, streak: int
    ) -> None:
        async with self._tx():
            await self.conn.execute(
                "INSERT INTO quest_cooldowns(user_id, quest_id, last_done_at, streak) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, quest_id) DO UPDATE SET "
                "last_done_at = excluded.last_done_at, streak = excluded.streak",
                (user_id, quest_id, last_done_at, streak),
            )

    # ---- items ------------------------------------------------------------
    async def add_item(self, user_id: int, item_id: str, qty: int) -> None:
        async with self._tx():
            await self.conn.execute(
                "INSERT INTO user_items(user_id, item_id, qty) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, item_id) DO UPDATE SET qty = qty + excluded.qty",
                (user_id, item_id, qty),
            )

    async def get_item_qty(self, user_id: int, item_id: str) -> int:
        async with self.conn.execute(
            "SELECT qty FROM user_items WHERE user_id = ? AND item_id = ?",
            (user_id, item_id),
        ) as cur:
            row = await cur.fetchone()
        return row["qty"] if row else 0

    async def list_items(self, user_id: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT item_id, qty FROM user_items WHERE user_id = ? AND qty > 0",
            (user_id,),
        ) as cur:
            return list(await cur.fetchall())

    async def try_consume_item(self, user_id: int, item_id: str, qty: int = 1) -> bool:
        async with self._tx():
            cur = await self.conn.execute(
                "UPDATE user_items SET qty = qty - ? "
                "WHERE user_id = ? AND item_id = ? AND qty >= ?",
                (qty, user_id, item_id, qty),
            )
            return cur.rowcount > 0

    # ---- payment cursor / idempotency ------------------------------------
    async def get_cursor(self) -> int:
        async with self.conn.execute(
            "SELECT cursor FROM payment_cursor WHERE id = 1"
        ) as cur:
            row = await cur.fetchone()
        return row["cursor"] if row else 0

    async def set_cursor(self, cursor: int) -> None:
        async with self._tx():
            await self.conn.execute(
                "UPDATE payment_cursor SET cursor = ? WHERE id = 1", (cursor,)
            )

    async def is_payment_processed(self, payment_id: int) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM processed_payments WHERE payment_id = ?", (payment_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def mark_payment_processed(self, payment_id: int) -> bool:
        async with self._tx():
            try:
                await self.conn.execute(
                    "INSERT INTO processed_payments(payment_id, ts) VALUES (?, ?)",
                    (payment_id, _now()),
                )
            except aiosqlite.IntegrityError:
                return False
            return True

    async def recent_transactions(self, user_id: int, limit: int = 10) -> list[aiosqlite.Row]:
        """ユーザーの最近の取引（購入・報酬・換金など）。内部監査行(medal_fee)は除外。"""
        async with self.conn.execute(
            "SELECT currency, amount, reason, ts FROM transactions "
            "WHERE user_id = ? AND currency IN ('coins','gems','yc') "
            "AND reason != 'withdraw_fee' ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ) as cur:
            return list(await cur.fetchall())

    # ---- economy dashboard （よあコイン建て） -----------------------------
    async def _reason_sum(self, where: str, params: tuple = ()) -> int:
        async with self.conn.execute(
            f"SELECT COALESCE(SUM(amount),0) AS s FROM transactions WHERE {where}", params
        ) as cur:
            return (await cur.fetchone())["s"]

    async def economy_summary(self, target_equity: int) -> dict:
        reserve = await self.get_reserve()
        liabilities = await self.liabilities()
        equity = reserve - liabilities

        deposits = await self._reason_sum("reason='deposit' AND currency='coins'")
        # faucet内訳（配ったリリーコイン）
        faucet_quest = await self._reason_sum(
            "currency='coins' AND amount>0 AND (reason LIKE 'quest:%' OR reason LIKE 'daily:%')"
        )
        faucet_login = await self._reason_sum("currency='coins' AND reason='login'")
        faucet_milestone = await self._reason_sum("currency='coins' AND reason='milestone'")
        faucet_release = await self._reason_sum("currency='coins' AND reason='release'")
        faucet = faucet_quest + faucet_login + faucet_milestone + faucet_release

        game_sink = -await self._reason_sum(
            "currency='coins' AND amount<0 AND "
            "(reason IN ('explore','tame') OR reason LIKE 'shop:%')"
        )
        gem_sales = -await self._reason_sum("currency='coins' AND reason='buygems'")
        fees = -await self._reason_sum("currency='coins' AND reason='withdraw_fee'")
        payouts = -await self._reason_sum("currency='coins' AND reason='withdraw'")

        async with self.conn.execute("SELECT COUNT(*) AS c FROM users") as cur:
            users = (await cur.fetchone())["c"]

        return {
            "reserve": reserve,
            "liabilities": liabilities,
            "equity": equity,
            "target": target_equity,
            "goal_remaining": max(0, target_equity - equity),
            "deposits": deposits,
            "faucet": faucet,
            "faucet_quest": faucet_quest,
            "faucet_login": faucet_login,
            "faucet_milestone": faucet_milestone,
            "faucet_release": faucet_release,
            "game_sink": game_sink,
            "gem_sales": gem_sales,
            "fees": fees,
            "payouts": payouts,
            "users": users,
        }

    # ---- リーダーボード ---------------------------------------------------
    async def _top(self, sql: str, limit: int) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, (limit,)) as cur:
            return list(await cur.fetchall())

    async def top_coins(self, limit: int = 10) -> list[aiosqlite.Row]:
        return await self._top(
            "SELECT user_id, coins AS v FROM users WHERE coins > 0 "
            "ORDER BY coins DESC LIMIT ?", limit)

    async def top_species(self, limit: int = 10) -> list[aiosqlite.Row]:
        return await self._top(
            "SELECT user_id, COUNT(DISTINCT species_id) AS v FROM user_creatures "
            "GROUP BY user_id ORDER BY v DESC LIMIT ?", limit)

    async def top_creatures(self, limit: int = 10) -> list[aiosqlite.Row]:
        return await self._top(
            "SELECT user_id, COUNT(*) AS v FROM user_creatures "
            "GROUP BY user_id ORDER BY v DESC LIMIT ?", limit)

    async def top_badges(self, limit: int = 10) -> list[aiosqlite.Row]:
        return await self._top(
            "SELECT user_id, COUNT(*) AS v FROM user_badges "
            "GROUP BY user_id ORDER BY v DESC LIMIT ?", limit)

    async def withdrawable(self, user_id: int) -> int:
        """このユーザーが換金できる上限枠 = Σ入金 − Σ換金gross。

        入金0のユーザーは0（無料リリーは換金不可＝farmer対策）。会社は入金分しか
        払い出さないため、準備金は常に増える方向。
        """
        async with self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM transactions "
            "WHERE user_id=? AND currency='coins' AND reason='deposit'", (user_id,)
        ) as cur:
            dep = (await cur.fetchone())["s"]
        async with self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM transactions "
            "WHERE user_id=? AND currency='coins' AND reason IN ('withdraw','withdraw_fee')",
            (user_id,),
        ) as cur:
            wd = (await cur.fetchone())["s"]  # 負値（換金で焼却したgross）
        return max(0, dep + wd)

    async def net_since(self, since_ts: int) -> dict:
        """since_ts 以降の会社純資産の増減内訳（日次純増など）。

        Equity変化 = ゲーム消費 + ジェム売上 + 出金手数料 − faucet。
        """
        def w(extra: str) -> str:
            return f"currency='coins' AND ts >= ? AND {extra}"

        p = (since_ts,)
        game_sink = -await self._reason_sum(
            w("amount<0 AND (reason IN ('explore','tame') OR reason LIKE 'shop:%')"), p)
        gem_sales = -await self._reason_sum(w("reason='buygems'"), p)
        fees = -await self._reason_sum(w("reason='withdraw_fee'"), p)
        faucet = await self._reason_sum(
            w("amount>0 AND (reason LIKE 'quest:%' OR reason LIKE 'daily:%' "
              "OR reason IN ('login','milestone','release'))"), p)
        net = game_sink + gem_sales + fees - faucet
        return {"net": net, "game_sink": game_sink, "gem_sales": gem_sales,
                "fees": fees, "faucet": faucet}

    # ---- 会社の開始（/economy 会計のリセット） ---------------------------
    async def all_user_ids(self) -> list[int]:
        async with self.conn.execute("SELECT user_id FROM users ORDER BY user_id") as cur:
            return [r["user_id"] for r in await cur.fetchall()]

    async def reset_economy(self, paid_net: int) -> None:
        """/economy に表示される会計をリセットする（返金の払い出し後に呼ぶ）。

        - 全ユーザーの**リリーコイン残高を0**に（＝負債0）。ジェム・生き物・図鑑・
          バッジ・アイテムなどのゲーム進捗は**保持**する。
        - **取引履歴・換金申請・換金状態を消去**（入金/消費/配布/手数料/換金枠/日次純増が
          すべて0に戻る）。
        - 準備金は**リセットしない**（現在の資本金を維持）。実際に払い出した返金 net の
          分だけ差し引くのみ。
        - payment_cursor / processed_payments は**残す**（過去のテスト入金が再取込され
          二重付与されるのを防ぐ）。
        """
        async with self._tx():
            await self.conn.execute("UPDATE users SET coins = 0, yc = 0")
            await self.conn.execute("DELETE FROM transactions")
            await self.conn.execute("DELETE FROM withdraw_requests")
            await self.conn.execute("DELETE FROM withdraw_state")
            if paid_net > 0:
                await self._add_reserve(-paid_net)

    # 全ユーザーのゲームデータ（生き物/図鑑/バッジ/アイテム/ジェム/クエスト等）も消す全消去
    _WIPE_TABLES = (
        "users", "user_creatures", "user_items", "user_quests", "quest_progress",
        "quest_cooldowns", "quest_reroll", "user_limits", "user_badges", "user_stats",
        "explore_state", "milestone_claims", "login_state", "unlocked_areas",
        "withdraw_state", "withdraw_requests", "transactions",
    )

    async def wipe_all_game_data(self, paid_net: int) -> None:
        """会社を完全リセット: 全ユーザーのゲームデータを消去する。

        - リリーコイン・ジェム・生き物・図鑑・バッジ・アイテム・クエスト・ログイン等を全消去。
        - 準備金は**リセットせず**、返金で払い出した net の分だけ差し引く。
        - payment_cursor / processed_payments は保持（過去入金の再取込防止）。
        """
        async with self._tx():
            for tbl in self._WIPE_TABLES:
                await self.conn.execute(f"DELETE FROM {tbl}")
            if paid_net > 0:
                await self._add_reserve(-paid_net)
