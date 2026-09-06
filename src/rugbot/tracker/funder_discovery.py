"""Automated funder discovery: token/wallet seed to dev, funders, and cluster stats.

The on-chain creator and the GMGN-attributed dev entity are tracked as distinct
evidence branches because they are frequently different wallets with different
funding sources. Each branch carries its own funder and typed funding evidence.
"""

# Parsing hostile RPC and GMGN JSON is intentionally branch-heavy and fail-closed.
# ruff: noqa: C901, PLR0912, PLR0913, PLR0915, S310, BLE001, TRY003

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar

from rugbot.domain.decisions import AbstainResult
from rugbot.integrations.pumpfun_creator_index import fetch_pumpfun_created_tokens
from rugbot.integrations.rpc_cache import RpcResponseCache
from rugbot.integrations.solscan import (
    SolscanClient,
    SolscanFundingCandidate,
    SolscanProviderError,
)
from rugbot.intelligence.gmgn_creator_history import (
    GmgnCreatorToken,
    fetch_gmgn_creator_history,
    fetch_gmgn_dev,
)
from rugbot.intelligence.token_resolver import resolve_token_or_wallet
from rugbot.runtime.config import load_provider_settings
from rugbot.tracker.models import (
    FunderRecord,
    TransferRecord,
    WalletRecord,
    WalletStatus,
)
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from rugbot.storage.tracker import SQLiteTrackerRepository

logger = get_logger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000

# Staged funding window shared with the cluster model Launch role (0.2-5 SOL).
# The skill playbook's narrower 0.2-3.0 SOL band is a strict subset of this
# window, so one constant pair serves both call sites.
STAGED_MIN_SOL = 0.2
STAGED_MAX_SOL = 5.0

# Edge-detection floor for nomination paths. There is deliberately NO upper
# bound: sweeps are unbounded (observed 16.64 SOL handoff), so any maximum
# would re-hide the very edges being hunted. The 0.2-5 SOL band still
# gates next-deployer candidacy downstream — detection reports everything
# at or above dust, candidacy interprets it.
EDGE_MIN_SOL = 0.2
EDGE_MAX_SOL = float("inf")

# Free-RPC fallback nomination bounds. Public RPC serves only a short recent
# window, so the fallback scans newest-first and stops early: few pages, no
# oldest-paging crawl, every call budget-charged and fail-fast on 429.
# Pages stay small (25) with a pacing delay because throttled free-tier
# keys 429 on bursts: hydrating 100-signature pages dies before finding
# anything. Pacing applies to production calls only (test transports
# bypass it to stay hermetic and fast).
FREE_RPC_NOMINATION_PAGES = 3
FREE_RPC_NOMINATION_PAGE_LIMIT = 25
FREE_RPC_NOMINATION_PACING_SECONDS = 1.0

MAX_FUNDING_PAGES = 200
MAX_PAGE_SIGNATURES = 1000
MAX_FAST_PATH_TRANSACTIONS = 100
MAX_FUNDING_CANDIDATES = 100
RPC_MAX_RETRIES = 5
RPC_RETRY_BASE_DELAY_SECONDS = 0.5
RPC_SCAN_DELAY_SECONDS = 0.5
RPC_TIMEOUT_SECONDS = 15
RPC_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
HTTP_TOO_MANY_REQUESTS = 429
STAGING_ABSTAIN_RATE_LIMITED = "rate_limited"
STAGING_ABSTAIN_BUDGET_EXCEEDED = "budget_exceeded"
MAX_FUNDING_RPC_CALLS = 25
MAX_STAGED_COUNTERPARTIES = 3
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


class StagingRateLimitedError(RuntimeError):
    """Raised when a staging scan hits rate limiting (fail-fast abstain)."""


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
    fallback_endpoints: tuple[str, ...] | None = None,
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
    providers = load_provider_settings()
    endpoint = endpoint or providers.rpc_http
    resolved_fallback_endpoints = (
        providers.rpc_http_fallbacks
        if fallback_endpoints is None
        else fallback_endpoints
    )
    if not endpoint:
        raise ValueError("SOLANA_RPC_HTTP is required")
    if gmgn_api_key:
        os.environ["GMGN_API_KEY"] = gmgn_api_key
    warnings: list[str] = []

    resolved = resolve_token_or_wallet(
        seed,
        rpc_url=endpoint,
        fallback_endpoints=resolved_fallback_endpoints,
    )
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

    subjects = (
        (onchain_creator,)
        if gmgn_dev == onchain_creator
        else (
            onchain_creator,
            gmgn_dev,
        )
    )
    solscan_candidates: dict[str, SolscanFundingCandidate] = {}
    if providers.solscan_api_key:
        try:
            rows = await asyncio.to_thread(
                SolscanClient(providers.solscan_api_key).funded_by,
                subjects,
            )
            solscan_candidates = {row.address: row for row in rows}
        except (OSError, ValueError, SolscanProviderError) as error:
            warnings.append(f"Solscan funding nomination unavailable: {error}")

    funding_results = await asyncio.gather(
        *(
            asyncio.to_thread(
                _trace_funding,
                subject,
                endpoint,
                solscan_candidate=solscan_candidates.get(subject),
            )
            for subject in subjects
        )
    )
    creator_funder, creator_evidence, creator_warning = funding_results[0]
    if creator_warning:
        warnings.append(creator_warning)

    if gmgn_dev != onchain_creator:
        dev_funder, dev_evidence, dev_warning = funding_results[1]
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


