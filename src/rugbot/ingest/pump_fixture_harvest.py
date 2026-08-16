"""Read-only Pump create_v2 fixture harvest smoke."""

import argparse
import asyncio
import base64
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol, cast

import aiohttp
import base58
from solders.transaction import VersionedTransaction

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_CREATE_V2_DISCRIMINATOR = bytes([214, 144, 76, 236, 95, 139, 49, 180])
PUMP_BUY_DISCRIMINATOR = bytes([102, 6, 61, 18, 1, 218, 235, 234])
PUMP_SELL_DISCRIMINATOR = bytes([51, 230, 133, 164, 1, 127, 131, 173])
PUMP_MIGRATE_DISCRIMINATOR = bytes([155, 234, 231, 146, 236, 158, 162, 30])
HARVEST_SCHEMA_VERSION = 1
HARVESTER_VERSION = "pump-create-v2-fixture-harvest-v1"
PUMP_INSTRUCTION_HARVESTER_VERSION = "pump-instruction-fixture-harvest-v1"
DEFAULT_MAX_SIGNATURES = 20
DEFAULT_MAX_TRANSACTIONS = 5
HTTP_TOO_MANY_REQUESTS = 429
HTTP_BAD_REQUEST = 400
ALLOWED_RPC_METHODS = frozenset(
    {
        "getHealth",
        "getVersion",
        "getSlot",
        "getSignaturesForAddress",
        "getTransaction",
        "getMultipleAccounts",
    }
)
SOLANA_RPC_ENDPOINT_ENV_PRIMARY = "SOLANA_NODE_RPC_ENDPOINT"
SOLANA_RPC_ENDPOINT_ENV_FALLBACK = "SOLANA_RPC_HTTP"
SOLANA_RPC_ENDPOINT_SKIP_MESSAGE = (
    "SOLANA_NODE_RPC_ENDPOINT or SOLANA_RPC_HTTP not configured"
)


class HarvestStatus(Enum):
    """Fixture harvest status."""

    OK = "ok"
    SKIP = "skip"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class PumpCreateV2Instruction:
    """Minimal decoded create_v2 instruction evidence."""

    instruction_index: int
    program_id: str
    account_pubkeys: tuple[str, ...]
    account_indices: tuple[int, ...]
    program_id_index: int | None
    data_base58: str


@dataclass(frozen=True, slots=True)
class PumpInstructionFixtureTarget:
    """Pump instruction fixture target."""

    instruction_name: str
    discriminator: bytes


@dataclass(frozen=True, slots=True)
class PumpInstructionFixtureEvidence:
    """Minimal decoded instruction evidence for a finalized Pump transaction."""

    instruction_name: str
    instruction_index: int
    program_id: str
    discriminator_hex: str
    account_pubkeys: tuple[str, ...]
    account_indices: tuple[int, ...]
    program_id_index: int | None
    data_base58: str


@dataclass(frozen=True, slots=True)
class PumpFixtureArtifact:
    """Immutable JSON artifact for one finalized Pump create_v2 transaction."""

    schema_version: int
    harvester_version: str
    signature: str
    as_of_slot: int
    finalized_slot_seen: int
    pump_program_id: str
    pump_idl_sha256: str
    create_v2: PumpCreateV2Instruction
    json_parsed_transaction_response: object
    base64_transaction_response: object


@dataclass(frozen=True, slots=True)
class PumpInstructionFixtureArtifact:
    """Immutable JSON artifact for one finalized Pump instruction transaction."""

    schema_version: int
    harvester_version: str
    signature: str
    as_of_slot: int
    finalized_slot_seen: int
    pump_program_id: str
    pump_idl_sha256: str
    instruction: PumpInstructionFixtureEvidence
    json_parsed_transaction_response: object
    base64_transaction_response: object


@dataclass(frozen=True, slots=True)
class HarvestResult:
    """Result of a bounded read-only fixture harvest."""

    status: HarvestStatus
    message: str
    artifact_path: Path | None = None
    signature: str | None = None
    as_of_slot: int | None = None


