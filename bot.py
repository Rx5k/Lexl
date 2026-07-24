"""よあコイン 経済×コレクション Bot のエントリポイント。

起動フロー:
1. .env から設定を読み込み、必須値を検証
2. SQLite に接続してスキーマ初期化
3. Cog をロード
4. スラッシュコマンドを同期
5. 入金ポーラをバックグラウンド起動（yoacoin入金→ジェム付与）
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from api.payment_poller import PaymentPoller
from config import Config
from db import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("yoacoin.bot")

INITIAL_COGS = [
    "cogs.economy",
    "cogs.collection",
    "cogs.shop",
    "cogs.profile",
    "cogs.admin",
]


class YoacoinBot(commands.Bot):
    def __init__(self, cfg: Config):
        intents = discord.Intents.default()
        # メッセージ本文は不要（スラッシュコマンド中心）。
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.cfg = cfg
        self.db = Database(cfg.db_path, start_reserve=cfg.company_start_reserve)
        self.poller: PaymentPoller | None = None
        # 通貨絵文字（カスタム絵文字が無ければUnicodeで代用）
        self.emoji_coin = "⚜️"   # リリーコイン
        self.emoji_yc = "🟡"     # よあコイン残高
        self._emojis_loaded = False
        # スラッシュコマンドのクリック可能メンション {name: "</name:id>"}（同期後に構築）
        self.command_mentions: dict[str, str] = {}

    def cmd(self, name: str) -> str:
        """クリックできるコマンドメンションを返す。未同期なら `/name` にフォールバック。"""
        return self.command_mentions.get(name, f"`/{name}`")

    async def setup_hook(self) -> None:
        await self.db.connect()
        log.info("DB接続完了: %s", self.cfg.db_path)

        for ext in INITIAL_COGS:
            await self.load_extension(ext)
            log.info("Cogロード: %s", ext)

        # スラッシュコマンド同期（開発ギルド指定があれば即時反映）
        if self.cfg.dev_guild_id:
            guild = discord.Object(id=self.cfg.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("スラッシュコマンド同期（ギルド %s）: %d件", self.cfg.dev_guild_id, len(synced))
        else:
            synced = await self.tree.sync()
            log.info("スラッシュコマンド同期（グローバル）: %d件 ※反映に最大1時間", len(synced))

        # /help などで使うクリック可能なコマンドメンションを構築
        self.command_mentions = {c.name: c.mention for c in synced}

        # 入金ポーラ起動
        self.poller = PaymentPoller(self.cfg, self.db)
        self.poller.on_deposit = self._on_deposit
        self.poller.start()

    async def _apply_currency_emojis(self) -> None:
        """Botのアプリ絵文字を名前(lily/yoa)で解決し、各Cogの表示絵文字に反映する。

        絵文字が見つからなければUnicode（⚜️/🟡）のまま。カスタム絵文字はBotの
        アプリケーション絵文字として追加しておくこと（Developer Portal → Emojis）。
        """
        import importlib

        mapping: dict[str, str] = {}
        try:
            for e in await self.fetch_application_emojis():
                mapping[e.name] = str(e)
        except Exception:
            log.warning("アプリ絵文字の取得に失敗。Unicode絵文字で代用します。")

        coin = mapping.get("lily")
        yc = mapping.get("yoa")
        if coin:
            self.emoji_coin = coin
        if yc:
            self.emoji_yc = yc

        # 各Cogモジュールの COIN / YC 定数を差し替え（f-string は実行時に参照する）
        for name in INITIAL_COGS:
            mod = importlib.import_module(name)
            if coin and hasattr(mod, "COIN"):
                mod.COIN = coin
            if yc and hasattr(mod, "YC"):
                mod.YC = yc

        log.info("通貨絵文字: リリーコイン=%s / よあコイン=%s",
                 coin or "⚜️(代用)", yc or "🟡(代用)")

    async def _on_deposit(self, discord_user_id: int, amount: int, yc: int) -> None:
        """入金→リリーコイン変換完了の通知（DM＋任意で通知チャンネル）。失敗しても無視。"""
        # DM通知
        try:
            user = self.get_user(discord_user_id) or await self.fetch_user(discord_user_id)
            if user is not None:
                await user.send(
                    f"{self.emoji_coin} 入金を確認しました！ **{amount:,} よあコイン** → "
                    f"同額の**リリーコイン**を付与しました。`/explore` などで遊べます！"
                )
        except Exception:
            log.debug("入金DM通知に失敗 (user=%s)", discord_user_id)

        # チャンネル通知（NOTIFY_CHANNEL_ID 設定時）
        if self.cfg.notify_channel_id:
            try:
                ch = (self.get_channel(self.cfg.notify_channel_id)
                      or await self.fetch_channel(self.cfg.notify_channel_id))
                if ch is not None:
                    await ch.send(
                        f"{self.emoji_coin} <@{discord_user_id}> さんの入金 "
                        f"**{amount:,} よあコイン** → 同額の**リリーコイン**に変換完了！"
                    )
            except Exception:
                log.debug("入金チャンネル通知に失敗 (channel=%s)", self.cfg.notify_channel_id)

    async def on_ready(self) -> None:
        log.info("ログイン完了: %s (id=%s)", self.user, self.user.id if self.user else "?")
        if not self._emojis_loaded:
            self._emojis_loaded = True
            await self._apply_currency_emojis()

    async def close(self) -> None:
        if self.poller is not None:
            await self.poller.stop()
        await self.db.close()
        await super().close()


def main() -> None:
    cfg = Config.load()
    cfg.require_for_bot()
    bot = YoacoinBot(cfg)
    bot.run(cfg.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
