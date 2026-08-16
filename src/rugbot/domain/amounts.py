"""Integer unit types used by executable quote and replay logic."""

from typing import NewType

Lamports = NewType("Lamports", int)
TokenBaseUnits = NewType("TokenBaseUnits", int)
QuoteBaseUnits = NewType("QuoteBaseUnits", int)
Slot = NewType("Slot", int)
ComputeUnitLimit = NewType("ComputeUnitLimit", int)
MicroLamportsPerComputeUnit = NewType("MicroLamportsPerComputeUnit", int)
