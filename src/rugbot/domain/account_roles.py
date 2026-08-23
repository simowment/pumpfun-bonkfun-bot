"""Account-role and entity membership domain objects."""

# ruff: noqa: TC001

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rugbot.domain.amounts import Slot


class AddressRole(StrEnum):
    """Point-in-time role assigned to an address within an entity graph."""

    CREATOR = "creator"
    CREATION_SUBMITTER = "creation_submitter"
    FEE_PAYER = "fee_payer"
    FUNDER = "funder"
    FIRST_BUYER = "first_buyer"
    FAKE_PUMP_BUYER = "fake_pump_buyer"
    INVENTORY_HOLDER = "inventory_holder"
    DUMPER = "dumper"
    PROFIT_COLLECTOR = "profit_collector"
    RELAY_ADDRESS = "relay_address"
    INTERMEDIARY = "intermediary"
    BUYER = "buyer"
    SELLER = "seller"
    DEPLOYER = "deployer"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AccountRoleProof:
    """Caller-supplied proof that an IDL account role resolves to a pubkey."""

    name: str
    pubkey: str


@dataclass(frozen=True, slots=True)
class AddressRoleAssignment:
    """Assignment of an AddressRole to an address with a probability."""

    address: str
    role: AddressRole
    probability_ppm: int
    as_of_slot: Slot


@dataclass(frozen=True, slots=True)
class AddressRoleSnapshot:
    """Snapshot of address role assignments as of a given slot."""

    as_of_slot: Slot
    assignments: tuple[AddressRoleAssignment, ...]


@dataclass(frozen=True, slots=True)
class EntityMembership:
    """Membership of an address in an entity with a membership probability."""

    entity_id: str
    address: str
    role: AddressRole
    membership_probability_ppm: int
    as_of_slot: Slot


@dataclass(frozen=True, slots=True)
class ProbabilisticEntity:
    """A probabilistic cluster entity aggregating address memberships."""

    entity_id: str
    as_of_slot: Slot
    memberships: tuple[EntityMembership, ...]


__all__ = [
    "AccountRoleProof",
    "AddressRole",
    "AddressRoleAssignment",
    "AddressRoleSnapshot",
    "EntityMembership",
    "ProbabilisticEntity",
]
