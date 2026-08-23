"""Automated funder discovery: token/wallet seed to dev, funders, and cluster stats.

The on-chain creator and the GMGN-attributed dev entity are tracked as distinct
evidence branches because they are frequently different wallets with different
funding sources. Each branch carries its own funder and typed funding evidence.
"""

# Parsing hostile RPC and GMGN JSON is intentionally branch-heavy and fail-closed.
# ruff: noqa: C901, PLR0912, PLR0913, PLR0915, S310, BLE001, TRY003

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from rugbot.domain.decisions import AbstainResult
from rugbot.intelligence.gmgn_creator_history import (
    GmgnCreatorToken,
    fetch_gmgn_creator_history,
    fetch_gmgn_dev,
)
from rugbot.intelligence.token_resolver import resolve_token_or_wallet
from rugbot.tracker.models import (
    FunderRecord,
    TransferRecord,
    WalletRecord,
    WalletStatus,
)
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from rugbot.storage.tracker import SQLiteTrackerRepository

logger = get_logger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000

MAX_FUNDING_PAGES = 200
MAX_PAGE_SIGNATURES = 1000
MAX_FAST_PATH_TRANSACTIONS = 100
MAX_FUNDING_CANDIDATES = 100
RPC_MAX_RETRIES = 5
RPC_RETRY_BASE_DELAY_SECONDS = 0.5
RPC_SCAN_DELAY_SECONDS = 0.5
RPC_TIMEOUT_SECONDS = 15
RPC_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
WINRATE_100K_MC = 100_000.0
WINRATE_250K_MC = 250_000.0


@dataclass(frozen=True, slots=True)
class _IncomingTransferEvidence:
    """Typed first-successful-incoming-transfer evidence for one subject wallet."""

    source: str
    amount_lamports: int
    signature: str
    slot: int
    instruction_index: int
    timestamp: int


@dataclass(slots=True)
class FunderDiscoveryReport:
    """Synthesized funder-discovery cluster report for one seed.

    Args:
        seed: The original token mint or wallet address input.
        resolved_creator: On-chain creator wallet (account_keys[0] of the
            creation transaction) or the seed itself when it is a wallet.
        gmgn_dev: GMGN-attributed entity wallet (``dev.creator_address``),
            which may differ from the on-chain creator.
        creator_funder: Funding source of the on-chain creator's first
            successful incoming SOL transfer, or None when inconclusive.
        creator_funding_evidence: Typed provenance of the creator funding
            transfer, or None.
        dev_funder: Funding source of the GMGN dev's first successful incoming
            SOL transfer, or None when inconclusive.
        dev_funding_evidence: Typed provenance of the dev funding transfer, or
            None.
        descendant_wallet_count: Number of tracked descendant wallets of the
            dev funder in the repository.
        launches: List of per-token dicts from the GMGN created-tokens array.
        launch_count: Total created count (inner plus open) reported by GMGN.
        open_count: Number of currently open tokens reported by GMGN.
        open_ratio: Open ratio reported by GMGN.
        ath_token: GMGN all-time-high token summary dict or None.
        winrate_100k: Fraction of launches with token_ath_mc >= 100000.
        winrate_250k: Fraction of launches with token_ath_mc >= 250000.
        avg_bundler_rate: Mean bundler rate across the returned tokens.
        total_fees_sol: Sum of per-token total fees in SOL.
        warnings: Non-fatal diagnostics collected during discovery.
    """

    seed: str
    resolved_creator: str | None
    gmgn_dev: str | None
    creator_funder: str | None
    creator_funding_evidence: TransferRecord | None
    dev_funder: str | None
    dev_funding_evidence: TransferRecord | None
    descendant_wallet_count: int
    launches: list[dict[str, object]]
    launch_count: int
    open_count: int
    open_ratio: str | None
    ath_token: dict[str, object] | None
    winrate_100k: float
    winrate_250k: float
    avg_bundler_rate: float
    total_fees_sol: float
    warnings: list[str] = field(default_factory=list)


