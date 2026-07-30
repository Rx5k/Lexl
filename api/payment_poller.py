"""入金ポーリング（カーソル式）。

ドキュメントの推奨パターンに準拠:
1. 保存済みカーソルを読む（初回0）
2. GET /payments?since=<cursor>&limit=100
3. 各入金を「処理成功後にのみ」カーソルを進めて保存
4. processed_payments で冪等化（再起動・重複でも二重付与しない）
5. count == limit なら追いつくまでループ、そうでなければ数秒スリープ

入金1リリーコインあたり GEMS_PER_YOACOIN 個のジェム（ハード通貨）を付与する。
ジェムはここでしか発行されないため、yoacoin の価値が希釈されない。
"""
from __future__ import annotations

import asyncio
import logging

from api.yoacoin_client import YoacoinAPIError, YoacoinClient
from config import Config
from db import Database

log = logging.getLogger("yoacoin.poller")

MAX_BACKOFF = 300       # 連続失敗時の最大待ち時間（秒）
LOG_EVERY = 15          # 連続失敗が続くとき、何回に1回ログを出すか


class PaymentPoller:
    def __init__(self, cfg: Config, db: Database, *, dry_run: bool = False):
        self.cfg = cfg
        self.db = db
        self.dry_run = dry_run
        self.limit = 100
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._fail_streak = 0
        # 入金を検知したときに呼ばれるフック（Bot側で通知に使う）。
        # 引数: (discord_user_id, yoacoin_amount, gems_granted)
        self.on_deposit = None

    def _backoff(self) -> float:
        """次に待つ秒数。APIに到達できない間は指数的に伸ばす。

        到達できない相手に数秒おきで接続し続けると、接続待ちが積み上がって
        イベントループを圧迫し、Discordのハートビートまで巻き添えになる。
        """
        if self._fail_streak == 0:
            return float(self.cfg.poll_interval)
        delay = self.cfg.poll_interval * (2 ** min(self._fail_streak, 8))
        return float(min(delay, MAX_BACKOFF))

    def _note_failure(self, e: object) -> None:
        self._fail_streak += 1
        # 毎回出すとログが埋まるので、最初と一定間隔だけ記録する
        if self._fail_streak == 1 or self._fail_streak % LOG_EVERY == 0:
            log.warning("payments 取得エラー（%d回連続・次は%.0f秒後）: %s",
                        self._fail_streak, self._backoff(), e)

    def _note_success(self) -> None:
        if self._fail_streak:
            log.info("payments 取得が回復しました（%d回連続失敗のあと）", self._fail_streak)
            self._fail_streak = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="payment_poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def run(self) -> None:
        log.info("入金ポーラ開始 (dry_run=%s, interval=%ss)",
                 self.dry_run, self.cfg.poll_interval)
        async with YoacoinClient(self.cfg.base_url, self.cfg.api_key) as client:
            while not self._stop.is_set():
                try:
                    caught_up = await self._poll_once(client)
                    self._note_success()
                except YoacoinAPIError as e:
                    # 401(キー不正)/5xx/接続不能 など。停止せず、間隔を空けて再試行。
                    self._note_failure(e)
                    caught_up = True
                except Exception as e:
                    if self._fail_streak == 0:
                        log.exception("ポーリング中に予期せぬ例外")
                    self._note_failure(e)
                    caught_up = True

                if caught_up:
                    # 追いついたら待つ。失敗が続く間は指数的に間隔を伸ばす
                    # （stop されたら即座に抜ける）
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=self._backoff())
                    except asyncio.TimeoutError:
                        pass
        log.info("入金ポーラ停止")

    async def _poll_once(self, client: YoacoinClient) -> bool:
        """1回分の取得と処理。追いついていれば True を返す。"""
        cursor = await self.db.get_cursor()
        page = await client.payments(since=cursor, limit=self.limit)

        if page.count == 0:
            return True

        for p in page.payments:
            await self._handle_payment(p)

        # 全件処理できたので、ここで初めてカーソルを保存する。
        if not self.dry_run:
            await self.db.set_cursor(page.next_cursor)
        else:
            log.info("[dry-run] カーソルは保存しません (次=%s)", page.next_cursor)

        # まだ続きがあるなら即ループ（スリープしない）。
        return page.count < self.limit

    async def _handle_payment(self, p) -> None:
        # 冪等: 既に付与済みならスキップ。
        if not await self.db.mark_payment_processed(p.id):
            log.debug("入金 id=%s は処理済み。スキップ", p.id)
            return

        # 入金額と同額のリリーコインを付与し、準備金も +amount（1:1）。
        if self.dry_run:
            log.info("[dry-run] 入金 id=%s user=%s amount=%s -> リリーコイン=%s",
                     p.id, p.user_id, p.amount, p.amount)
            return

        if p.amount > 0:
            await self.db.deposit_yoacoin(p.user_id, p.amount)
        log.info("入金付与 id=%s user=%s amount=%s リリーコイン=%s",
                 p.id, p.user_id, p.amount, p.amount)

        if self.on_deposit is not None:
            try:
                res = self.on_deposit(p.user_id, p.amount, p.amount)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                log.exception("on_deposit フックでエラー")


async def _dry_run_main() -> None:
    """`python -m api.payment_poller` で dry-run 実行。DBは書き換えない。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.load()
    cfg.require_for_bot()
    db = Database(cfg.db_path)
    await db.connect()
    poller = PaymentPoller(cfg, db, dry_run=True)
    try:
        async with YoacoinClient(cfg.base_url, cfg.api_key) as client:
            await poller._poll_once(client)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(_dry_run_main())
