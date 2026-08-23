"""Read-only creator history from the official GMGN CLI."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from decimal import Decimal

from rugbot.domain.decisions import AbstainReason, AbstainResult

DEFAULT_PUBLIC_API_KEY = "gmgn_solbscbaseethmonadtron"
DEFAULT_TIMEOUT_SECONDS = 20
MAX_CREATOR_TOKENS = 5_000
MIN_PUBKEY_LENGTH = 32
MAX_PUBKEY_LENGTH = 44

_READ_ONLY_ENV_KEYS = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SystemRoot",
        "SYSTEMROOT",
        "WINDIR",
        "ComSpec",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "GMGN_API_KEY",
        "GMGN_API_URL",
        "GMGN_BASE_URL",
        "SOLANA_RPC_HTTP",
        "SOLANA_NODE_RPC_ENDPOINT",
        "SOLANA_NODE_WSS_ENDPOINT",
        "HELIUS_API_KEY",
        "HELIUS_RPC_URL",
        "HELIUS_RPC_ENDPOINT",
    }
)


@dataclass(frozen=True, slots=True)
class GmgnCreatorToken:
    """Small, display-safe summary of one creator token."""

    address: str
    symbol: str
    create_timestamp: int
    is_open: bool
    market_cap: str
    token_ath_mc: str
    pool_liquidity: str
    holders: int
    bundler_rate: str
    launchpad_platform: str
    volume_1h: str
    total_fee: str
    cto_flag: bool


@dataclass(frozen=True, slots=True)
class GmgnCreatorHistory:
    """Creator-wide history returned by GMGN.

    This is external, non-finalized enrichment. It is intentionally kept
    separate from the finalized RPC evidence used by adverse-intel decisions.
    """

    source: str
    creator: str
    inner_count: int
    open_count: int
    open_ratio: str
    last_create_timestamp: int | None
    ath_token: str | None
    ath_symbol: str | None
    ath_name: str | None
    ath_market_cap: str | None
    tokens: tuple[GmgnCreatorToken, ...]

    @property
    def total_created_count(self) -> int:
        """Return the provider's historical plus currently open count."""

        return self.inner_count + self.open_count


GmgnCreatorHistoryResult = GmgnCreatorHistory | AbstainResult