async def discover_funder(
    seed: str,
    *,
    repository: SQLiteTrackerRepository,
    endpoint: str | None = None,
    gmgn_api_key: str | None = None,
) -> FunderDiscoveryReport:
    """Trace a token mint or wallet seed to its creator, dev, funders, and stats.

    The pipeline resolves the on-chain creator, attributes the GMGN dev entity,
    traces the funding source of both the on-chain creator and the GMGN dev
    (when distinct) over RPC, enumerates the dev's launch history via GMGN,
    persists the discovered records into the tracker repository, and synthesizes
    a cluster report.

    Args:
        seed: A token mint address or a wallet address.
        repository: The tracker repository used to persist discovered records.
        endpoint: Optional Solana RPC HTTP endpoint; resolved from the
            environment when omitted.
        gmgn_api_key: Optional GMGN API key; falls back to the environment or
            the public testing key when omitted.

    Returns:
        A synthesized :class:`FunderDiscoveryReport`.
    """
    endpoint = endpoint or _resolve_endpoint()
    if not endpoint:
        raise ValueError("SOLANA_RPC_HTTP or SOLANA_NODE_RPC_ENDPOINT is required")
    if gmgn_api_key:
        os.environ["GMGN_API_KEY"] = gmgn_api_key
    warnings: list[str] = []

    resolved = resolve_token_or_wallet(seed, rpc_url=endpoint)
    onchain_creator = resolved.target_wallet
    is_token = resolved.is_token

    gmgn_dev = onchain_creator
    if is_token:
        dev = await fetch_gmgn_dev(seed)
        if dev is None:
            warnings.append(
                "GMGN dev attribution unavailable; using on-chain creator as dev"
            )
        else:
            gmgn_dev = dev

    creator_funder, creator_evidence, creator_warning = await _trace_funding(
        onchain_creator, endpoint
    )
    if creator_warning:
        warnings.append(creator_warning)

    if gmgn_dev != onchain_creator:
        dev_funder, dev_evidence, dev_warning = await _trace_funding(gmgn_dev, endpoint)
        if dev_warning:
            warnings.append(dev_warning)
    else:
        # Converged branch: trace once and reuse identical typed evidence.
        dev_funder = creator_funder
        dev_evidence = creator_evidence

    now_iso = datetime.now(UTC).isoformat()
    _persist_branch(
        repository,
        subject=onchain_creator,
        funder=creator_funder,
        evidence=creator_evidence,
        now_iso=now_iso,
        label_prefix="Creator funder",
    )
    if gmgn_dev != onchain_creator:
        _persist_branch(
            repository,
            subject=gmgn_dev,
            funder=dev_funder,
            evidence=dev_evidence,
            now_iso=now_iso,
            label_prefix="Dev funder",
        )

    history = await fetch_gmgn_creator_history(gmgn_dev)
    if isinstance(history, AbstainResult):
        warnings.append(f"creator history unavailable: {history.message}")
        history = None

    launches: list[dict[str, object]] = []
    launch_count = 0
    open_count = 0
    open_ratio: str | None = None
    ath_token: dict[str, object] | None = None
    winrate_100k = 0.0
    winrate_250k = 0.0
    avg_bundler_rate = 0.0
    total_fees_sol = 0.0

    if history is not None:
        launch_count = history.total_created_count
        open_count = history.open_count
        open_ratio = history.open_ratio
        if history.ath_token:
            ath_token = {
                "token": history.ath_token,
                "symbol": history.ath_symbol,
                "name": history.ath_name,
                "market_cap": history.ath_market_cap,
            }
        launches = [_token_to_dict(token) for token in history.tokens]
        winrate_100k, winrate_250k, avg_bundler_rate, total_fees_sol = _synthesize(
            history.tokens
        )
        if history.tokens:
            warnings.append(
                "GMGN created-tokens lacks on-chain signature/slot evidence; "
                "launch records were not persisted"
            )

    descendant_wallet_count = (
        len(repository.get_descendants(dev_funder)) if dev_funder else 0
    )

    return FunderDiscoveryReport(
        seed=seed,
        resolved_creator=onchain_creator,
        gmgn_dev=gmgn_dev,
        creator_funder=creator_funder,
        creator_funding_evidence=creator_evidence,
        dev_funder=dev_funder,
        dev_funding_evidence=dev_evidence,
        descendant_wallet_count=descendant_wallet_count,
        launches=launches,
        launch_count=launch_count,
        open_count=open_count,
        open_ratio=open_ratio,
        ath_token=ath_token,
        winrate_100k=winrate_100k,
        winrate_250k=winrate_250k,
        avg_bundler_rate=avg_bundler_rate,
        total_fees_sol=total_fees_sol,
        warnings=warnings,
    )


