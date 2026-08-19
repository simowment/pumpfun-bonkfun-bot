"""Normalized Solana protocol package."""

from rugbot.protocol.solana.models import SolTransfer
from rugbot.protocol.solana.transfers import parse_sol_transfers

__all__ = ["SolTransfer", "parse_sol_transfers"]
