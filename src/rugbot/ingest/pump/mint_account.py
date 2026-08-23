"""Strict SPL-2022 mint metadata decoder for finalized account evidence."""

from __future__ import annotations

import base58

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.metadata_resolver import (
    PumpFinalizedMintMetadataEvidence,
)
from rugbot.domain.observations import RawChainObservation
from rugbot.ingest.pump.create_decoder import SPL_2022_PROGRAM_ID

MINT_LAYOUT_ARTIFACT_VERSION = "spl-token-2022-mint-metadata-layout-v1"
MINT_BASE_LAYOUT_SIZE = 82
MINT_ACCOUNT_TYPE_OFFSET = 165
MINT_TLV_OFFSET = 166
MINT_ACCOUNT_TYPE_MINT = 1
MINT_TLV_HEADER_SIZE = 4
METADATA_POINTER_EXTENSION = 18
TOKEN_METADATA_EXTENSION = 19
METADATA_POINTER_LENGTH = 64
MAX_SUPPORTED_DECIMALS = 18

MintMetadataResult = PumpFinalizedMintMetadataEvidence | AbstainResult


def decode_spl_token_2022_mint_metadata(
    observation: RawChainObservation,
    *,
    mint_pubkey: str,
) -> MintMetadataResult:
    """Decode decimals from one finalized, pinned SPL-2022 mint account.

    Pump mints currently use only metadata extensions.  Transfer-affecting or
    unknown extensions remain unsupported so a paper quote cannot silently
    diverge from executable on-chain semantics.
    """

    validation = _validate_observation(observation, mint_pubkey)
    if validation is not None:
        return validation
    data = observation.raw_account_data
    if len(data) < MINT_BASE_LAYOUT_SIZE:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "SPL-2022 mint account has an unsupported extension layout",
            observation.slot,
        )
    if len(data) > MINT_BASE_LAYOUT_SIZE:
        extension_error = _validate_metadata_extensions(data)
        if extension_error is not None:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                extension_error,
                observation.slot,
            )
    initialized = data[45]
    if initialized != 1:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "SPL-2022 mint account is not initialized",
            observation.slot,
        )
    decimals = data[44]
    if decimals > MAX_SUPPORTED_DECIMALS:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "SPL-2022 mint decimals are unsupported",
            observation.slot,
        )
    owner = base58.b58encode(observation.account_owner_program_id).decode("ascii")
    return PumpFinalizedMintMetadataEvidence(
        as_of_slot=observation.slot,
        mint_pubkey=mint_pubkey,
        owner_program_id=owner,
        decimals=decimals,
        source_artifact=MINT_LAYOUT_ARTIFACT_VERSION,
        commitment="finalized",
    )


def _validate_observation(  # noqa: PLR0911
    observation: object,
    mint_pubkey: str,
) -> AbstainResult | None:
    if type(observation) is not RawChainObservation:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "finalized mint account observation is required",
            -1,
        )
    if (
        observation.commitment != "finalized"
        or observation.canonical_status != "canonical"
        or observation.source_update_kind != "account"
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "mint metadata requires finalized canonical account evidence",
            observation.slot,
        )
    if (
        type(observation.slot) is not int
        or observation.slot < 0
        or type(observation.account_pubkey) is not bytes
        or type(observation.account_owner_program_id) is not bytes
        or type(observation.raw_account_data) is not bytes
        or not isinstance(mint_pubkey, str)
        or not mint_pubkey
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "mint account identity and raw bytes are required",
            observation.slot if type(observation.slot) is int else -1,
        )
    try:
        expected_mint = bytes(base58.b58decode(mint_pubkey))
    except ValueError:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "mint metadata pubkey is not valid base58",
            observation.slot,
        )
    expected_owner = bytes(base58.b58decode(SPL_2022_PROGRAM_ID))
    if observation.account_pubkey != expected_mint:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "mint account observation does not match the requested mint",
            observation.slot,
        )
    if observation.account_owner_program_id != expected_owner:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "mint account is not owned by pinned SPL-2022",
            observation.slot,
        )
    return None


def _validate_metadata_extensions(data: bytes) -> str | None:  # noqa: C901, PLR0911
    """Validate the pinned non-transfer-affecting Token-2022 extensions."""

    if len(data) <= MINT_ACCOUNT_TYPE_OFFSET:
        return "SPL-2022 mint account has a truncated account type"
    if data[MINT_ACCOUNT_TYPE_OFFSET] != MINT_ACCOUNT_TYPE_MINT:
        return "SPL-2022 mint account has an unsupported account type"

    offset = MINT_TLV_OFFSET
    seen: set[int] = set()
    while offset < len(data):
        if not any(data[offset:]):
            return None
        if len(data) - offset < MINT_TLV_HEADER_SIZE:
            return "SPL-2022 mint extension TLV header is truncated"
        extension_type = int.from_bytes(data[offset : offset + 2], "little")
        extension_length = int.from_bytes(data[offset + 2 : offset + 4], "little")
        end = offset + 4 + extension_length
        if extension_length == 0 or end > len(data):
            return "SPL-2022 mint extension TLV is malformed"
        if extension_type in seen:
            return "SPL-2022 mint extension is duplicated"
        seen.add(extension_type)
        if extension_type == METADATA_POINTER_EXTENSION:
            if extension_length != METADATA_POINTER_LENGTH:
                return "SPL-2022 metadata pointer extension has an invalid length"
        elif extension_type != TOKEN_METADATA_EXTENSION:
            return "SPL-2022 mint has an unsupported transfer or unknown extension"
        offset = end
    return None


def _abstain(reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "MINT_BASE_LAYOUT_SIZE",
    "MINT_LAYOUT_ARTIFACT_VERSION",
    "decode_spl_token_2022_mint_metadata",
]