def _persist_branch(
    repository: SQLiteTrackerRepository,
    *,
    subject: str,
    funder: str | None,
    evidence: TransferRecord | None,
    now_iso: str,
    label_prefix: str,
) -> None:
    """Persist one discovered funder, subject wallet, and funding transfer."""
    if funder:
        repository.save_funder(
            FunderRecord(
                id=None,
                address=funder,
                label=f"{label_prefix} of {subject[:6]}...",
                enabled=True,
                created_at=now_iso,
                last_seen_at=now_iso,
            )
        )
    repository.save_wallet(
        WalletRecord(
            address=subject,
            root_funder=funder or subject,
            parent_wallet=funder,
            depth=1,
            status=WalletStatus.CREATOR,
            discovered_at=now_iso,
            expires_at=None,
            last_active_at=now_iso,
        )
    )
    if evidence:
        repository.save_transfer(evidence)


def _resolve_endpoint() -> str | None:
    """Resolve the Solana RPC HTTP endpoint from the environment."""
    return os.environ.get("SOLANA_RPC_HTTP") or os.environ.get(
        "SOLANA_NODE_RPC_ENDPOINT"
    )


def _rpc_call(endpoint: str, method: str, params: list[object]) -> object:
    """Perform a raw JSON-RPC HTTP call with bounded retries.

    Retries are limited to ``RPC_MAX_RETRIES`` attempts and only for transient
    status codes (429 and 5xx). When the server supplies a numeric
    ``Retry-After`` header it is honored; otherwise a fixed base delay is used.
    Non-transient failures and exhausted retries propagate the last error rather
    than silently returning a fabricated result.
    """
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    last_error: Exception | None = None
    for attempt in range(RPC_MAX_RETRIES):
        try:
            req = urllib.request.Request(
                endpoint,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0",
                },
            )
            with urllib.request.urlopen(req, timeout=RPC_TIMEOUT_SECONDS) as resp:
                data: dict[str, Any] = json.loads(resp.read().decode())
                return data.get("result")
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in RPC_RETRYABLE_STATUS_CODES:
                raise
            if attempt < RPC_MAX_RETRIES - 1:
                retry_after = _retry_after_seconds(exc) or 0.0
                backoff = RPC_RETRY_BASE_DELAY_SECONDS * (2**attempt)
                time.sleep(max(retry_after, backoff))
        except Exception as exc:
            last_error = exc
            if attempt < RPC_MAX_RETRIES - 1:
                time.sleep(RPC_RETRY_BASE_DELAY_SECONDS)
    if last_error:
        raise last_error
    return None


def _retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
    """Return the numeric ``Retry-After`` delay, or None when absent/unparseable."""
    value = exc.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


async def _trace_funding(
    subject_wallet: str, endpoint: str
) -> tuple[str | None, TransferRecord | None, str | None]:
    """Trace a subject wallet's first successful incoming SOL transfer.

    Uses the provider-capability fast path (``getTransactionsForAddress``)
    oldest-first when available, falling back to bounded standard
    ``getSignaturesForAddress`` paging when the method is unsupported or
    malformed. Returns (funder, evidence, warning).
    """
    fast = _trace_funding_via_transactions(subject_wallet, endpoint)
    if fast is not None:
        return fast
    return _trace_funding_via_signatures(subject_wallet, endpoint)


def _trace_funding_via_transactions(
    subject_wallet: str, endpoint: str
) -> tuple[str | None, TransferRecord | None, str | None] | None:
    """Try the ``getTransactionsForAddress`` fast path; None when unsupported.

    Pages oldest-first with full jsonParsed transactions and scans each ordered
    entry as it is fetched, stopping at the first successful incoming native SOL
    transfer to the subject. Paging is bounded by ``MAX_FUNDING_PAGES``.
    """
    token: str | None = None
    pages = 0
    scanned = 0
    while pages < MAX_FUNDING_PAGES:
        options: dict[str, object] = {
            "transactionDetails": "full",
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 0,
            "sortOrder": "asc",
            "limit": MAX_FAST_PATH_TRANSACTIONS,
            "commitment": "finalized",
            "filters": {"status": "succeeded"},
        }
        if token:
            options["paginationToken"] = token
        try:
            result = _rpc_call(
                endpoint, "getTransactionsForAddress", [subject_wallet, options]
            )
        except Exception:
            return None
        if result is None:
            return None
        if not isinstance(result, dict):
            return None
        data = result.get("data")
        if not isinstance(data, list):
            return None
        token = result.get("paginationToken")
        pages += 1
        for tx in data:
            if not isinstance(tx, dict):
                continue
            scanned += 1
            evidence = _find_incoming_transfer(tx, subject_wallet)
            if evidence is not None:
                transfer = _evidence_to_transfer(evidence, subject_wallet)
                return evidence.source, transfer, None
        if not token or len(data) < MAX_FAST_PATH_TRANSACTIONS:
            break
        time.sleep(RPC_SCAN_DELAY_SECONDS)
    return (
        None,
        None,
        f"no incoming transfer found in the first {scanned} candidate transactions",
    )