@dataclass(frozen=True, slots=True)
class PumpFixtureHarvestConfig:
    """Configuration for one bounded fixture harvest attempt."""

    output_dir: Path
    idl_path: Path
    max_signatures: int = DEFAULT_MAX_SIGNATURES
    max_transactions: int = DEFAULT_MAX_TRANSACTIONS
    request_delay_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class _ArtifactInputs:
    signature: str
    finalized_slot_seen: int
    idl_path: Path
    candidate: PumpCreateV2Instruction
    json_parsed_response: object
    base64_response: object


@dataclass(frozen=True, slots=True)
class _InstructionArtifactInputs:
    signature: str
    finalized_slot_seen: int
    idl_path: Path
    candidate: PumpInstructionFixtureEvidence
    json_parsed_response: object
    base64_response: object


PUMP_INSTRUCTION_FIXTURE_TARGETS = {
    "buy": PumpInstructionFixtureTarget(
        instruction_name="buy",
        discriminator=PUMP_BUY_DISCRIMINATOR,
    ),
    "sell": PumpInstructionFixtureTarget(
        instruction_name="sell",
        discriminator=PUMP_SELL_DISCRIMINATOR,
    ),
    "migrate": PumpInstructionFixtureTarget(
        instruction_name="migrate",
        discriminator=PUMP_MIGRATE_DISCRIMINATOR,
    ),
}
DEFAULT_OUTPUT_DIRS = {
    "create_v2": Path("fixtures/finalized_transactions/pump_create_v2"),
    "buy": Path("fixtures/finalized_transactions/pump_buy"),
    "sell": Path("fixtures/finalized_transactions/pump_sell"),
    "migrate": Path("fixtures/finalized_transactions/pump_migrate"),
}


class ReadOnlyRpcClient(Protocol):
    """Read-only JSON-RPC client contract."""

    async def post_rpc(
        self,
        method: str,
        params: Sequence[object] | None = None,
    ) -> object:
        """Call one allowlisted read-only JSON-RPC method."""


class RpcMethodNotAllowedError(ValueError):
    """Raised when a JSON-RPC method is outside the read-only allowlist."""

    def __init__(self) -> None:
        """Initialize the allowlist error."""

        super().__init__("json-rpc method is not allowlisted")


class RpcRateLimitedError(RuntimeError):
    """Raised when the public RPC endpoint rate limits the harvest."""

    def __init__(self) -> None:
        """Initialize the rate-limit error."""

        super().__init__("json-rpc endpoint rate limited the harvest")


class RpcHarvestError(RuntimeError):
    """Raised when a read-only harvest RPC call fails."""

    @classmethod
    def http_error(cls, *, status: int, method: str) -> "RpcHarvestError":
        """Build an HTTP-status harvest error."""

        return cls(f"HTTP {status} from {method}")

    @classmethod
    def non_object_json(cls, *, method: str) -> "RpcHarvestError":
        """Build a non-object JSON harvest error."""

        return cls(f"{method} returned non-object JSON")

    @classmethod
    def rpc_error(cls, *, method: str, error: object) -> "RpcHarvestError":
        """Build a JSON-RPC error harvest error."""

        return cls(f"RPC error from {method}: {error!r}")

    @classmethod
    def missing_result(cls, *, method: str) -> "RpcHarvestError":
        """Build a missing-result harvest error."""

        return cls(f"No result from {method}")


