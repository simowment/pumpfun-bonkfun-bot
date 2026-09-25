"""Chain-agnostic token and amount representations."""

from __future__ import annotations

from dataclasses import dataclass

from rugbot.core.models.address import Address


@dataclass(frozen=True, slots=True)
class TokenAmount:
    """Precise currency or token amount tracked in raw integer units with decimals.

    Prevents precision loss from floating-point arithmetic.
    """

    raw_units: int
    decimals: int

    def __post_init__(self) -> None:
        if not isinstance(self.raw_units, int):
            msg = "raw_units must be an integer"
            raise TypeError(msg)
        if not isinstance(self.decimals, int) or self.decimals < 0:
            msg = "decimals must be a non-negative integer"
            raise ValueError(msg)

    @property
    def ui_value(self) -> float:
        """Return human-readable float representation."""
        return self.raw_units / (10**self.decimals)

    @classmethod
    def from_ui(cls, value: float | int, decimals: int) -> TokenAmount:
        """Construct from human-readable float amount."""
        raw_units = round(float(value) * (10**decimals))
        return cls(raw_units=raw_units, decimals=decimals)

    def __str__(self) -> str:
        return f"{self.ui_value:.6f}".rstrip("0").rstrip(".")


@dataclass(frozen=True, slots=True)
class TokenMetadata:
    """Universal token descriptor across all supported chains."""

    address: Address
    symbol: str
    name: str
    decimals: int
    total_supply: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.address, Address):
            msg = "address must be an instance of Address"
            raise TypeError(msg)
        if not self.symbol:
            msg = "symbol cannot be empty"
            raise ValueError(msg)