def _trace_funding(
    subject_wallet: str,
    endpoint: str,
    *,
    solscan_candidate: SolscanFundingCandidate | None = None,
) -> tuple[str | None, TransferRecord | None, str | None]:
    """Trace a subject wallet's first successful incoming SOL transfer.

    Uses the provider-capability fast path (``getTransactionsForAddress``)
    oldest-first when available, falling back to bounded standard
    ``getSignaturesForAddress`` paging when the method is unsupported or
    malformed. Returns (funder, evidence, warning).
    """
    candidate_warning: str | None = None
    if solscan_candidate is not None:
        candidate_result = _confirm_solscan_funding(
            subject_wallet,
            endpoint,
            solscan_candidate,
        )
        if candidate_result is not None:
            return candidate_result
        candidate_warning = "Solscan funder candidate failed finalized RPC confirmation"

    fast = _trace_funding_via_transactions(subject_wallet, endpoint)
    if fast is not None:
        return _with_warning(fast, candidate_warning)
    return _with_warning(
        _trace_funding_via_signatures(subject_wallet, endpoint),
        candidate_warning,
    )


def _confirm_solscan_funding(
    subject_wallet: str,
    endpoint: str,
    candidate: SolscanFundingCandidate,
) -> tuple[str, TransferRecord, None] | None:
    """Confirm one indexed candidate by decoding its finalized RPC transaction."""

    try:
        transaction = _rpc_call(
            endpoint,
            "getTransaction",
            [
                candidate.transaction_signature,
                {
                    "commitment": "finalized",
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )
    except Exception:
        return None
    if not isinstance(transaction, dict):
        return None
    evidence = _find_incoming_transfer(transaction, subject_wallet)
    if evidence is None or evidence.source != candidate.funded_by:
        return None
    return evidence.source, _evidence_to_transfer(evidence, subject_wallet), None


def _with_warning(
    result: tuple[str | None, TransferRecord | None, str | None],
    warning: str | None,
) -> tuple[str | None, TransferRecord | None, str | None]:
    if warning is None:
        return result
    funder, evidence, fallback_warning = result
    combined = warning if fallback_warning is None else f"{warning}; {fallback_warning}"
    return funder, evidence, combined


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


@dataclass(frozen=True, slots=True)
class StagedTransferCandidate:
    """One native SOL transfer staging a creator wallet under review."""

    wallet: str
    amount_sol: float
    slot: int
    signature: str
    nominated_by: str = "rpc"


@dataclass(frozen=True, slots=True)
class OutboundStagedEdge:
    """One staged recipient linked (or noted) from a creator's outbound flow."""

    wallet: str
    amount_sol: float
    slot: int
    signature: str
    created_mints: tuple[str, ...] = ()
    linked: bool = False
    source: str = "rpc"


@dataclass(frozen=True, slots=True)
class InboundStagedEdge:
    """One staged funder linked (or noted) from a creator's inbound flow."""

    wallet: str
    amount_sol: float
    slot: int
    signature: str
    created_mints: tuple[str, ...] = ()
    linked: bool = False
    source: str = "rpc"


StagedEdgeT = TypeVar("StagedEdgeT", OutboundStagedEdge, InboundStagedEdge)


def _link_staged(
    transfers: list[StagedTransferCandidate],
    created_mints_by_wallet: dict[str, list[str]],
    *,
    min_sol: float,
    max_sol: float,
    edge_cls: type[StagedEdgeT],
) -> list[StagedEdgeT]:
    """Shared pure core for staged-edge linking in either direction."""
    edges: list[StagedEdgeT] = []
    for transfer in transfers:
        if transfer.amount_sol < min_sol or transfer.amount_sol > max_sol:
            continue
        mints = tuple(created_mints_by_wallet.get(transfer.wallet, ()))
        edges.append(
            edge_cls(
                wallet=transfer.wallet,
                amount_sol=transfer.amount_sol,
                slot=transfer.slot,
                signature=transfer.signature,
                created_mints=mints,
                linked=bool(mints),
                source=transfer.nominated_by,
            )
        )
    return edges


def link_outbound_staged(
    transfers: list[StagedTransferCandidate],
    created_mints_by_wallet: dict[str, list[str]],
    *,
    min_sol: float = STAGED_MIN_SOL,
    max_sol: float = STAGED_MAX_SOL,
) -> list[OutboundStagedEdge]:
    """Link staged outbound transfers to later token creators (pure).

    Args:
        transfers: Outbound transfer candidates from one creator wallet.
        created_mints_by_wallet: Indexed pump::create mints keyed by wallet.
        min_sol: Inclusive lower bound of the staging window in SOL.
        max_sol: Inclusive upper bound of the staging window in SOL.

    Returns:
        Edges for in-window transfers only. Recipients with indexed mints
        are marked linked; other in-window recipients are noted unlinked.
        Out-of-window transfers are ignored.
    """
    return _link_staged(
        transfers,
        created_mints_by_wallet,
        min_sol=min_sol,
        max_sol=max_sol,
        edge_cls=OutboundStagedEdge,
    )


def link_inbound_staged(
    transfers: list[StagedTransferCandidate],
    created_mints_by_wallet: dict[str, list[str]],
    *,
    min_sol: float = STAGED_MIN_SOL,
    max_sol: float = STAGED_MAX_SOL,
) -> list[InboundStagedEdge]:
    """Link staged inbound funders to token creators (pure).

    Unlike the first-incoming-only funding trace, every in-window inbound
    funder is reported; funders with indexed pump::create mints are marked
    linked. Out-of-window transfers are ignored.

    Args:
        transfers: Inbound transfer candidates crediting one creator wallet.
        created_mints_by_wallet: Indexed pump::create mints keyed by wallet.
        min_sol: Inclusive lower bound of the staging window in SOL.
        max_sol: Inclusive upper bound of the staging window in SOL.

    Returns:
        Edges for in-window transfers only.
    """
    return _link_staged(
        transfers,
        created_mints_by_wallet,
        min_sol=min_sol,
        max_sol=max_sol,
        edge_cls=InboundStagedEdge,
    )


def _staged_edges_to_json(edges: list[Any]) -> list[dict[str, object]]:
    """Serialize staged edges for the existing JSON pipeline."""
    return [
        {
            "wallet": edge.wallet,
            "amount_sol": edge.amount_sol,
            "slot": edge.slot,
            "signature": edge.signature,
            "created_mints": list(edge.created_mints),
            "linked": edge.linked,
            "source": edge.source,
        }
        for edge in edges
    ]


def outbound_staged_to_json(edges: list[OutboundStagedEdge]) -> list[dict[str, object]]:
    """Serialize outbound staged edges for the existing JSON pipeline."""
    return _staged_edges_to_json(edges)


def inbound_staged_to_json(edges: list[InboundStagedEdge]) -> list[dict[str, object]]:
    """Serialize inbound staged edges for the existing JSON pipeline."""
    return _staged_edges_to_json(edges)


def scan_outbound_staging(
    creator_wallet: str,
    endpoint: str,
    *,
    transport: Callable[[str, str, list[object]], object] | None = None,
    solscan_api_key: str | None = None,
    solscan_client: SolscanClient | None = None,
) -> tuple[list[OutboundStagedEdge], str | None, int]:
    """Scan a creator wallet's bounded finalized outbound staged transfers.

    Question-driven: a bounded oldest-first scan of the Solscan Transfers
    tab (native SOL, outflow) is filtered locally to the 0.2-15 SOL edge
    window; only surviving counterparties (at most 3) are RPC-confirmed,
    one call each. Each staged recipient is then checked for later
    pump::create mints via the existing creator-index path.

    Fail-fast: the first HTTP 429 abstains the whole scan with reason
    ``rate_limited``. There are no rotation retries. RPC spend is capped
    by ``MAX_FUNDING_RPC_CALLS``; on exceed the scan returns partial
    edges with reason ``budget_exceeded``.

    Args:
        creator_wallet: The creator wallet whose outbound flow is scanned.
        endpoint: Solana RPC HTTP endpoint.
        transport: Optional single-attempt RPC transport (test seam).
        solscan_api_key: Optional Solscan key enabling indexed nomination.
        solscan_client: Optional Solscan client override (test seam).

    Returns:
        Tuple of linked/noted staged edges, an optional warning, and the
        number of RPC calls made (the index call is not counted).
    """
    return _scan_staged(
        creator_wallet,
        endpoint,
        direction="outbound",
        link_fn=link_outbound_staged,
        transport=transport,
        solscan_api_key=solscan_api_key,
        solscan_client=solscan_client,
    )


def scan_inbound_staging(
    creator_wallet: str,
    endpoint: str,
    *,
    solscan_api_key: str | None = None,
    transport: Callable[[str, str, list[object]], object] | None = None,
    solscan_client: SolscanClient | None = None,
) -> tuple[list[InboundStagedEdge], str | None, int]:
    """Scan a creator wallet's bounded finalized inbound staged funders.

    Question-driven: a bounded oldest-first scan of the Solscan Transfers
    tab (native SOL, inflow) is filtered locally to the 0.2-15 SOL edge
    window; only surviving counterparties (at most 3) are RPC-confirmed,
    one call each. Unlike the first-incoming-only funding trace, every in-window
    inbound funder is collected and each funder is checked for
    pump::create mints via the existing creator-index path, so later
    upstream funders that are themselves creators stay visible.

    Fail-fast: the first HTTP 429 abstains the whole scan with reason
    ``rate_limited``. There are no rotation retries. RPC spend is capped
    by ``MAX_FUNDING_RPC_CALLS``; on exceed the scan returns partial
    edges with reason ``budget_exceeded``.

    Args:
        creator_wallet: The creator wallet whose inbound flow is scanned.
        endpoint: Solana RPC HTTP endpoint.
        solscan_api_key: Optional Solscan key enabling indexed nomination.
        transport: Optional single-attempt RPC transport (test seam).
        solscan_client: Optional Solscan client override (test seam).

    Returns:
        Tuple of linked/noted staged edges, an optional warning, and the
        number of RPC calls made (the index call is not counted).
    """
    return _scan_staged(
        creator_wallet,
        endpoint,
        direction="inbound",
        link_fn=link_inbound_staged,
        solscan_api_key=solscan_api_key,
        transport=transport,
        solscan_client=solscan_client,
    )


@dataclass(slots=True)
class _RpcBudget:
    """Mutable RPC spend counter bounding one funding-edge scan."""

    limit: int = MAX_FUNDING_RPC_CALLS
    calls_made: int = 0

    def charge(self) -> bool:
        """Charge one RPC call, returning False when the budget is spent."""
        if self.calls_made >= self.limit:
            return False
        self.calls_made += 1
        return True


def _nominate_via_transfer_endpoint(
    client: SolscanClient,
    wallet: str,
    direction: str,
) -> tuple[list[StagedTransferCandidate], str | None]:
    """Nominate staged counterparties from the Solscan Transfers tab.

    One bounded oldest-first scan of ``GET /account/transfer`` (native SOL,
    direction-mapped flow) is filtered locally to the edge-detection window
    (0.2-15 SOL). Transfer rows carry no slot, so candidates use slot ``-1``
    and confirmation matches on signature plus counterparty instead.

    Raises:
        SolscanProviderError: On index failure (429 handled upstream).
    """

    flow = "out" if direction == "outbound" else "in"
    rows, scan_warning = client.account_transfer_scan(wallet, flow=flow)
    staged: list[StagedTransferCandidate] = []
    for row in rows:
        if direction == "outbound":
            if row.from_address != wallet or row.to_address == wallet:
                continue
            other = row.to_address
        elif row.to_address != wallet or row.from_address == wallet:
            continue
        else:
            other = row.from_address
        if row.amount_sol < EDGE_MIN_SOL or row.amount_sol > EDGE_MAX_SOL:
            continue
        staged.append(
            StagedTransferCandidate(
                wallet=other,
                amount_sol=row.amount_sol,
                slot=-1,
                signature=row.signature,
                nominated_by="solscan-transfer",
            )
        )
    return staged, scan_warning


def _solscan_failure_label(exc: Exception) -> str:
    """Summarize a Solscan failure without leaking response bodies or keys."""

    match = re.search(r"HTTP (\d{3})", str(exc))
    if match is not None:
        return f"HTTP {match.group(1)}"
    if "cooldown" in str(exc):
        return "cooldown"
    return type(exc).__name__


def _pace_nomination(
    transport: Callable[[str, str, list[object]], object] | None,
) -> None:
    """Pause between free-RPC nomination calls on the production path.

    Throttled free-tier keys 429 on bursts; test transports bypass the
    pause to stay hermetic and fast.
    """

    if transport is None and FREE_RPC_NOMINATION_PACING_SECONDS > 0:
        time.sleep(FREE_RPC_NOMINATION_PACING_SECONDS)


def _nominate_via_free_rpc(
    wallet: str,
    direction: str,
    endpoint: str,
    budget: _RpcBudget,
    transport: Callable[[str, str, list[object]], object] | None = None,
) -> tuple[list[StagedTransferCandidate], str | None]:
    """Nominate staged counterparties from bounded free-RPC paging (no key).

    Scans newest-first signature pages (public RPC only serves a short
    recent window) and hydrates each signature, collecting in-window
    transfers in the requested direction. Every RPC call is budget-charged;
    the first 429 raises fail-fast. Per-transaction hydration failures are
    skipped so one bad receipt cannot sink the scan.

    Raises:
        StagingRateLimitedError: On RPC rate limiting or budget exhaustion
            mid-page (partial edges are lost; the caller reports the note).
    """

    tx_params = {
        "commitment": "finalized",
        "encoding": "jsonParsed",
        "maxSupportedTransactionVersion": 0,
    }
    staged: list[StagedTransferCandidate] = []
    before: str | None = None
    for _ in range(FREE_RPC_NOMINATION_PAGES):
        if not budget.charge():
            return staged, STAGING_ABSTAIN_BUDGET_EXCEEDED
        _pace_nomination(transport)
        page_params: list[object] = [
            wallet,
            {"limit": FREE_RPC_NOMINATION_PAGE_LIMIT, "commitment": "finalized"},
        ]
        if before is not None:
            page_params[1] = {
                "limit": FREE_RPC_NOMINATION_PAGE_LIMIT,
                "commitment": "finalized",
                "before": before,
            }
        page = _staging_rpc_call(
            endpoint, "getSignaturesForAddress", page_params, transport=transport
        )
        if not isinstance(page, list) or not page:
            break
        for entry in page:
            if not isinstance(entry, dict):
                continue
            signature = entry.get("signature")
            if not isinstance(signature, str):
                continue
            if not budget.charge():
                return staged, STAGING_ABSTAIN_BUDGET_EXCEEDED
            _pace_nomination(transport)
            try:
                tx = _staging_rpc_call(
                    endpoint,
                    "getTransaction",
                    [signature, tx_params],
                    transport=transport,
                )
            except StagingRateLimitedError:
                raise
            except Exception as exc:
                logger.warning("free-RPC nomination hydration failed: %s", exc)
                continue
            if not isinstance(tx, dict):
                continue
            slot = tx.get("slot")
            if not isinstance(slot, int):
                continue
            if direction == "outbound":
                evidence = _find_outgoing_transfers(tx, wallet)
                others = [(item.destination, item.amount_lamports) for item in evidence]
            else:
                evidence = _find_all_incoming_transfers(tx, wallet)
                others = [(item.source, item.amount_lamports) for item in evidence]
            for other, lamports in others:
                if other == wallet:
                    continue
                amount_sol = lamports / LAMPORTS_PER_SOL
                if amount_sol < EDGE_MIN_SOL or amount_sol > EDGE_MAX_SOL:
                    continue
                staged.append(
                    StagedTransferCandidate(
                        wallet=other,
                        amount_sol=amount_sol,
                        slot=slot,
                        signature=signature,
                        nominated_by="rpc",
                    )
                )
        last = page[-1]
        last_sig = last.get("signature") if isinstance(last, dict) else None
        if len(page) < FREE_RPC_NOMINATION_PAGE_LIMIT or not isinstance(last_sig, str):
            break
        before = last_sig
    return staged, None


def find_funding_edges(
    wallet: str,
    direction: str,
    endpoint: str,
    *,
    solscan_client: SolscanClient | None = None,
    transport: Callable[[str, str, list[object]], object] | None = None,
    budget: _RpcBudget | None = None,
) -> tuple[list[StagedTransferCandidate], str | None]:
    """Answer one funding-edge question, Solscan-first with free fallback.

    The Solscan Transfers tab (native SOL, oldest-first, direction-mapped
    flow) nominates counterparties when a client is available; otherwise,
    or when the index fails without rate limiting (bad key, 401, outage),
    nomination falls back to bounded free-RPC paging with an honest note.
    Only survivors in the 0.2-15 SOL edge window (at most
    ``MAX_STAGED_COUNTERPARTIES``) are RPC-confirmed, one ``getTransaction``
    call each.

    The Solscan Transfers tab (native SOL, oldest-first, direction-mapped
    flow) nominates counterparties; only survivors in the 0.2-15 SOL edge
    window (at most ``MAX_STAGED_COUNTERPARTIES``) are RPC-confirmed, one
    ``getTransaction`` call each. Transfer rows carry no slot, so
    confirmation matches on signature plus counterparty and takes the slot
    from finalized RPC. Everything else is answered from the index data
    and never hydrated.

    Args:
        wallet: The wallet whose funding edges are answered.
        direction: ``"inbound"`` (funders) or ``"outbound"`` (recipients).
        endpoint: Solana RPC HTTP endpoint.
        solscan_client: Indexed transfer-listing client.
        transport: Optional single-attempt RPC transport (test seam).
        budget: Optional RPC spend counter (a fresh one is built by
            default).

    Returns:
        Tuple of RPC-confirmed staged candidates with ``solscan``
        provenance and an optional warning (``budget_exceeded`` when the
        RPC budget ran out mid-confirmation, leaving partial edges).

    Raises:
        StagingRateLimitedError: On Solscan or RPC rate limiting.
    """
    own_budget = budget if budget is not None else _RpcBudget()
    fallback_note: str | None = None
    if solscan_client is not None:
        try:
            nominated, scan_warning = _nominate_via_transfer_endpoint(
                solscan_client, wallet, direction
            )
        except SolscanProviderError as exc:
            if "429" in str(exc) or "cooldown" in str(exc):
                raise StagingRateLimitedError(str(exc)) from exc
            logger.warning("funding edge index lookup failed for %s: %s", wallet, exc)
            nominated, fallback_note = _nominate_via_free_rpc(
                wallet, direction, endpoint, own_budget, transport
            )
            scan_warning = (
                f"solscan unavailable ({_solscan_failure_label(exc)}), "
                "free-RPC nomination" + (f"; {fallback_note}" if fallback_note else "")
            )
    else:
        nominated, fallback_note = _nominate_via_free_rpc(
            wallet, direction, endpoint, own_budget, transport
        )
        scan_warning = "solscan unavailable, free-RPC nomination" + (
            f"; {fallback_note}" if fallback_note else ""
        )
    survivors = _newest_per_wallet(nominated)[:MAX_STAGED_COUNTERPARTIES]
    confirmed: list[StagedTransferCandidate] = []
    scan_note = scan_warning
    for survivor in survivors:
        if survivor.nominated_by == "rpc":
            # Already sourced from a finalized RPC hydration during
            # nomination; re-fetching the same transaction would spend
            # budget to re-parse identical data.
            confirmed.append(survivor)
            continue
        if not own_budget.charge():
            logger.warning("funding edge scan exceeded its RPC budget")
            return confirmed, STAGING_ABSTAIN_BUDGET_EXCEEDED
        try:
            tx = _staging_rpc_call(
                endpoint,
                "getTransaction",
                [
                    survivor.signature,
                    {
                        "commitment": "finalized",
                        "encoding": "jsonParsed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
                transport=transport,
            )
        except StagingRateLimitedError:
            raise
        except Exception as exc:
            logger.warning("funding edge confirmation failed: %s", exc)
            continue
        if not isinstance(tx, dict):
            continue
        match = _match_confirmed_transfer(tx, wallet, direction, survivor)
        if match is not None:
            confirmed.append(match)
    return confirmed, scan_note


def _match_confirmed_transfer(
    tx: dict[str, Any],
    wallet: str,
    direction: str,
    survivor: StagedTransferCandidate,
) -> StagedTransferCandidate | None:
    """Confirm one indexed survivor against its finalized RPC transaction.

    Returns a decoded candidate with ``solscan`` provenance when the
    finalized transaction carries the same counterparty transfer, else
    None (fail-closed on mismatch). Transfer-endpoint nominees carry slot
    ``-1`` (the Transfers tab exposes no slot), so the slot check is
    skipped for them: the fetched signature is globally unique, making
    signature plus counterparty an airtight match.
    """
    skip_slot_check = survivor.slot < 0
    if direction == "outbound":
        for evidence in _find_outgoing_transfers(tx, wallet):
            if evidence.destination != survivor.wallet:
                continue
            if not skip_slot_check and evidence.slot != survivor.slot:
                continue
            return StagedTransferCandidate(
                wallet=evidence.destination,
                amount_sol=evidence.amount_lamports / LAMPORTS_PER_SOL,
                slot=evidence.slot,
                signature=evidence.signature,
                nominated_by=survivor.nominated_by,
            )
        return None
    for evidence in _find_all_incoming_transfers(tx, wallet):
        if evidence.source != survivor.wallet:
            continue
        if not skip_slot_check and evidence.slot != survivor.slot:
            continue
        return StagedTransferCandidate(
            wallet=evidence.source,
            amount_sol=evidence.amount_lamports / LAMPORTS_PER_SOL,
            slot=evidence.slot,
            signature=evidence.signature,
            nominated_by=survivor.nominated_by,
        )
    return None


def _scan_staged(
    creator_wallet: str,
    endpoint: str,
    *,
    direction: str,
    link_fn: Callable[..., list[StagedEdgeT]],
    solscan_api_key: str | None = None,
    transport: Callable[[str, str, list[object]], object] | None = None,
    solscan_client: SolscanClient | None = None,
) -> tuple[list[StagedEdgeT], str | None, int]:
    """Shared question-driven staging scan for either flow direction.

    Answers ``find_funding_edges`` Solscan-first with a bounded free-RPC
    fallback (no key needed), RPC-confirming only survivors, then checks
    each staged counterparty for pump::create mints via the existing
    creator-index path. Linking uses the edge-detection window (0.2-15
    SOL) so sweep-size edges stay visible; deployer candidacy still gates
    on 0.2-5 SOL downstream.

    Fail-fast: the first rate-limit signal abstains the scan immediately
    with reason ``rate_limited``. No endpoint rotation is attempted.
    """
    client = solscan_client
    if client is None and solscan_api_key:
        client = SolscanClient(solscan_api_key)
    budget = _RpcBudget()
    try:
        staged = find_funding_edges(
            creator_wallet,
            direction,
            endpoint,
            solscan_client=client,
            transport=transport,
            budget=budget,
        )
    except StagingRateLimitedError:
        logger.warning("%s staging scan abstained: rate limited", direction)
        return [], STAGING_ABSTAIN_RATE_LIMITED, budget.calls_made
    candidates, warning = staged
    created_mints_by_wallet: dict[str, list[str]] = {}
    for wallet in [candidate.wallet for candidate in candidates]:
        created_mints_by_wallet[wallet] = _indexed_created_mints(wallet)
    return (
        link_fn(
            candidates,
            created_mints_by_wallet,
            min_sol=EDGE_MIN_SOL,
            max_sol=EDGE_MAX_SOL,
        ),
        warning,
        budget.calls_made,
    )


def _newest_per_wallet(
    candidates: list[StagedTransferCandidate],
) -> list[StagedTransferCandidate]:
    """Keep the first (newest-scanned) candidate per wallet (pure)."""
    newest: dict[str, StagedTransferCandidate] = {}
    for candidate in candidates:
        newest.setdefault(candidate.wallet, candidate)
    return list(newest.values())


_STAGING_RPC_CACHE: RpcResponseCache | None = None


def get_shared_rpc_cache() -> RpcResponseCache | None:
    """Return the process-wide staging RPC cache, or None when unavailable.

    Construction failure (e.g. unwritable state dir) degrades to uncached
    scans rather than breaking funding discovery. The trade-graph
    traversal shares this file so REST pages and RPC receipts accumulate
    in one quota pool.
    """

    global _STAGING_RPC_CACHE  # noqa: PLW0603
    if _STAGING_RPC_CACHE is not None:
        return _STAGING_RPC_CACHE
    try:
        _STAGING_RPC_CACHE = RpcResponseCache()
    except Exception:
        logger.debug("staging RPC cache unavailable, scanning uncached")
        return None
    return _STAGING_RPC_CACHE


def _staging_rpc_call(
    endpoint: str,
    method: str,
    params: list[object],
    *,
    transport: Callable[[str, str, list[object]], object] | None = None,
) -> object:
    """Perform exactly one staging RPC call (no retries, no rotation).

    Production calls (no transport seam) are served from the persistent
    RPC cache first and stored on success, so repeat scans stop re-burning
    public-RPC quota. Failures are never cached. Test transports bypass
    the cache to stay hermetic.

    Args:
        endpoint: Solana RPC HTTP endpoint.
        method: JSON-RPC method name.
        params: JSON-RPC parameters.
        transport: Optional test transport replacing the single HTTP call.

    Returns:
        The decoded ``result`` payload.

    Raises:
        StagingRateLimitedError: On HTTP 429.
    """
    if transport is not None:
        return transport(endpoint, method, params)
    cache = get_shared_rpc_cache()
    if cache is not None:
        try:
            hit = cache.lookup(method, params)
        except Exception:
            hit = None
        if isinstance(hit, dict) and "result" in hit:
            return hit["result"]
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
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
            result = data.get("result")
            if cache is not None and isinstance(result, (dict, list)):
                try:
                    cache.store(method, params, {"result": result})
                except Exception:
                    logger.debug("staging RPC cache store failed for %s", method)
            return result
    except urllib.error.HTTPError as exc:
        if exc.code == HTTP_TOO_MANY_REQUESTS:
            raise StagingRateLimitedError(
                f"staging RPC rate limited on {method}"
            ) from exc
        raise


@dataclass(frozen=True, slots=True)
class _OutgoingTransferEvidence:
    """Typed outbound-transfer evidence for one creator wallet."""

    destination: str
    amount_lamports: int
    signature: str
    slot: int
    instruction_index: int
    timestamp: int


def _find_all_incoming_transfers(
    tx: dict[str, Any], subject_wallet: str
) -> list[_IncomingTransferEvidence]:
    """Find every parsed transfer instruction crediting the subject wallet.

    Failed transactions are rejected: a transfer in a failed transaction did
    not execute and is not valid staging evidence.
    """
    meta = tx.get("meta")
    transaction = tx.get("transaction")
    if not isinstance(meta, dict) or not isinstance(transaction, dict):
        return []
    if meta.get("err") is not None:
        return []
    message = transaction.get("message")
    if not isinstance(message, dict):
        return []
    slot = tx.get("slot")
    signatures = transaction.get("signatures")
    signature = signatures[0] if isinstance(signatures, list) and signatures else None
    block_time = tx.get("blockTime") or meta.get("blockTime")
    if not isinstance(slot, int) or not isinstance(signature, str):
        return []

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

    found: list[_IncomingTransferEvidence] = []
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
        found.append(
            _IncomingTransferEvidence(
                source=source,
                amount_lamports=amount,
                signature=signature,
                slot=slot,
                instruction_index=instruction_index,
                timestamp=block_time if isinstance(block_time, int) else 0,
            )
        )
    return found


def _find_outgoing_transfers(
    tx: dict[str, Any], subject_wallet: str
) -> list[_OutgoingTransferEvidence]:
    """Find every parsed transfer instruction debiting the subject wallet.

    Failed transactions are rejected: a transfer in a failed transaction did
    not execute and is not valid staging evidence.
    """
    meta = tx.get("meta")
    transaction = tx.get("transaction")
    if not isinstance(meta, dict) or not isinstance(transaction, dict):
        return []
    if meta.get("err") is not None:
        return []
    message = transaction.get("message")
    if not isinstance(message, dict):
        return []
    slot = tx.get("slot")
    signatures = transaction.get("signatures")
    signature = signatures[0] if isinstance(signatures, list) and signatures else None
    block_time = tx.get("blockTime") or meta.get("blockTime")
    if not isinstance(slot, int) or not isinstance(signature, str):
        return []

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

    found: list[_OutgoingTransferEvidence] = []
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
        if info.get("source") != subject_wallet:
            continue
        destination = info.get("destination")
        amount = (
            info.get("lamports") if parsed_type == "transfer" else info.get("amount")
        )
        if not isinstance(destination, str) or not isinstance(amount, int):
            continue
        found.append(
            _OutgoingTransferEvidence(
                destination=destination,
                amount_lamports=amount,
                signature=signature,
                slot=slot,
                instruction_index=instruction_index,
                timestamp=block_time if isinstance(block_time, int) else 0,
            )
        )
    return found


def _indexed_created_mints(wallet: str) -> list[str]:
    """Return indexed pump.fun mints for one wallet, fail-soft on error."""
    try:
        candidates = fetch_pumpfun_created_tokens(wallet)
    except Exception as exc:
        logger.warning("creator index lookup failed for %s: %s", wallet, exc)
        return []
    return [candidate.mint for candidate in candidates]


__all__ = [
    "MAX_FUNDING_RPC_CALLS",
    "STAGING_ABSTAIN_BUDGET_EXCEEDED",
    "STAGING_ABSTAIN_RATE_LIMITED",
    "FunderDiscoveryReport",
    "InboundStagedEdge",
    "OutboundStagedEdge",
    "StagedTransferCandidate",
    "StagingRateLimitedError",
    "discover_funder",
    "find_funding_edges",
    "get_shared_rpc_cache",
    "inbound_staged_to_json",
    "link_inbound_staged",
    "link_outbound_staged",
    "outbound_staged_to_json",
    "scan_inbound_staging",
    "scan_outbound_staging",
]