class ReadOnlySolanaRpcClient:
    """Small allowlisted Solana JSON-RPC client for fixture harvest smoke."""

    def __init__(self, endpoint: str, *, timeout_seconds: int = 20) -> None:
        """Initialize the read-only RPC client."""

        self._endpoint = endpoint
        self._timeout_seconds = timeout_seconds

    async def post_rpc(
        self,
        method: str,
        params: Sequence[object] | None = None,
    ) -> object:
        """Call one allowlisted JSON-RPC method."""

        ensure_rpc_method_allowed(method)
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        body: dict[str, object] = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            body["params"] = list(params)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self._endpoint, json=body) as response:
                if response.status == HTTP_TOO_MANY_REQUESTS:
                    raise RpcRateLimitedError
                if response.status >= HTTP_BAD_REQUEST:
                    raise RpcHarvestError.http_error(
                        status=response.status,
                        method=method,
                    )
                payload = cast("object", await response.json(content_type=None))

        if not isinstance(payload, dict):
            raise RpcHarvestError.non_object_json(method=method)
        if "error" in payload:
            raise RpcHarvestError.rpc_error(method=method, error=payload["error"])
        if "result" not in payload:
            raise RpcHarvestError.missing_result(method=method)
        return payload["result"]


def ensure_rpc_method_allowed(method: str) -> None:
    """Reject methods outside the read-only fixture-harvest allowlist."""

    if method not in ALLOWED_RPC_METHODS:
        raise RpcMethodNotAllowedError


async def harvest_one_pump_create_v2_fixture(
    *,
    rpc_client: ReadOnlyRpcClient,
    config: PumpFixtureHarvestConfig,
) -> HarvestResult:
    """Harvest at most one finalized Pump create_v2 raw transaction fixture."""

    if config.max_signatures <= 0 or config.max_transactions <= 0:
        return HarvestResult(
            status=HarvestStatus.SKIP,
            message="max_signatures and max_transactions must be positive",
        )

    try:
        finalized_slot = await _finalized_slot(rpc_client)
        signatures = await _candidate_signatures(
            rpc_client,
            max_signatures=config.max_signatures,
        )
        checked_transactions = 0
        for signature in signatures:
            if checked_transactions >= config.max_transactions:
                break
            checked_transactions += 1
            await _optional_delay(config.request_delay_seconds)

            json_parsed = await _get_transaction(
                rpc_client,
                signature,
                encoding="jsonParsed",
            )
            candidate = _find_create_v2_instruction(
                transaction_response=json_parsed,
            )
            if candidate is None:
                continue

            await _optional_delay(config.request_delay_seconds)
            base64_response = await _get_transaction(
                rpc_client,
                signature,
                encoding="base64",
            )
            artifact = _build_artifact(
                _ArtifactInputs(
                    signature=signature,
                    finalized_slot_seen=finalized_slot,
                    idl_path=config.idl_path,
                    candidate=candidate,
                    json_parsed_response=json_parsed,
                    base64_response=base64_response,
                )
            )
            artifact_path = _write_artifact(config.output_dir, artifact)
            return HarvestResult(
                status=HarvestStatus.OK,
                message="harvested finalized Pump create_v2 fixture",
                artifact_path=artifact_path,
                signature=signature,
                as_of_slot=artifact.as_of_slot,
            )
    except (aiohttp.ClientError, TimeoutError, RpcHarvestError) as error:
        return HarvestResult(
            status=HarvestStatus.FAIL,
            message=f"fixture harvest failed: {error}",
        )
    except RpcRateLimitedError:
        return HarvestResult(
            status=HarvestStatus.SKIP,
            message="fixture harvest skipped after public RPC rate limit",
        )

    return HarvestResult(
        status=HarvestStatus.SKIP,
        message="no finalized Pump create_v2 candidate found within request cap",
    )


