"""同時実行下での台帳の整合性テスト。

背景: aiosqlite の接続は全ユーザーで1本を共有しており、commit()/rollback() は
「その接続で未確定の全変更」に効く。書き込みメソッドが execute と commit の間で
他タスクに制御を譲ると、他人の rollback で自分の減算が巻き戻され、コインを
払わずにアイテムを得られてしまう（＝コイン増殖）。

ここでは実際に多数の操作を並行実行し、コインの総量が保存されることを検証する。

pytest でも、`python -m tests.test_concurrency` でも実行できる。
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from db import Database


def _run(coro):
    return asyncio.run(coro)


async def _fresh_db() -> tuple[Database, Path]:
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    db = Database(str(tmp), start_reserve=30000)
    await db.connect()
    return db, tmp


def test_concurrent_spend_does_not_duplicate_coins():
    """成功する消費と失敗する消費を混ぜて並行実行しても、コインが増えないこと。

    修正前は、残高不足で失敗した側の rollback() が、別ユーザーの未確定の減算を
    巻き戻していた（支払わずに消費が成功＝増殖）。
    """
    async def main():
        db, _ = await _fresh_db()
        try:
            rich, poor = 1, 2
            await db.add_coins(rich, 10_000, "test")
            await db.add_coins(poor, 0, "test")

            n = 200
            cost = 50
            # 成功する消費（rich）と必ず失敗する消費（poor: 残高0）を交互に並行実行
            tasks = []
            for _ in range(n):
                tasks.append(db.try_spend_coins(rich, cost, "explore"))
                tasks.append(db.try_spend_coins(poor, cost, "explore"))
            results = await asyncio.gather(*tasks)

            ok_rich = sum(1 for r in results[0::2] if r)
            ok_poor = sum(1 for r in results[1::2] if r)
            assert ok_rich == n, f"rich の消費が {ok_rich}/{n} しか成功していない"
            assert ok_poor == 0, f"残高0のはずの poor が {ok_poor} 回消費できてしまった"

            bal = await db.get_balance(rich)
            expected = 10_000 - cost * n
            assert bal.coins == expected, (
                f"コインが増殖/消失した: 実際={bal.coins:,} 期待={expected:,} "
                f"(差分 {bal.coins - expected:+,})"
            )
            # 台帳(transactions)の合計も残高と一致すること
            async with db.conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS s FROM transactions "
                "WHERE user_id=? AND currency='coins'", (rich,)
            ) as cur:
                logged = (await cur.fetchone())["s"]
            assert logged == expected, f"台帳合計 {logged:,} と残高 {expected:,} が不一致"
        finally:
            await db.close()

    _run(main())


def test_concurrent_buy_gems_is_atomic():
    """ジェム購入（コイン消費＋ジェム付与）が並行実行でも片側だけ成立しないこと。"""
    async def main():
        db, _ = await _fresh_db()
        try:
            uid = 1
            price, gems_each, n = 100, 1, 50
            await db.add_coins(uid, price * n, "test")

            results = await asyncio.gather(
                *[db.buy_gems(uid, gems_each, price) for _ in range(n)]
            )
            assert all(results), "残高は足りているのに購入が失敗した"

            bal = await db.get_balance(uid)
            assert bal.coins == 0, f"コインが残っている: {bal.coins:,}"
            assert bal.gems == gems_each * n, (
                f"ジェム数が不一致: 実際={bal.gems} 期待={gems_each * n}"
            )
        finally:
            await db.close()

    _run(main())


def test_concurrent_withdraw_respects_reserve_floor():
    """並行換金でも準備金フロアを割らず、負債と準備金の整合が保たれること。"""
    async def main():
        db, _ = await _fresh_db()
        try:
            floor = 29_000  # 準備金30,000に対しフロアを高くし、少数しか通らない状況に
            users = list(range(1, 21))
            for uid in users:
                await db.deposit_yoacoin(uid, 1_000)  # 準備金 +1,000 / coins +1,000

            reserve_before = await db.get_reserve()
            # 各自 1,000 リリー(net 900 / fee 100)を同時に換金
            results = await asyncio.gather(
                *[db.withdraw_to_yoacoin(uid, 1_000, 900, 100, floor, queue_only=False)
                  for uid in users]
            )

            paid = sum(1 for r in results if r)
            reserve_after = await db.get_reserve()
            assert reserve_after == reserve_before - 900 * paid, (
                f"準備金が払い出し件数と不整合: after={reserve_after:,} "
                f"before={reserve_before:,} paid={paid}"
            )
            assert reserve_after >= floor, (
                f"準備金フロア {floor:,} を割った: {reserve_after:,}"
            )
            # 成功した人だけコインが減っていること
            for uid, ok in zip(users, results):
                bal = await db.get_balance(uid)
                assert bal.coins == (0 if ok else 1_000), (
                    f"user={uid} ok={ok} なのに残高 {bal.coins:,}"
                )
        finally:
            await db.close()

    _run(main())


def test_concurrent_quest_claim_is_once_only():
    """同じクエスト報酬を同時に受け取ろうとしても1回しか成立しないこと。"""
    async def main():
        db, _ = await _fresh_db()
        try:
            uid, qid, period = 1, "q_test", "2026-07-25"
            await db.ensure_quest(uid, qid, period, target=1)
            await db.bump_quest(uid, qid, period, target=1, delta=1)

            results = await asyncio.gather(
                *[db.try_claim_quest(uid, qid, period) for _ in range(30)]
            )
            assert sum(1 for r in results if r) == 1, (
                f"報酬が {sum(1 for r in results if r)} 回受け取れてしまった"
            )
        finally:
            await db.close()

    _run(main())


def test_concurrent_quest_progress_not_lost():
    """並行して進捗を加算しても取りこぼしが起きないこと（read-modify-write の保護）。"""
    async def main():
        db, _ = await _fresh_db()
        try:
            uid, qid, period, target = 1, "q_test", "2026-07-25", 100
            await asyncio.gather(
                *[db.bump_quest(uid, qid, period, target, 1) for _ in range(target)]
            )
            row = await db.get_quest(uid, qid, period)
            assert row["progress"] == target, (
                f"進捗が取りこぼされた: {row['progress']}/{target}"
            )
        finally:
            await db.close()

    _run(main())


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
