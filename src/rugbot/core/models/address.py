"""Normalized blockchain-agnostic Address value object."""

from __future__ import annotations

from dataclasses import dataclass

EVM_ADDRESS_LENGTH = 42
SOLANA_MIN_ADDRESS_LENGTH = 32
SOLANA_MAX_ADDRESS_LENGTH = 44


@dataclass(frozen=True, slots=True)
class Address:
    """Opaque, immutable identifier representing an account or contract address.

    Attributes:
        raw: The raw string address (e.g. Base58 for Solana, Hex for EVM).
        chain_id: Canonical chain identifier (e.g. 'solana:mainnet', 'evm:robinhood_orbit').
    """

    raw: str
    chain_id: str = "solana:mainnet"

    def __post_init__(self) -> None:
        if not self.raw or not isinstance(self.raw, str):
            msg = "Address raw value must be a non-empty string"
            raise ValueError(msg)
        if not self.chain_id or not isinstance(self.chain_id, str):
            msg = "Address chain_id must be a non-empty string"
            raise ValueError(msg)

    def __str__(self) -> str:
        return self.raw

    def __repr__(self) -> str:
        return f"Address({self.raw!r}, chain_id={self.chain_id!r})"

    def is_evm(self) -> bool:
        """Return True if this address represents an EVM hex address."""
        return self.raw.startswith("0x") and len(self.raw) == EVM_ADDRESS_LENGTH

    def is_solana(self) -> bool:
        """Return True if this address represents a Solana base58 address."""
        return (
            not self.raw.startswith("0x")
            and SOLANA_MIN_ADDRESS_LENGTH <= len(self.raw) <= SOLANA_MAX_ADDRESS_LENGTH
        )