async def harvest_one_pump_instruction_fixture(
    *,
    rpc_client: ReadOnlyRpcClient,
    config: PumpFixtureHarvestConfig,
    target: PumpInstructionFixtureTarget,
) -> HarvestResult:
    """Harvest at most one finalized Pump instruction raw transaction fixture."""

    if config.max_signatures <= 0 or config.max_transactions <= 0:
        return HarvestResult(
            status=HarvestStatus.SKIP,
            message="max_signatures and max_transactions must be positive",
        )

    try:
        finalized_slot = await _finalized_slot(rpc_client)
        signatures = await _candidate_signatures(
            rpc_client,
            max_signatures=config.max_signatures,
        )
        checked_transactions = 0
        for signature in signatures:
            if checked_transactions >= config.max_transactions:
                break
            checked_transactions += 1
            await _optional_delay(config.request_delay_seconds)

            json_parsed = await _get_transaction(
                rpc_client,
                signature,
                encoding="jsonParsed",
            )
            candidate = _find_instruction_fixture_evidence(
                transaction_response=json_parsed,
                target=target,
            )
            if candidate is None:
                continue

            await _optional_delay(config.request_delay_seconds)
            base64_response = await _get_transaction(
                rpc_client,
                signature,
                encoding="base64",
            )
            artifact = _build_instruction_artifact(
                _InstructionArtifactInputs(
                    signature=signature,
                    finalized_slot_seen=finalized_slot,
                    idl_path=config.idl_path,
                    candidate=candidate,
                    json_parsed_response=json_parsed,
                    base64_response=base64_response,
                )
            )
            artifact_path = _write_artifact(config.output_dir, artifact)
            return HarvestResult(
                status=HarvestStatus.OK,
                message=(f"harvested finalized Pump {target.instruction_name} fixture"),
                artifact_path=artifact_path,
                signature=signature,
                as_of_slot=artifact.as_of_slot,
            )
    except (aiohttp.ClientError, TimeoutError, RpcHarvestError) as error:
        return HarvestResult(
            status=HarvestStatus.FAIL,
            message=f"fixture harvest failed: {error}",
        )
    except RpcRateLimitedError:
        return HarvestResult(
            status=HarvestStatus.SKIP,
            message="fixture harvest skipped after public RPC rate limit",
        )

    return HarvestResult(
        status=HarvestStatus.SKIP,
        message=(
            "no finalized Pump "
            f"{target.instruction_name} candidate found within request cap"
        ),
    )


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the fixture-harvest CLI parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rpc",
        default=rpc_endpoint_from_env(),
        help=(
            "Solana HTTP RPC endpoint. Defaults to SOLANA_NODE_RPC_ENDPOINT, "
            "then SOLANA_RPC_HTTP."
        ),
    )
    parser.add_argument(
        "--target",
        choices=tuple(DEFAULT_OUTPUT_DIRS),
        default="create_v2",
        help="Pump instruction target to harvest.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for harvested fixture artifacts. Defaults by target.",
    )
    parser.add_argument(
        "--idl-path",
        type=Path,
        default=Path("idl/pump_fun_idl.json"),
        help="Pinned Pump IDL path used to compute artifact hash.",
    )
    parser.add_argument(
        "--max-signatures",
        type=int,
        default=DEFAULT_MAX_SIGNATURES,
        help="Maximum signatures to request from Pump program history.",
    )
    parser.add_argument(
        "--max-transactions",
        type=int,
        default=DEFAULT_MAX_TRANSACTIONS,
        help="Maximum transactions to fetch and inspect.",
    )
    parser.add_argument(
        "--request-delay-seconds",
        type=float,
        default=1.0,
        help="Delay between transaction requests to reduce public RPC pressure.",
    )
    return parser


async def async_main(args: argparse.Namespace) -> int:
    """Run the fixture-harvest CLI."""

    rpc_endpoint = _non_blank_str(args.rpc)
    if rpc_endpoint is None:
        print(f"pump_fixture_harvest: skip - {SOLANA_RPC_ENDPOINT_SKIP_MESSAGE}")
        return 0

    result = await _harvest_target(
        target_name=args.target,
        rpc_client=ReadOnlySolanaRpcClient(rpc_endpoint),
        config=_harvest_config_from_args(args),
    )
    path_message = f", path={result.artifact_path}" if result.artifact_path else ""
    print(
        f"pump_fixture_harvest: {result.status.value} - {result.message}{path_message}"
    )
    return 1 if result.status == HarvestStatus.FAIL else 0


async def _harvest_target(
    *,
    target_name: str,
    rpc_client: ReadOnlyRpcClient,
    config: PumpFixtureHarvestConfig,
) -> HarvestResult:
    if target_name == "create_v2":
        return await harvest_one_pump_create_v2_fixture(
            rpc_client=rpc_client,
            config=config,
        )
    return await harvest_one_pump_instruction_fixture(
        rpc_client=rpc_client,
        config=config,
        target=PUMP_INSTRUCTION_FIXTURE_TARGETS[target_name],
    )


