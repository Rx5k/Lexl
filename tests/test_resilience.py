"""ネットワークが不安定なホストでも動き続けるかのテスト。

無料ホスティングでは、よあコインAPIやDiscordへ数十分繋がらないことがある。
そのとき数秒おきに接続し続けると、接続待ちが積み上がってイベントループを圧迫し、
Discordのハートビートが止まって切断・コマンドの応答期限切れを引き起こす。

ここでは「失敗が続くほど再試行の間隔を空ける」「接続待ちが長引かない」
「待っている間もループが動く」ことを検証する。

pytest でも、`python -m tests.test_resilience` でも実行できる。
"""
from __future__ import annotations

import asyncio
import socket
import time

from api import payment_poller, yoacoin_client
from api.payment_poller import PaymentPoller
from config import Config


def _poller() -> PaymentPoller:
    return PaymentPoller(Config.load(), db=None)


def test_backoff_grows_and_is_capped():
    """失敗が続くほど間隔が伸び、上限で頭打ちになること。"""
    p = _poller()
    interval = p.cfg.poll_interval

    p._fail_streak = 0
    assert p._backoff() == interval, "成功中は通常の間隔で回る"

    prev = 0.0
    for n in range(1, 8):
        p._fail_streak = n
        d = p._backoff()
        assert d >= prev, f"{n}回目で間隔が縮んだ: {prev} → {d}"
        assert d <= payment_poller.MAX_BACKOFF
        prev = d

    p._fail_streak = 100
    assert p._backoff() == payment_poller.MAX_BACKOFF, "上限で頭打ちになること"


def test_backoff_reaches_cap_within_reasonable_failures():
    """数分ほどで上限に達し、無駄な接続を積み上げ続けないこと。"""
    p = _poller()
    total = 0.0
    for n in range(1, 8):
        p._fail_streak = n
        total += p._backoff()
    assert total <= 20 * 60, f"7回失敗までに{total/60:.0f}分もかかる"
    p._fail_streak = 7
    assert p._backoff() >= 60, "失敗が続いても間隔が短いままでは負荷が下がらない"


def test_failure_streak_resets_on_success():
    p = _poller()
    p._fail_streak = 5
    p._note_success()
    assert p._fail_streak == 0
    assert p._backoff() == p.cfg.poll_interval


def test_connect_timeout_is_bounded():
    """接続段階に個別の上限があること（総timeoutぶん待たされない）。"""
    c = yoacoin_client.YoacoinClient("https://example.invalid", "k", timeout=10)
    t = c._timeout
    assert t.connect is not None and t.connect <= 5
    assert t.sock_connect is not None and t.sock_connect <= 5
    assert t.connect < t.total, "接続待ちが総timeoutより短く打ち切られること"


def test_connector_avoids_dual_stack_hang():
    """IPv6が繋がらないホストでの長い待ちを避ける設定になっていること。"""
    async def main():
        c = yoacoin_client._make_connector()
        try:
            expected = (socket.AF_INET if yoacoin_client.FORCE_IPV4
                        else socket.AF_UNSPEC)
            assert c._family == expected
        finally:
            await c.close()
    asyncio.run(main())


def test_unreachable_host_does_not_block_event_loop():
    """到達できない相手を待つ間も、他の処理（＝ハートビート）が動き続けること。"""
    async def main():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        # 10.255.255.1 は応答しない前提のアドレス（RFC1918内の未使用IP）
        client = yoacoin_client.YoacoinClient("https://10.255.255.1", "dummy", timeout=10)
        await client.start()
        hb = asyncio.create_task(heartbeat())
        started = time.perf_counter()
        try:
            try:
                await client.payments(since=0, limit=1)
            except yoacoin_client.YoacoinAPIError as e:
                assert str(e), "エラーメッセージが空だと原因が追えない"
            elapsed = time.perf_counter() - started
        finally:
            hb.cancel()
            await client.close()

        assert elapsed < 8, f"接続待ちが{elapsed:.1f}秒も続いた"
        assert ticks > 10, f"待機中にループが止まっていた（{ticks}回しか動いていない）"
    asyncio.run(main())


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
