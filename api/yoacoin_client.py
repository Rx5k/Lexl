"""よあコイン API の非同期クライアント（会社向けの最小利用）。

一般会社はチップ操作(/chips)ができないため、このBotが使うのは
- GET /health   ヘルスチェック（認証不要）
- GET /company  自社情報（資金・種別）
- GET /payments 入金検知（since / limit のカーソル式ポーリング）
の3つのみ。

スモークテスト:
    python -m api.yoacoin_client
"""
from __future__ import annotations

import asyncio
import socket
import ssl
from dataclasses import dataclass

import aiohttp

# IPv6 が繋がらない環境では、接続時に IPv6 を先に試して長時間ハングすることがある
# （aiohttp の happy-eyeballs が両系統を並行して試すため）。IPv4 に固定すると
# その待ちが消える。ホストのIPv6が正常なら False にしてよい。
FORCE_IPV4 = True


def _make_connector() -> aiohttp.TCPConnector:
    """接続の待ちでイベントループが詰まらないようにしたコネクタ。"""
    return aiohttp.TCPConnector(
        ssl=_make_ssl_context(),
        family=socket.AF_INET if FORCE_IPV4 else socket.AF_UNSPEC,
        limit=20,
        ttl_dns_cache=300,     # DNSを都度引き直さない（名前解決失敗の連発を緩和）
    )


def _err_detail(e: BaseException) -> str:
    """例外の説明。str() が空になる例外が多いので型名で補う。"""
    return str(e) or e.__class__.__name__


def _make_ssl_context() -> ssl.SSLContext:
    """信頼チェーン・ホスト名・有効期限の検証は維持しつつ、厳格RFC適合チェックだけ緩める。

    Python 3.13+ は既定で VERIFY_X509_STRICT を有効化した。よあコインAPIサーバの証明書は
    中間/ルートCAの basicConstraints を critical にしていないため、厳格検証だと
    「Basic Constraints of CA cert not marked critical」で接続が弾かれる。ここでは
    その厳格フラグのみ外し、通常の証明書検証（改ざん・なりすまし対策）は維持する。
    """
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


class YoacoinAPIError(Exception):
    """API がエラーを返したとき（status と本文の error を保持）。"""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"[{status}] {message}")


@dataclass
class Payment:
    id: int
    user_id: int
    amount: int
    reason: str
    at: str

    @classmethod
    def from_json(cls, d: dict) -> "Payment":
        return cls(
            id=int(d["id"]),
            user_id=int(d["user_id"]),
            amount=int(d["amount"]),
            reason=str(d.get("reason", "")),
            at=str(d.get("at", "")),
        )


@dataclass
class PaymentsPage:
    payments: list[Payment]
    next_cursor: int
    count: int


class YoacoinClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 10.0):
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        # 接続段階にも個別の上限を設ける。到達できない相手に総timeoutぶん
        # ぶら下がると、その間ポーラが次に進めず再接続が積み上がる。
        self._timeout = aiohttp.ClientTimeout(
            total=timeout, connect=5.0, sock_connect=5.0, sock_read=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "YoacoinClient":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout, connector=_make_connector())

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def _s(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("YoacoinClient.start() が呼ばれていません")
        return self._session

    async def _get(self, path: str, *, auth: bool = True, params: dict | None = None):
        headers = self._headers if auth else {}
        url = f"{self._base}{path}"
        try:
            async with self._s.get(url, headers=headers, params=params) as resp:
                # エラー時は {"error": "reason"} 形式。JSON でないケースも保険で処理。
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = {"error": (await resp.text())[:200]}
                if resp.status >= 400:
                    msg = data.get("error", "unknown error") if isinstance(data, dict) else str(data)
                    raise YoacoinAPIError(resp.status, msg)
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            # 接続不能・SSL・タイムアウト等も YoacoinAPIError に正規化（呼び出し側で一律処理）
            raise YoacoinAPIError(0, f"接続エラー: {_err_detail(e)}") from e

    async def health(self) -> bool:
        """GET /health（認証不要）。到達できれば True。"""
        try:
            await self._get("/health", auth=False)
            return True
        except YoacoinAPIError:
            return False

    async def company(self) -> dict:
        """GET /company 自社情報。"""
        return await self._get("/company")

    async def _post(self, path: str, body: dict):
        url = f"{self._base}{path}"
        try:
            async with self._s.post(url, headers=self._headers, json=body) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = {"error": (await resp.text())[:200]}
                if resp.status >= 400:
                    msg = data.get("error", "unknown error") if isinstance(data, dict) else str(data)
                    raise YoacoinAPIError(resp.status, msg)
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise YoacoinAPIError(0, f"接続エラー: {_err_detail(e)}") from e

    async def payout(
        self, user_id: int, amount: int, idempotency_key: str, kind: str = "withdraw"
    ) -> dict:
        """【提案中の新API】会社→ユーザーのよあコイン付与。

        docs/PAYOUT_API_PROPOSAL.md の仕様を想定。kind は "withdraw"（払い戻し）/
        "reward"（報酬付与）。API未採用の環境では 404 等を返すため、呼び出し側は
        PAYOUT_ENABLED=false のとき本メソッドを呼ばない設計にしている。
        """
        return await self._post(
            "/payout",
            {"user_id": user_id, "amount": amount, "kind": kind,
             "idempotency_key": idempotency_key},
        )

    async def payout_batch(self, items: list[dict], idempotency_key: str) -> dict:
        """【提案中の新API】一括付与。items = [{"user_id", "amount", "kind"}, ...]。

        イベント/デイリー報酬を多人数へ一度に付与する用途（PAYOUT_ENABLED制御）。
        """
        return await self._post(
            "/payout/batch",
            {"items": items, "idempotency_key": idempotency_key},
        )

    async def payouts(self, since: int, limit: int = 100) -> dict:
        """【提案中の新API】払い出し履歴（/payments と対称のカーソル式）。監査用。"""
        limit = max(1, min(200, limit))
        return await self._get("/payouts", params={"since": since, "limit": limit})

    async def user_balance(self, user_id: int) -> int:
        """【提案中の新API】ユーザーのグローバルよあコイン残高を取得（読み取り専用）。

        docs/PAYOUT_API_PROPOSAL.md 3.5。BALANCE_API_ENABLED=false のとき呼ばない。
        """
        data = await self._get(f"/users/{user_id}/balance")
        return int(data.get("balance", 0))

    async def charge(self, user_id: int, amount: int, idempotency_key: str) -> dict:
        """【提案中の新API】ユーザー→会社の課金（/payout の逆方向）。

        docs/PAYOUT_API_PROPOSAL.md 3.6。CHARGE_ENABLED=false のとき呼ばない。
        """
        return await self._post(
            "/charge",
            {"user_id": user_id, "amount": amount, "idempotency_key": idempotency_key},
        )

    async def payments(self, since: int, limit: int = 100) -> PaymentsPage:
        """GET /payments?since=&limit= 入金の検知。"""
        limit = max(1, min(200, limit))  # ドキュメント上の最大は 200
        data = await self._get("/payments", params={"since": since, "limit": limit})
        items = [Payment.from_json(p) for p in data.get("payments", [])]
        return PaymentsPage(
            payments=items,
            next_cursor=int(data.get("next_cursor", since)),
            count=int(data.get("count", len(items))),
        )


async def _smoke() -> None:
    """接続スモークテスト: /health と /company を確認。"""
    from config import Config

    cfg = Config.load()
    if not cfg.api_key:
        print("YOACOIN_API_KEY が未設定です。.env を確認してください。")
        return

    async with YoacoinClient(cfg.base_url, cfg.api_key) as client:
        ok = await client.health()
        print(f"/health -> {'OK' if ok else 'NG'}")
        try:
            info = await client.company()
            print("/company ->", info)
        except YoacoinAPIError as e:
            print("/company -> エラー:", e)
        try:
            page = await client.payments(since=0, limit=5)
            print(f"/payments -> count={page.count} next_cursor={page.next_cursor}")
            for p in page.payments:
                print(f"   入金 id={p.id} user={p.user_id} amount={p.amount} at={p.at}")
        except YoacoinAPIError as e:
            print("/payments -> エラー:", e)


if __name__ == "__main__":
    asyncio.run(_smoke())