def _harvest_config_from_args(args: argparse.Namespace) -> PumpFixtureHarvestConfig:
    return PumpFixtureHarvestConfig(
        output_dir=_output_dir_for_target(args.target, args.output_dir),
        idl_path=args.idl_path,
        max_signatures=args.max_signatures,
        max_transactions=args.max_transactions,
        request_delay_seconds=args.request_delay_seconds,
    )


def _output_dir_for_target(target_name: str, output_dir: Path | None) -> Path:
    if output_dir is not None:
        return output_dir
    return DEFAULT_OUTPUT_DIRS[target_name]


def rpc_endpoint_from_env(env: Mapping[str, str] | None = None) -> str | None:
    """Return the configured Solana HTTP RPC endpoint, if any."""

    environ = os.environ if env is None else env
    primary = _non_blank_str(environ.get(SOLANA_RPC_ENDPOINT_ENV_PRIMARY))
    if primary is not None:
        return primary
    return _non_blank_str(environ.get(SOLANA_RPC_ENDPOINT_ENV_FALLBACK))


def main() -> None:
    """CLI entry point."""

    parser = build_arg_parser()
    args = parser.parse_args()
    raise SystemExit(asyncio.run(async_main(args)))


async def _finalized_slot(rpc_client: ReadOnlyRpcClient) -> int:
    await rpc_client.post_rpc("getHealth")
    await rpc_client.post_rpc("getVersion")
    slot = await rpc_client.post_rpc(
        "getSlot",
        [{"commitment": "finalized"}],
    )
    if not isinstance(slot, int):
        raise RpcHarvestError.missing_result(method="getSlot")
    return slot


async def _candidate_signatures(
    rpc_client: ReadOnlyRpcClient,
    *,
    max_signatures: int,
) -> list[str]:
    result = await rpc_client.post_rpc(
        "getSignaturesForAddress",
        [
            PUMP_PROGRAM_ID,
            {
                "limit": max_signatures,
                "commitment": "finalized",
            },
        ],
    )
    if not isinstance(result, list):
        raise RpcHarvestError.missing_result(method="getSignaturesForAddress")

    signatures: list[str] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        if item.get("err") is not None:
            continue
        signature = item.get("signature")
        if isinstance(signature, str):
            signatures.append(signature)
    return signatures


async def _get_transaction(
    rpc_client: ReadOnlyRpcClient,
    signature: str,
    *,
    encoding: str,
) -> object:
    return await rpc_client.post_rpc(
        "getTransaction",
        [
            signature,
            {
                "encoding": encoding,
                "commitment": "finalized",
                "maxSupportedTransactionVersion": 0,
            },
        ],
    )


def _find_create_v2_instruction(
    *,
    transaction_response: object,
) -> PumpCreateV2Instruction | None:
    instructions = _json_instruction_sequence(transaction_response)
    if instructions is None:
        return None

    for instruction_index, instruction in enumerate(instructions):
        candidate = _candidate_from_json_instruction(
            instruction=instruction,
            instruction_index=instruction_index,
        )
        if candidate is not None:
            return candidate
    return None


def _find_instruction_fixture_evidence(
    *,
    transaction_response: object,
    target: PumpInstructionFixtureTarget,
) -> PumpInstructionFixtureEvidence | None:
    instructions = _json_instruction_sequence(transaction_response)
    if instructions is None:
        return None

    for instruction_index, instruction in enumerate(instructions):
        candidate = _candidate_from_json_instruction_for_target(
            instruction=instruction,
            instruction_index=instruction_index,
            target=target,
        )
        if candidate is not None:
            return candidate
    return None


