"""Integer unit types used by executable quote and replay logic."""

from typing import Final, NewType

Lamports = NewType("Lamports", int)
TokenBaseUnits = NewType("TokenBaseUnits", int)
QuoteBaseUnits = NewType("QuoteBaseUnits", int)
Slot = NewType("Slot", int)
ComputeUnitLimit = NewType("ComputeUnitLimit", int)
MicroLamportsPerComputeUnit = NewType("MicroLamportsPerComputeUnit", int)

PROBABILITY_PPM_DENOMINATOR: Final[int] = 1_000_000
PPM_SCALE: Final[int] = 1_000_000
