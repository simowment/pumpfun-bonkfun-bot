"""Account-role proof domain objects."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AccountRoleProof:
    """Caller-supplied proof that an IDL account role resolves to a pubkey."""

    name: str
    pubkey: str