def _candidate_from_json_instruction(
    *,
    instruction: object,
    instruction_index: int,
) -> PumpCreateV2Instruction | None:
    instruction_data = _json_pump_instruction_data(instruction)
    if instruction_data is None:
        return None
    data_base58, accounts = instruction_data
    try:
        data = base58.b58decode(data_base58)
    except ValueError:
        return None
    if not data.startswith(PUMP_CREATE_V2_DISCRIMINATOR):
        return None

    return PumpCreateV2Instruction(
        instruction_index=instruction_index,
        program_id=PUMP_PROGRAM_ID,
        account_pubkeys=accounts,
        account_indices=(),
        program_id_index=None,
        data_base58=data_base58,
    )


def _candidate_from_json_instruction_for_target(
    *,
    instruction: object,
    instruction_index: int,
    target: PumpInstructionFixtureTarget,
) -> PumpInstructionFixtureEvidence | None:
    instruction_data = _json_pump_instruction_data(instruction)
    if instruction_data is None:
        return None
    data_base58, accounts = instruction_data
    try:
        data = base58.b58decode(data_base58)
    except ValueError:
        return None
    if not data.startswith(target.discriminator):
        return None

    return PumpInstructionFixtureEvidence(
        instruction_name=target.instruction_name,
        instruction_index=instruction_index,
        program_id=PUMP_PROGRAM_ID,
        discriminator_hex=target.discriminator.hex(),
        account_pubkeys=accounts,
        account_indices=(),
        program_id_index=None,
        data_base58=data_base58,
    )


def _json_instruction_sequence(transaction_response: object) -> list[object] | None:
    if not isinstance(transaction_response, dict):
        return None
    transaction = cast("Mapping[str, object]", transaction_response)
    slot = transaction.get("slot")
    if not isinstance(slot, int):
        return None
    meta = transaction.get("meta")
    if not isinstance(meta, dict) or meta.get("err") is not None:
        return None
    message = _transaction_message(transaction)
    if message is None:
        return None
    instructions = message.get("instructions")
    if not isinstance(instructions, list):
        return None
    return cast("list[object]", instructions)


def _json_pump_instruction_data(
    instruction: object,
) -> tuple[str, tuple[str, ...]] | None:
    if not isinstance(instruction, dict):
        return None
    if instruction.get("programId") != PUMP_PROGRAM_ID:
        return None
    data_base58 = instruction.get("data")
    accounts = instruction.get("accounts")
    if not isinstance(data_base58, str):
        return None
    if not isinstance(accounts, list) or not all(
        isinstance(account, str) for account in accounts
    ):
        return None
    return data_base58, tuple(cast("list[str]", accounts))


def _build_artifact(inputs: _ArtifactInputs) -> PumpFixtureArtifact:
    transaction = _transaction_result(inputs.json_parsed_response)
    slot = transaction.get("slot")
    if not isinstance(slot, int):
        raise RpcHarvestError.missing_result(method="getTransaction")

    enriched_candidate = _candidate_with_compiled_indices(
        candidate=inputs.candidate,
        base64_response=inputs.base64_response,
    )
    return PumpFixtureArtifact(
        schema_version=HARVEST_SCHEMA_VERSION,
        harvester_version=HARVESTER_VERSION,
        signature=inputs.signature,
        as_of_slot=slot,
        finalized_slot_seen=inputs.finalized_slot_seen,
        pump_program_id=PUMP_PROGRAM_ID,
        pump_idl_sha256=_file_sha256(inputs.idl_path),
        create_v2=enriched_candidate,
        json_parsed_transaction_response=inputs.json_parsed_response,
        base64_transaction_response=inputs.base64_response,
    )


def _build_instruction_artifact(
    inputs: _InstructionArtifactInputs,
) -> PumpInstructionFixtureArtifact:
    transaction = _transaction_result(inputs.json_parsed_response)
    slot = transaction.get("slot")
    if not isinstance(slot, int):
        raise RpcHarvestError.missing_result(method="getTransaction")

    enriched_candidate = _instruction_candidate_with_compiled_indices(
        candidate=inputs.candidate,
        base64_response=inputs.base64_response,
    )
    return PumpInstructionFixtureArtifact(
        schema_version=HARVEST_SCHEMA_VERSION,
        harvester_version=PUMP_INSTRUCTION_HARVESTER_VERSION,
        signature=inputs.signature,
        as_of_slot=slot,
        finalized_slot_seen=inputs.finalized_slot_seen,
        pump_program_id=PUMP_PROGRAM_ID,
        pump_idl_sha256=_file_sha256(inputs.idl_path),
        instruction=enriched_candidate,
        json_parsed_transaction_response=inputs.json_parsed_response,
        base64_transaction_response=inputs.base64_response,
    )