def _trace_funding_via_signatures(
    subject_wallet: str, endpoint: str
) -> tuple[str | None, TransferRecord | None, str | None]:
    """Trace funding via bounded standard ``getSignaturesForAddress`` paging.

    Pages the wallet's signatures to the oldest page (bounded by
    ``MAX_FUNDING_PAGES``), then scans oldest-to-newer through a bounded window
    of ``MAX_FUNDING_CANDIDATES`` candidate transactions. Failed transactions
    and successful transactions without an incoming native SOL transfer to the
    subject are skipped. Candidates are hydrated sequentially because ordering
    defines "first funding".
    """
    try:
        oldest_sigs, cap_hit = _page_to_oldest(subject_wallet, endpoint)
    except Exception as exc:
        return None, None, f"funding trace failed: {type(exc).__name__}"
    if not oldest_sigs:
        return None, None, "dev wallet has no finalized signatures"
    scanned = 0
    for sig_info in oldest_sigs[:MAX_FUNDING_CANDIDATES]:
        signature = sig_info.get("signature")
        if not isinstance(signature, str):
            continue
        try:
            tx = _rpc_call(
                endpoint,
                "getTransaction",
                [
                    signature,
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
                ],
            )
        except Exception as exc:
            return None, None, f"funding trace failed: {type(exc).__name__}"
        if not isinstance(tx, dict):
            continue
        scanned += 1
        evidence = _find_incoming_transfer(tx, subject_wallet)
        if evidence is not None:
            transfer = _evidence_to_transfer(evidence, subject_wallet)
            warning = (
                "signature page cap hit before reaching the oldest transaction"
                if cap_hit
                else None
            )
            return evidence.source, transfer, warning
        time.sleep(RPC_SCAN_DELAY_SECONDS)
    return (
        None,
        None,
        f"no incoming transfer found in the first {scanned} candidate transactions",
    )


def _page_to_oldest(wallet: str, endpoint: str) -> tuple[list[dict[str, Any]], bool]:
    """Page a wallet's signatures to the oldest page, bounded by the page cap.

    Returns the oldest page's signatures ordered oldest-to-newest and whether the
    page cap was hit before reaching the true oldest signature.
    """
    last_sig: str | None = None
    oldest_page: list[dict[str, Any]] = []
    pages = 0
    while pages < MAX_FUNDING_PAGES:
        params: list[object] = [wallet, {"limit": MAX_PAGE_SIGNATURES}]
        if last_sig:
            params[1] = {"limit": MAX_PAGE_SIGNATURES, "before": last_sig}
        sigs = _rpc_call(endpoint, "getSignaturesForAddress", params)
        if not sigs:
            break
        oldest_page = sigs
        pages += 1
        if len(sigs) < MAX_PAGE_SIGNATURES:
            break
        last_sig = sigs[-1]["signature"]
        time.sleep(RPC_SCAN_DELAY_SECONDS)
    else:
        return list(reversed(oldest_page)), True
    return list(reversed(oldest_page)), False


