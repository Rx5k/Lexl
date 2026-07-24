"""会社の開始（/economy 会計のリセット）スクリプト。

テスト運用の会計を片付けて、まっさらな /economy から会社を開始するための保守ツール。

やること:
1. 入金したユーザーへ **返金**（入金分＝換金可能枠 Σ入金−Σ換金gross に対し、本番同様
   10% 手数料を引いた net を `POST /payout` でよあコイン払い出し）。無料ファーム分は返金
   しないので **会社は損しない**。手数料分は会社利益として残る。
2. **/economy の会計をリセット**（全ユーザーのリリーコイン残高を0＝負債0、取引履歴・
   換金申請・換金状態を消去 → 入金/消費/配布/手数料/換金枠/日次純増が0に戻る）。
   ※生き物・図鑑・バッジ・アイテム・ジェムなどの**ゲーム進捗は保持**する。
3. 準備金（会社の資本金）は**リセットしない**。返金で払い出した net の分だけ差し引く。
4. payment_cursor / processed_payments は**残す**（過去のテスト入金の二重取込を防ぐ）。

安全のため既定は **ドライラン**（何も変更せず計画だけ表示）。実際に実行するには --execute。
返金の払い出しが1件でも失敗した場合はリセットを中止する（誰も損しないように）。

使い方:
    python -m scripts.reset_company              # ドライラン（計画表示のみ）
    python -m scripts.reset_company --execute    # 実行（返金→/economy会計リセット）
    python -m scripts.reset_company --execute --no-refund   # 返金せず会計リセットのみ
"""
from __future__ import annotations

import argparse
import asyncio

import game
from config import Config
from db import Database


async def _compute_refunds(db: Database, fee_bps: int) -> list[dict]:
    """各ユーザーの返金予定（入金分に10%手数料）。net>0 のみ返す。"""
    plans = []
    for uid in await db.all_user_ids():
        gross = await db.withdrawable(uid)  # 入金分（未換金）
        if gross <= 0:
            continue
        net, fee = game.withdraw_split(gross, fee_bps)
        if net <= 0:
            continue
        plans.append({"uid": uid, "gross": gross, "net": net, "fee": fee})
    return plans


async def run(execute: bool, do_refund: bool, full: bool) -> dict:
    cfg = Config.load()
    db = Database(cfg.db_path, start_reserve=cfg.company_start_reserve)
    await db.connect()

    try:
        plans = await _compute_refunds(db, cfg.withdraw_fee_bps) if do_refund else []
        refund_net = sum(p["net"] for p in plans)
        refund_fee = sum(p["fee"] for p in plans)
        reserve_before = await db.get_reserve()
        users = len(await db.all_user_ids())

        scope = "完全リセット（ゲームデータ全消去）" if full else "/economy 会計リセット"
        print("=" * 60)
        print(f"会社リセット【{scope}】" + ("【実行】" if execute else "【ドライラン：変更なし】"))
        print("=" * 60)
        print(f"対象ユーザー数           : {users}")
        print(f"返金対象（入金分・net>0）: {len(plans)} 人")
        print(f"返金合計 net（払出）     : {refund_net:,} よあコイン")
        print(f"返金手数料（会社利益）   : {refund_fee:,} よあコイン（10%）")
        print(f"準備金（資本金）         : {reserve_before:,} → {reserve_before - refund_net:,}"
              f"（リセットせず、返金分のみ差引）")
        if full:
            print("消去範囲                 : 全ユーザーのリリーコイン・ジェム・生き物・図鑑・"
                  "バッジ・アイテム・クエスト・ログイン等をすべて消去")
            print("保持されるもの           : 準備金・入金カーソル")
        else:
            print("会計リセット             : 全ユーザーのリリーコインを0・取引履歴/換金申請/換金状態を消去")
            print("保持されるもの           : 生き物・図鑑・バッジ・アイテム・ジェム・入金カーソル")
        for p in plans[:20]:
            print(f"  - user {p['uid']}: 入金分 {p['gross']:,} → 返金 {p['net']:,}（手数料 {p['fee']:,}）")
        if len(plans) > 20:
            print(f"  … ほか {len(plans) - 20} 人")

        if not execute:
            print("\n[ドライラン] --execute を付けると実際に返金・リセットします。")
            return {"executed": False, "users": users, "refund_net": refund_net,
                    "refund_fee": refund_fee, "plans": len(plans)}

        # --- 実行: まず返金（冪等キーは安定＝再実行しても二重払いしない）---
        paid_total = 0
        failed: list[str] = []
        if plans:
            from api.yoacoin_client import YoacoinAPIError, YoacoinClient
            async with YoacoinClient(cfg.base_url, cfg.api_key) as client:
                for p in plans:
                    try:
                        await client.payout(
                            p["uid"], p["net"],
                            idempotency_key=f"reset-{p['uid']}", kind="reward")
                        paid_total += p["net"]
                    except YoacoinAPIError as e:
                        failed.append(f"user {p['uid']}: {e}")

        if failed:
            # 1件でも失敗したらリセットしない（誰も損しないように）。冪等キーが安定なので
            # API復旧後に再実行すれば、成功済みユーザーは二重払いされない。
            print("\n⚠️ 返金に失敗したユーザーがいます。会計リセットは中止しました。")
            for f in failed[:20]:
                print(f"  ! {f}")
            print("API を確認して再実行してください（成功済みは二重払いされません）。")
            return {"executed": False, "aborted": True, "paid": paid_total, "failed": len(failed)}

        # --- 全返金成功 → リセット ---
        if full:
            await db.wipe_all_game_data(paid_total)
            done = "全ゲームデータ消去"
        else:
            await db.reset_economy(paid_total)
            done = "リリーコイン残高0・会計履歴消去"
        reserve_after = await db.get_reserve()
        print(f"\n✅ 完了: 返金 {paid_total:,} よあコイン払出 / "
              f"{done} / 準備金 {reserve_before:,} → {reserve_after:,}。")
        return {"executed": True, "users": users, "paid": paid_total,
                "reserve_after": reserve_after}
    finally:
        await db.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="会社を開始（リセット）する")
    ap.add_argument("--execute", action="store_true", help="実際に実行（既定はドライラン）")
    ap.add_argument("--no-refund", action="store_true", help="返金せずリセットのみ")
    ap.add_argument("--full", action="store_true",
                    help="完全リセット（生き物/図鑑/バッジ/アイテム/ジェムなどゲームデータも全消去）")
    args = ap.parse_args()
    asyncio.run(run(execute=args.execute, do_refund=not args.no_refund, full=args.full))


if __name__ == "__main__":
    main()
