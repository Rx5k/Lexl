"""環境変数の読み込みと設定値の一元管理。"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    discord_token: str
    api_key: str
    base_url: str
    db_path: str
    poll_interval: int
    dev_guild_id: int | None
    # --- 経済（よあコイン建て） ---
    company_start_reserve: int   # 会社の初期準備金（よあコイン）
    reserve_floor: int           # これを割る出金は拒否（破綻防止）
    withdraw_fee_bps: int        # 出金手数料（basis point, 既定1000=10%）
    min_withdraw: int            # 最低出金額
    withdraw_cooldown: int       # 出金クールダウン（秒）
    gem_price_coins: int         # ジェム1個の価格（リリーコイン）
    payout_enabled: bool         # 新API(payout)が使えるか。falseなら申請キュー
    balance_api_enabled: bool    # GET /users/{id}/balance が使えるか（/balanceで実残高表示）
    charge_enabled: bool         # POST /charge が使えるか（よあコイン→リリーコイン即時交換等）
    company_name: str            # y!支払 で使う会社名（/deposit の案内に表示）
    notify_channel_id: int | None  # 入金完了をお知らせするチャンネルID（任意）
    owner_id: int                # 社長（管理コマンドを実行できる唯一のユーザーID）

    @classmethod
    def load(cls) -> "Config":
        token = os.getenv("DISCORD_TOKEN", "").strip()
        api_key = os.getenv("YOACOIN_API_KEY", "").strip()
        base_url = os.getenv(
            "YOACOIN_BASE_URL", "https://yoacoin-api.keitodaze.net/api/v1"
        ).strip().rstrip("/")
        db_path = os.getenv("DB_PATH", "yoacoin_bot.db").strip() or "yoacoin_bot.db"
        poll_interval = max(2, _get_int("POLL_INTERVAL_SECONDS", 5))

        dev_guild_raw = os.getenv("DEV_GUILD_ID", "").strip()
        dev_guild_id = int(dev_guild_raw) if dev_guild_raw.isdigit() else None

        return cls(
            discord_token=token,
            api_key=api_key,
            base_url=base_url,
            db_path=db_path,
            poll_interval=poll_interval,
            dev_guild_id=dev_guild_id,
            company_start_reserve=max(0, _get_int("COMPANY_START_RESERVE", 30000)),
            reserve_floor=max(0, _get_int("RESERVE_FLOOR", 5000)),
            withdraw_fee_bps=max(0, min(10000, _get_int("WITHDRAW_FEE_BPS", 1000))),
            min_withdraw=max(1, _get_int("MIN_WITHDRAW", 100)),
            withdraw_cooldown=max(0, _get_int("WITHDRAW_COOLDOWN", 300)),
            gem_price_coins=max(1, _get_int("GEM_PRICE_COINS", 120)),
            payout_enabled=_get_bool("PAYOUT_ENABLED", True),
            balance_api_enabled=_get_bool("BALANCE_API_ENABLED", False),
            charge_enabled=_get_bool("CHARGE_ENABLED", False),
            company_name=os.getenv("COMPANY_NAME", "おちんぽグループ").strip() or "おちんぽグループ",
            notify_channel_id=(
                int(os.getenv("NOTIFY_CHANNEL_ID", "").strip())
                if os.getenv("NOTIFY_CHANNEL_ID", "").strip().isdigit() else None
            ),
            owner_id=_get_int("OWNER_ID", 1084039929878810624),
        )

    def require_for_bot(self) -> None:
        """Bot起動に必須の値が揃っているか検証。"""
        missing = []
        if not self.discord_token:
            missing.append("DISCORD_TOKEN")
        if not self.api_key:
            missing.append("YOACOIN_API_KEY")
        if missing:
            raise RuntimeError(
                "必須の環境変数が未設定です: " + ", ".join(missing) +
                "\n.env.example をコピーして .env を作成し、値を設定してください。"
            )