def _find_incoming_transfer(
    tx: dict[str, Any], subject_wallet: str
) -> _IncomingTransferEvidence | None:
    """Find the first parsed transfer instruction crediting the subject wallet.

    Failed transactions are rejected: a transfer in a failed transaction did not
    execute and is not valid funding evidence.
    """
    meta = tx.get("meta")
    transaction = tx.get("transaction")
    if not isinstance(meta, dict) or not isinstance(transaction, dict):
        return None
    if meta.get("err") is not None:
        return None
    message = transaction.get("message")
    if not isinstance(message, dict):
        return None
    slot = tx.get("slot")
    signatures = transaction.get("signatures")
    signature = signatures[0] if isinstance(signatures, list) and signatures else None
    block_time = tx.get("blockTime") or meta.get("blockTime")
    if not isinstance(slot, int) or not isinstance(signature, str):
        return None

    instructions: list[tuple[int, dict[str, Any]]] = []
    outer = message.get("instructions")
    if isinstance(outer, list):
        for index, instruction in enumerate(outer):
            if isinstance(instruction, dict):
                instructions.append((index, instruction))
    inner = meta.get("innerInstructions")
    if isinstance(inner, list):
        for group in inner:
            if not isinstance(group, dict):
                continue
            group_index = group.get("index", 0)
            group_instructions = group.get("instructions")
            if isinstance(group_instructions, list):
                for inner_index, instruction in enumerate(group_instructions):
                    if isinstance(instruction, dict):
                        instructions.append(
                            (
                                1_000_000 + group_index * 10_000 + inner_index,
                                instruction,
                            )
                        )

    for instruction_index, instruction in instructions:
        parsed = instruction.get("parsed")
        if not isinstance(parsed, dict):
            continue
        parsed_type = parsed.get("type")
        if parsed_type not in ("transfer", "transferChecked"):
            continue
        info = parsed.get("info")
        if not isinstance(info, dict):
            continue
        if info.get("destination") != subject_wallet:
            continue
        source = info.get("source")
        amount = (
            info.get("lamports") if parsed_type == "transfer" else info.get("amount")
        )
        if not isinstance(source, str) or not isinstance(amount, int):
            continue
        return _IncomingTransferEvidence(
            source=source,
            amount_lamports=amount,
            signature=signature,
            slot=slot,
            instruction_index=instruction_index,
            timestamp=block_time if isinstance(block_time, int) else 0,
        )
    return None


def _evidence_to_transfer(
    evidence: _IncomingTransferEvidence, subject_wallet: str
) -> TransferRecord:
    """Build a canonical typed transfer record from incoming-transfer evidence."""
    return TransferRecord(
        signature=evidence.signature,
        instruction_index=evidence.instruction_index,
        slot=evidence.slot,
        timestamp=evidence.timestamp,
        from_wallet=evidence.source,
        to_wallet=subject_wallet,
        amount_lamports=evidence.amount_lamports,
        amount_sol=evidence.amount_lamports / LAMPORTS_PER_SOL,
        root_funder=evidence.source,
        depth=1,
    )


def _synthesize(
    tokens: tuple[GmgnCreatorToken, ...],
) -> tuple[float, float, float, float]:
    """Compute winrate, bundler, and fee synthesis stats from the token array."""
    if not tokens:
        return 0.0, 0.0, 0.0, 0.0
    ath_values = [_to_float(token.token_ath_mc) for token in tokens]
    winrate_100k = sum(
        1 for value in ath_values if value is not None and value >= WINRATE_100K_MC
    ) / len(tokens)
    winrate_250k = sum(
        1 for value in ath_values if value is not None and value >= WINRATE_250K_MC
    ) / len(tokens)
    bundler_values = [
        value
        for value in (_to_float(token.bundler_rate) for token in tokens)
        if value is not None
    ]
    avg_bundler_rate = (
        sum(bundler_values) / len(bundler_values) if bundler_values else 0.0
    )
    fee_values = [
        value
        for value in (_to_float(token.total_fee) for token in tokens)
        if value is not None
    ]
    total_fees_sol = sum(fee_values)
    return winrate_100k, winrate_250k, avg_bundler_rate, total_fees_sol


def _to_float(value: str | None) -> float | None:
    """Parse a numeric text field to float, returning None when unparseable."""
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _token_to_dict(token: GmgnCreatorToken) -> dict[str, object]:
    """Convert one parsed creator token into a display-safe dict."""
    return {
        "token_address": token.address,
        "symbol": token.symbol,
        "create_timestamp": token.create_timestamp,
        "is_open": token.is_open,
        "market_cap": token.market_cap,
        "token_ath_mc": token.token_ath_mc,
        "pool_liquidity": token.pool_liquidity,
        "holders": token.holders,
        "bundler_rate": token.bundler_rate,
        "launchpad_platform": token.launchpad_platform,
        "volume_1h": token.volume_1h,
        "total_fee": token.total_fee,
        "cto_flag": token.cto_flag,
    }


__all__ = ["FunderDiscoveryReport", "discover_funder"]