async def fetch_gmgn_creator_history(  # noqa: PLR0911
    creator: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> GmgnCreatorHistoryResult:
    """Fetch creator-wide token history through the official read-only CLI.

    The CLI owns GMGN authentication and request details. No signing key or
    transaction capability is loaded. A public testing key is used when the
    user has not configured ``GMGN_API_KEY``.
    """

    if not _looks_like_pubkey(creator):
        return _abstain("creator history requires a valid Solana address")
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        return _abstain("creator history timeout is invalid")
    api_key = os.environ.get("GMGN_API_KEY", DEFAULT_PUBLIC_API_KEY).strip()
    if not api_key:
        return _abstain("GMGN_API_KEY is empty")

    try:
        process = await asyncio.create_subprocess_exec(
            "gmgn-cli.cmd" if sys.platform == "win32" else "gmgn-cli",
            "portfolio",
            "created-tokens",
            "--chain",
            "sol",
            "--wallet",
            creator,
            "--raw",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=read_only_subprocess_environment(api_key),
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds
        )
    except FileNotFoundError:
        return _abstain("gmgn-cli is not installed")
    except TimeoutError:
        return _abstain("GMGN creator history timed out")
    except OSError as error:
        return _abstain(f"GMGN creator history could not start: {type(error).__name__}")

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        return _abstain(
            "GMGN creator history request failed"
            + (f": {detail[:240]}" if detail else "")
        )
    try:
        payload = json.loads(stdout.decode("utf-8"), parse_int=int, parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _abstain("GMGN creator history returned invalid JSON")
    return _parse_payload(payload, creator)


async def fetch_gmgn_dev(  # noqa: PLR0911
    mint: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> str | None:
    """Fetch the GMGN-attributed dev entity wallet for a token mint.

    Returns the ``dev.creator_address`` from ``gmgn-cli token info``, or None
    when the CLI is unavailable, the request fails, or the payload lacks a
    usable creator address. The caller owns fail-closed handling.
    """
    api_key = os.environ.get("GMGN_API_KEY", DEFAULT_PUBLIC_API_KEY).strip()
    if not api_key:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            "gmgn-cli.cmd" if sys.platform == "win32" else "gmgn-cli",
            "token",
            "info",
            "--chain",
            "sol",
            "--address",
            mint,
            "--raw",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=read_only_subprocess_environment(api_key),
        )
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds
        )
    except (FileNotFoundError, TimeoutError, OSError):
        return None
    if process.returncode != 0:
        return None
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    dev = payload.get("dev")
    if not isinstance(dev, dict):
        return None
    creator_address = dev.get("creator_address")
    return (
        creator_address
        if isinstance(creator_address, str) and creator_address
        else None
    )


def creator_history_to_json(
    history: GmgnCreatorHistory | None,
) -> dict[str, object] | None:
    """Serialize optional creator history without exposing provider internals."""

    if history is None:
        return None
    return {
        "source": history.source,
        "creator": history.creator,
        "external_non_finalized": True,
        "decision_input": False,
        "inner_count": history.inner_count,
        "open_count": history.open_count,
        "total_created_count": history.total_created_count,
        "open_ratio": history.open_ratio,
        "last_create_timestamp": history.last_create_timestamp,
        "ath": {
            "token": history.ath_token,
            "symbol": history.ath_symbol,
            "name": history.ath_name,
            "market_cap": history.ath_market_cap,
        },
        "tokens": [
            {
                "address": token.address,
                "symbol": token.symbol,
                "create_timestamp": token.create_timestamp,
                "is_open": token.is_open,
                "market_cap": token.market_cap,
                "token_ath_mc": token.token_ath_mc,
                "pool_liquidity": token.pool_liquidity,
                "holders": token.holders,
                "bundler_rate": token.bundler_rate,
                "launchpad_platform": token.launchpad_platform,
                "volume_1h": token.volume_1h,
                "total_fee": token.total_fee,
                "cto_flag": token.cto_flag,
            }
            for token in history.tokens
        ],
    }


def _parse_payload(payload: object, creator: str) -> GmgnCreatorHistoryResult:
    if not isinstance(payload, dict):
        return _abstain("GMGN creator history payload is malformed")
    inner_count = _int_field(payload, "inner_count")
    open_count = _int_field(payload, "open_count")
    open_ratio = _number_text(payload.get("open_ratio"))
    tokens_raw = payload.get("tokens")
    if (
        inner_count is None
        or open_count is None
        or open_ratio is None
        or not isinstance(tokens_raw, list)
        or len(tokens_raw) > MAX_CREATOR_TOKENS
    ):
        return _abstain("GMGN creator history payload is incomplete")

    tokens: list[GmgnCreatorToken] = []
    for item in tokens_raw:
        token = _parse_token(item)
        if isinstance(token, AbstainResult):
            return token
        tokens.append(token)
    ath = payload.get("creator_ath_info")
    if ath is not None and not isinstance(ath, dict):
        return _abstain("GMGN creator ATH payload is malformed")
    return GmgnCreatorHistory(
        source="gmgn-cli",
        creator=creator,
        inner_count=inner_count,
        open_count=open_count,
        open_ratio=open_ratio,
        last_create_timestamp=_int_field(payload, "last_create_timestamp"),
        ath_token=_str_field(ath, "ath_token") if isinstance(ath, dict) else None,
        ath_symbol=_str_field(ath, "token_symbol") if isinstance(ath, dict) else None,
        ath_name=_str_field(ath, "token_name") if isinstance(ath, dict) else None,
        ath_market_cap=_number_text(ath.get("ath_mc"))
        if isinstance(ath, dict)
        else None,
        tokens=tuple(tokens),
    )


def _parse_token(item: object) -> GmgnCreatorToken | AbstainResult:
    if not isinstance(item, dict):
        return _abstain("GMGN creator token row is malformed")
    address = _str_field(item, "token_address")
    symbol = _str_field(item, "symbol")
    create_timestamp = _int_field(item, "create_timestamp")
    is_open = item.get("is_open")
    market_cap = _number_text(item.get("market_cap"))
    token_ath_mc = _number_text(item.get("token_ath_mc"))
    pool_liquidity = _number_text(item.get("pool_liquidity"))
    holders = _int_field(item, "holders")
    bundler_rate = _number_text(item.get("bundler_rate"))
    launchpad_platform = _str_field(item, "launchpad_platform")
    volume_1h = _number_text(item.get("volume_1h"))
    total_fee = _number_text(item.get("total_fee"))
    cto_flag = item.get("cto_flag")
    if (
        address is None
        or symbol is None
        or create_timestamp is None
        or type(is_open) is not bool
        or market_cap is None
        or token_ath_mc is None
        or pool_liquidity is None
        or holders is None
        or bundler_rate is None
        or launchpad_platform is None
        or volume_1h is None
        or total_fee is None
        or type(cto_flag) is not bool
    ):
        return _abstain("GMGN creator token row is incomplete")
    return GmgnCreatorToken(
        address=address,
        symbol=symbol,
        create_timestamp=create_timestamp,
        is_open=is_open,
        market_cap=market_cap,
        token_ath_mc=token_ath_mc,
        pool_liquidity=pool_liquidity,
        holders=holders,
        bundler_rate=bundler_rate,
        launchpad_platform=launchpad_platform,
        volume_1h=volume_1h,
        total_fee=total_fee,
        cto_flag=cto_flag,
    )


def _int_field(value: object, key: str) -> int | None:
    if not isinstance(value, dict):
        return None
    candidate = value.get(key)
    return candidate if type(candidate) is int and candidate >= 0 else None


def _str_field(value: object, key: str) -> str | None:
    if not isinstance(value, dict):
        return None
    candidate = value.get(key)
    return candidate if isinstance(candidate, str) else None


def _number_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if type(value) is int or isinstance(value, Decimal):
        return str(value)
    return None


def _looks_like_pubkey(value: str) -> bool:
    if (
        not isinstance(value, str)
        or not MIN_PUBKEY_LENGTH <= len(value) <= MAX_PUBKEY_LENGTH
    ):
        return False
    alphabet = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    return all(character in alphabet for character in value)


def _abstain(message: str) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.MISSING_FEATURE,
        message=message,
        as_of_slot=-1,
    )


def read_only_subprocess_environment(api_key: str) -> dict[str, str]:
    """Build the minimal environment permitted for the history subprocess."""

    environment = {
        key: value for key, value in os.environ.items() if key in _READ_ONLY_ENV_KEYS
    }
    environment["GMGN_API_KEY"] = api_key
    return environment