def _candidate_with_compiled_indices(
    *,
    candidate: PumpCreateV2Instruction,
    base64_response: object,
) -> PumpCreateV2Instruction:
    transaction_bytes = _raw_transaction_bytes(base64_response)
    if transaction_bytes is None:
        return candidate

    try:
        transaction = VersionedTransaction.from_bytes(transaction_bytes)
    except ValueError:
        return candidate
    instruction = transaction.message.instructions[candidate.instruction_index]
    return PumpCreateV2Instruction(
        instruction_index=candidate.instruction_index,
        program_id=candidate.program_id,
        account_pubkeys=candidate.account_pubkeys,
        account_indices=tuple(int(index) for index in instruction.accounts),
        program_id_index=int(instruction.program_id_index),
        data_base58=candidate.data_base58,
    )


def _instruction_candidate_with_compiled_indices(
    *,
    candidate: PumpInstructionFixtureEvidence,
    base64_response: object,
) -> PumpInstructionFixtureEvidence:
    transaction_bytes = _raw_transaction_bytes(base64_response)
    if transaction_bytes is None:
        return candidate

    try:
        transaction = VersionedTransaction.from_bytes(transaction_bytes)
    except ValueError:
        return candidate
    instruction = transaction.message.instructions[candidate.instruction_index]
    return PumpInstructionFixtureEvidence(
        instruction_name=candidate.instruction_name,
        instruction_index=candidate.instruction_index,
        program_id=candidate.program_id,
        discriminator_hex=candidate.discriminator_hex,
        account_pubkeys=candidate.account_pubkeys,
        account_indices=tuple(int(index) for index in instruction.accounts),
        program_id_index=int(instruction.program_id_index),
        data_base58=candidate.data_base58,
    )


def _raw_transaction_bytes(base64_response: object) -> bytes | None:
    transaction = _transaction_result(base64_response)
    encoded_transaction = transaction.get("transaction")
    if not isinstance(encoded_transaction, list) or not encoded_transaction:
        return None
    first_item = encoded_transaction[0]
    if not isinstance(first_item, str):
        return None
    try:
        return base64.b64decode(first_item)
    except ValueError:
        return None


def _transaction_result(transaction_response: object) -> Mapping[str, object]:
    if not isinstance(transaction_response, dict):
        raise RpcHarvestError.non_object_json(method="getTransaction")
    return cast("Mapping[str, object]", transaction_response)


def _transaction_message(
    transaction: Mapping[str, object],
) -> Mapping[str, object] | None:
    transaction_payload = transaction.get("transaction")
    if not isinstance(transaction_payload, dict):
        return None
    message = transaction_payload.get("message")
    if not isinstance(message, dict):
        return None
    return cast("Mapping[str, object]", message)


def _write_artifact(output_dir: Path, artifact: PumpFixtureArtifact) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = output_dir / f"{artifact.signature}.json"
    if artifact_path.exists():
        return artifact_path

    payload = asdict(artifact)
    with NamedTemporaryFile(
        "w",
        delete=False,
        dir=output_dir,
        encoding="utf-8",
    ) as temp_file:
        json.dump(payload, temp_file, indent=2, sort_keys=True)
        temp_file.write("\n")
        temp_path = Path(temp_file.name)

    temp_path.replace(artifact_path)
    return artifact_path


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _optional_delay(delay_seconds: float) -> None:
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)


def _non_blank_str(value: object) -> str | None:
    if type(value) is not str:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped


if __name__ == "__main__":
    main()
