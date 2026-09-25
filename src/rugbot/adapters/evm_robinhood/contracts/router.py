"""Uniswap V2/V3 compatible router ABI calldata encoding."""

from __future__ import annotations

import time

from rugbot.adapters.evm_robinhood.contracts.erc20 import (
    encode_address_arg,
    encode_uint256_arg,
)

SWAP_EXACT_ETH_FOR_TOKENS = bytes.fromhex("7ff36ab5")
SWAP_EXACT_TOKENS_FOR_ETH = bytes.fromhex("18c6080d")
GET_AMOUNTS_OUT = bytes.fromhex("d06ca61f")
DEFAULT_DEADLINE_SECONDS = 300


def encode_path_array(path: list[str]) -> bytes:
    """Encode dynamic address[] array in standard ABI format."""
    # Length of array as 32-byte word
    encoded = encode_uint256_arg(len(path))
    for addr in path:
        encoded += encode_address_arg(addr)
    return encoded


def encode_swap_exact_eth_for_tokens(
    amount_out_min: int,
    path: list[str],
    recipient: str,
    deadline: int | None = None,
) -> str:
    """Encode calldata for swapExactETHForTokens(amountOutMin, path, to, deadline)."""
    deadline_ts = deadline or (int(time.time()) + DEFAULT_DEADLINE_SECONDS)

    # Offset to dynamic parameter 'path': 4 parameters = 4 * 32 = 128 bytes (0x80)
    path_offset = 128

    calldata = (
        SWAP_EXACT_ETH_FOR_TOKENS
        + encode_uint256_arg(amount_out_min)
        + encode_uint256_arg(path_offset)
        + encode_address_arg(recipient)
        + encode_uint256_arg(deadline_ts)
        + encode_path_array(path)
    )
    return "0x" + calldata.hex()


def encode_swap_exact_tokens_for_eth(
    amount_in: int,
    amount_out_min: int,
    path: list[str],
    recipient: str,
    deadline: int | None = None,
) -> str:
    """Encode calldata for swapExactTokensForETH(amountIn, amountOutMin, path, to, deadline)."""
    deadline_ts = deadline or (int(time.time()) + DEFAULT_DEADLINE_SECONDS)

    # Offset to dynamic parameter 'path': 5 parameters = 5 * 32 = 160 bytes (0xa0)
    path_offset = 160

    calldata = (
        SWAP_EXACT_TOKENS_FOR_ETH
        + encode_uint256_arg(amount_in)
        + encode_uint256_arg(amount_out_min)
        + encode_uint256_arg(path_offset)
        + encode_address_arg(recipient)
        + encode_uint256_arg(deadline_ts)
        + encode_path_array(path)
    )
    return "0x" + calldata.hex()


def encode_get_amounts_out(amount_in: int, path: list[str]) -> str:
    """Encode calldata for getAmountsOut(amountIn, path)."""
    # Offset to dynamic parameter 'path': 2 parameters = 2 * 32 = 64 bytes (0x40)
    path_offset = 64

    calldata = (
        GET_AMOUNTS_OUT
        + encode_uint256_arg(amount_in)
        + encode_uint256_arg(path_offset)
        + encode_path_array(path)
    )
    return "0x" + calldata.hex()
