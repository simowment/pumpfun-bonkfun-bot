"""ERC-20 ABI calldata encoding and return decoding."""

from __future__ import annotations

# Function Selectors (keccak256 hash first 4 bytes)
BALANCE_OF_SELECTOR = bytes.fromhex("70a08231")  # balanceOf(address)
ALLOWANCE_SELECTOR = bytes.fromhex("dd62ed3e")  # allowance(address,address)
APPROVE_SELECTOR = bytes.fromhex("095ea7b3")  # approve(address,uint256)
DECIMALS_SELECTOR = bytes.fromhex("313ce567")  # decimals()
SYMBOL_SELECTOR = bytes.fromhex("95d89b41")  # symbol()
NAME_SELECTOR = bytes.fromhex("06fdde03")  # name()

MAX_UINT256 = (1 << 256) - 1


def encode_address_arg(address_hex: str) -> bytes:
    """Pad an EVM address string into a 32-byte word."""
    clean = address_hex.lower()
    if clean.startswith("0x"):
        clean = clean[2:]
    return bytes.fromhex(clean.zfill(64))


def encode_uint256_arg(value: int) -> bytes:
    """Pad an integer into a 32-byte big-endian word."""
    return value.to_bytes(32, byteorder="big")


def encode_balance_of(account_hex: str) -> str:
    """Encode calldata for balanceOf(address)."""
    calldata = BALANCE_OF_SELECTOR + encode_address_arg(account_hex)
    return "0x" + calldata.hex()


def encode_allowance(owner_hex: str, spender_hex: str) -> str:
    """Encode calldata for allowance(address,address)."""
    calldata = (
        ALLOWANCE_SELECTOR
        + encode_address_arg(owner_hex)
        + encode_address_arg(spender_hex)
    )
    return "0x" + calldata.hex()


def encode_approve(spender_hex: str, value: int = MAX_UINT256) -> str:
    """Encode calldata for approve(address,uint256)."""
    calldata = (
        APPROVE_SELECTOR + encode_address_arg(spender_hex) + encode_uint256_arg(value)
    )
    return "0x" + calldata.hex()


def encode_decimals() -> str:
    """Encode calldata for decimals()."""
    return "0x" + DECIMALS_SELECTOR.hex()


def decode_uint256(data_hex: str) -> int:
    """Decode a 32-byte integer response from an eth_call."""
    clean = data_hex.lower()
    if clean.startswith("0x"):
        clean = clean[2:]
    if not clean:
        return 0
    return int(clean, 16)
