"""Multiplexed native Solana WebSocket trigger for tracked wallet activity."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import websockets

from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = get_logger(__name__)

LOGS_SUBSCRIBE: Final[str] = "logsSubscribe"
LOGS_NOTIFICATION: Final[str] = "logsNotification"
FINALIZED: Final[str] = "finalized"
RECONNECT_DELAY_SECONDS: Final[float] = 1.0
MAX_RECONNECT_DELAY_SECONDS: Final[float] = 30.0


class SolanaLogsStreamError(ValueError):
    """Raised when the native Solana WSS configuration is invalid."""


@dataclass(frozen=True, slots=True)
class WalletLogNotification:
    """One successful finalized transaction notification for a tracked wallet."""

    wallet: str
    signature: str
    slot: int


class SolanaLogsStream:
    """Maintain one native WSS connection with one logs subscription per wallet.

    ``logsSubscribe`` permits one mentioned wallet per subscription, while a
    single socket may carry many subscriptions. This object owns that fan-in and
    returns only successful, finalized transaction triggers. Callers must still
    hydrate the signature through finalized HTTP RPC before interpreting it.
    """

    def __init__(self, websocket_endpoint: str) -> None:
        """Initialize the stream for one configured Solana WSS endpoint."""

        if not websocket_endpoint.strip():
            raise SolanaLogsStreamError
        self._websocket_endpoint = websocket_endpoint
        self._wallets: tuple[str, ...] = ()
        self._websocket: object | None = None
        self._request_wallets: dict[int, str] = {}
        self._subscription_wallets: dict[int, str] = {}
        self._pending_notifications: list[dict[str, object]] = []
        self._next_request_id = 1
        self._reconcile_lock = asyncio.Lock()
        self._wallets_available = asyncio.Event()
        self._connected = False
        self._failed = False

    @property
    def connected(self) -> bool:
        """Whether the shared socket is currently connected."""

        return self._connected

    @property
    def failed(self) -> bool:
        """Whether at least one connection or receive attempt has failed."""

        return self._failed

    async def reconcile(self, wallets: Iterable[str]) -> None:
        """Replace the active wallet subscription set on the shared socket."""

        next_wallets = tuple(sorted(set(wallets)))
        async with self._reconcile_lock:
            if next_wallets == self._wallets and self._websocket is not None:
                return
            self._wallets = next_wallets
            if next_wallets:
                self._wallets_available.set()
            else:
                self._wallets_available.clear()
            await self._close()

    async def next_notification(self) -> WalletLogNotification:
        """Wait for the next successful finalized transaction trigger."""

        reconnect_delay = RECONNECT_DELAY_SECONDS
        while True:
            if not self._wallets:
                await self._wallets_available.wait()
                reconnect_delay = RECONNECT_DELAY_SECONDS
            pending = self._pop_pending_notification()
            if pending is not None:
                return pending
            try:
                notification = await self._read_notification()
            except (OSError, ValueError, websockets.WebSocketException):
                self._failed = True
                logger.warning(
                    "Solana WebSocket disconnected; retrying in %.1f seconds",
                    reconnect_delay,
                )
                await self._close()
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY_SECONDS)
            else:
                if notification is not None:
                    reconnect_delay = RECONNECT_DELAY_SECONDS
                    return notification

    async def _read_notification(self) -> WalletLogNotification | None:
        if self._websocket is None:
            await self._connect()
        websocket = self._websocket
        if websocket is None:
            return None
        payload = _parse_message(await websocket.recv())
        if payload is None:
            return None
        response_id = payload.get("id")
        subscription_id = payload.get("result")
        if type(response_id) is int and type(subscription_id) is int:
            wallet = self._request_wallets.pop(response_id, None)
            if wallet is not None:
                self._subscription_wallets[subscription_id] = wallet
            return None
        if payload.get("method") != LOGS_NOTIFICATION:
            return None
        notification = _notification_from_payload(payload, self._subscription_wallets)
        if notification is None:
            self._pending_notifications.append(payload)
        return notification

    async def close(self) -> None:
        """Close the shared socket and discard pending subscription state."""

        async with self._reconcile_lock:
            self._wallets = ()
            self._wallets_available.clear()
            await self._close()

    async def _connect(self) -> None:
        if not self._wallets:
            return
        websocket = await websockets.connect(self._websocket_endpoint)
        self._websocket = websocket
        self._connected = True
        self._failed = False
        self._request_wallets.clear()
        self._subscription_wallets.clear()
        self._pending_notifications.clear()
        for wallet in self._wallets:
            request_id = self._next_request_id
            self._next_request_id += 1
            self._request_wallets[request_id] = wallet
            await websocket.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": LOGS_SUBSCRIBE,
                        "params": [
                            {"mentions": [wallet]},
                            {"commitment": FINALIZED},
                        ],
                    },
                    separators=(",", ":"),
                )
            )

    async def _close(self) -> None:
        websocket = self._websocket
        self._websocket = None
        self._connected = False
        self._request_wallets.clear()
        self._subscription_wallets.clear()
        self._pending_notifications.clear()
        if websocket is not None:
            await websocket.close()

    def _pop_pending_notification(self) -> WalletLogNotification | None:
        while self._pending_notifications:
            payload = self._pending_notifications.pop(0)
            notification = _notification_from_payload(
                payload, self._subscription_wallets
            )
            if notification is not None:
                return notification
        return None


def _parse_message(message: object) -> dict[str, object] | None:
    if isinstance(message, bytes):
        message = message.decode("utf-8")
    if not isinstance(message, str):
        return None
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        return None
    return payload if type(payload) is dict else None


def _notification_from_payload(
    payload: dict[str, object], subscription_wallets: dict[int, str]
) -> WalletLogNotification | None:
    params = payload.get("params")
    if type(params) is not dict:
        return None
    subscription = params.get("subscription")
    result = params.get("result")
    if type(subscription) is not int or type(result) is not dict:
        return None
    wallet = subscription_wallets.get(subscription)
    context = result.get("context")
    value = result.get("value")
    if wallet is None or type(context) is not dict or type(value) is not dict:
        return None
    slot = context.get("slot")
    signature = value.get("signature")
    if value.get("err") is not None or type(slot) is not int or slot < 0:
        return None
    if type(signature) is not str or not signature:
        return None
    return WalletLogNotification(wallet=wallet, signature=signature, slot=slot)


__all__ = ["SolanaLogsStream", "SolanaLogsStreamError", "WalletLogNotification"]
