"""Runtime coordinator for the insider cabal intelligence pipeline.

Subscribes to tracked cabal wallets via WebSocket/RPC, applies multi-factor
signal gating, dispatches rich alerts to Telegram and Discord, and manages
paper copytrade execution with trailing stops and TP ladders.
"""

# ruff: noqa: S310, PLR0913, BLE001, ARG001, C901, PLR0912, PLR0915, FBT001, FBT002, ANN401

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from rugbot.discover.cabal import CabalCluster, CabalStore
from rugbot.execution.cabal_executor import CabalActivePosition, CabalExecutor
from rugbot.integrations.pumpfun import PumpPortalStream
from rugbot.integrations.pumpfun_api import PumpFunApiClient, get_client
from rugbot.integrations.rpc_access import resolve_rpc_endpoints
from rugbot.intelligence.signal_filter import SignalDecision, SignalFilter
from rugbot.tracker.funding_chain import _rpc_call
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

DISCORD_WEBHOOK_TIMEOUT_SECONDS: Final[int] = 10
TELEGRAM_TIMEOUT_SECONDS: Final[int] = 10
DISCORD_COLOR_BUY: Final[int] = 0x00FFA3  # Solana Green
DISCORD_COLOR_ABSTAIN: Final[int] = 0x64748B  # Slate Grey
DISCORD_COLOR_REVIEW: Final[int] = 0xF59E0B  # Amber
DISCORD_COLOR_DUMP: Final[int] = 0xEF4444  # Crimson Red
SOL_DELTA_THRESHOLD_SOL: Final[float] = 0.0001
MAX_SEEN_SIGNATURES: Final[int] = 2000
PRUNE_SEEN_SIGNATURES: Final[int] = 1000


class SizingMode(StrEnum):
    """Supported position sizing algorithms."""

    FIXED = "fixed"
    PROPORTIONAL = "proportional"
    BALANCE_PCT = "balance_pct"


@dataclass(frozen=True, slots=True)
class PositionSizingConfig:
    """Configurable sizing parameters for risk and exposure management."""

    mode: SizingMode = SizingMode.FIXED
    fixed_size_sol: float = 0.25
    copy_ratio: float = 0.50  # 50% of insider buy in proportional mode
    balance_pct: float = 10.0  # 10% of portfolio in balance_pct mode
    max_position_sol: float = 1.00  # Hard ceiling per snipe
    min_position_sol: float = 0.05  # Hard floor to cover fees
    gas_reserve_sol: float = 0.05  # Reserve lamports for rent & gas

    def calculate_entry_size(
        self,
        target_buy_sol: float,
        available_balance_sol: float = 2.0,
    ) -> float:
        """Compute exact order size adhering to configured risk rules and bounds."""
        if self.mode == SizingMode.FIXED:
            raw = self.fixed_size_sol
        elif self.mode == SizingMode.PROPORTIONAL:
            raw = target_buy_sol * self.copy_ratio
        elif self.mode == SizingMode.BALANCE_PCT:
            raw = available_balance_sol * (self.balance_pct / 100.0)
        else:
            raw = self.fixed_size_sol

        # Apply hard boundary caps
        bounded = max(self.min_position_sol, min(raw, self.max_position_sol))

        # Enforce gas reserve buffer
        max_spendable = max(0.0, available_balance_sol - self.gas_reserve_sol)
        if max_spendable > 0:
            bounded = min(bounded, max_spendable)

        return round(bounded, 4)


def send_discord_cabal_alert(
    webhook_url: str,
    decision: SignalDecision,
    cluster: CabalCluster | None,
    *,
    token_name: str = "",
    token_symbol: str = "",
    market_cap_usd: float = 0.0,
) -> bool:
    """Post rich embed alert to Discord webhook (fail-soft)."""
    if not webhook_url.strip():
        return False

    if decision.action == "BUY":
        color = DISCORD_COLOR_BUY
        title = f"🎯 Cabal Signal: BUY {token_symbol or decision.mint[:8]}"
    elif decision.needs_manual_review:
        color = DISCORD_COLOR_REVIEW
        title = f"⚠️ MANUAL REVIEW: High-Conviction Solo Buy {token_symbol or decision.mint[:8]}"
    else:
        color = DISCORD_COLOR_ABSTAIN
        title = f"🎯 Cabal Signal: ABSTAIN {token_symbol or decision.mint[:8]}"

    dex_url = f"https://dexscreener.com/solana/{decision.mint}"

    cluster_id = cluster.cluster_id if cluster else "unknown"
    funder = cluster.funder if cluster else "unknown"

    fields = [
        {
            "name": "Mint",
            "value": f"[{decision.mint[:8]}...]({dex_url})",
            "inline": True,
        },
        {"name": "Symbol", "value": token_symbol or "UNKNOWN", "inline": True},
        {"name": "Market Cap", "value": f"${market_cap_usd:,.0f}", "inline": True},
        {"name": "Cabal Cluster", "value": f"`{cluster_id}`", "inline": True},
        {"name": "Funder", "value": f"`{funder[:8]}...`", "inline": True},
        {
            "name": "Buy Amount",
            "value": f"{decision.amount_sol:.3f} SOL",
            "inline": True,
        },
        {
            "name": "Confluence",
            "value": f"{decision.confluence_count} wallet(s)",
            "inline": True,
        },
        {
            "name": "Conviction Ratio",
            "value": f"{decision.conviction_ratio * 100:.0f}%",
            "inline": True,
        },
        {"name": "Signal Score", "value": f"{decision.score:.2f}", "inline": True},
    ]

    if decision.reasons:
        fields.append(
            {
                "name": "Abstain Reasons",
                "value": "\n".join(f"• {r}" for r in decision.reasons),
                "inline": False,
            }
        )

    payload = {
        "embeds": [
            {
                "title": title,
                "url": dex_url,
                "color": color,
                "fields": fields,
                "footer": {"text": "rugbot cabal pipeline • paper/observe"},
                "timestamp": datetime.now(UTC).isoformat(),
            }
        ]
    }

    try:
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "rugbot/2.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=DISCORD_WEBHOOK_TIMEOUT_SECONDS):
            return True
    except Exception as exc:
        logger.warning("Failed sending Discord alert: %s", exc)
        return False


def send_telegram_cabal_alert(
    token: str,
    chat_id: str | int,
    decision: SignalDecision,
    cluster: CabalCluster | None,
    *,
    token_symbol: str = "",
) -> bool:
    """Post notification message to Telegram bot chat (fail-soft)."""
    if not token.strip() or not chat_id:
        return False

    if decision.action == "BUY":
        emoji = "🟢 <b>BUY</b>"
    elif decision.needs_manual_review:
        emoji = "⚠️ <b>MANUAL REVIEW REQUIRED (High Conviction Solo Buy)</b>"
    else:
        emoji = "⚪ <b>ABSTAIN</b>"
    cluster_id = cluster.cluster_id if cluster else "unknown"

    text = (
        f"{emoji} <b>Cabal Trade Detected</b>\n"
        f"<b>Token:</b> {token_symbol or decision.mint[:8]} (<code>{decision.mint}</code>)\n"
        f"<b>Cabal:</b> <code>{cluster_id}</code>\n"
        f"<b>Buyer:</b> <code>{decision.wallet}</code>\n"
        f"<b>Amount:</b> {decision.amount_sol:.3f} SOL\n"
        f"<b>Confluence:</b> {decision.confluence_count} wallet(s)\n"
        f"<b>Conviction:</b> {decision.conviction_ratio * 100:.0f}%\n"
        f"<b>Score:</b> {decision.score:.2f}\n"
    )
    if decision.reasons:
        text += "<b>Reasons:</b>\n" + "\n".join(f"• {r}" for r in decision.reasons)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TELEGRAM_TIMEOUT_SECONDS):
            return True
    except Exception as exc:
        logger.warning("Failed sending Telegram alert: %s", exc)
        return False


def send_discord_exit_alert(
    webhook_url: str,
    position: CabalActivePosition,
    *,
    token_symbol: str = "",
) -> bool:
    """Send position exit notification to Discord webhook."""
    if not webhook_url.strip():
        return False

    is_profit = position.realized_pnl_sol > 0
    color = DISCORD_COLOR_BUY if is_profit else DISCORD_COLOR_DUMP
    title = f"🔔 Position Closed: {token_symbol or position.mint[:8]} ({position.exit_reason})"
    dex_url = f"https://dexscreener.com/solana/{position.mint}"

    roi_pct = (
        (position.realized_pnl_sol) / max(0.001, position.entry_sol_amount)
    ) * 100.0

    payload = {
        "embeds": [
            {
                "title": title,
                "url": dex_url,
                "color": color,
                "fields": [
                    {
                        "name": "Mint",
                        "value": f"[{position.mint[:8]}...]({dex_url})",
                        "inline": True,
                    },
                    {
                        "name": "Exit Reason",
                        "value": f"`{position.exit_reason}`",
                        "inline": True,
                    },
                    {
                        "name": "Realized PnL",
                        "value": f"**{position.realized_pnl_sol:+.4f} SOL** ({roi_pct:+.1f}%)",
                        "inline": True,
                    },
                    {
                        "name": "Entry Size",
                        "value": f"{position.entry_sol_amount:.3f} SOL",
                        "inline": True,
                    },
                    {
                        "name": "Peak ROI Seen",
                        "value": f"+{position.current_roi_pct:.1f}%",
                        "inline": True,
                    },
                    {
                        "name": "Wallet",
                        "value": f"`{position.wallet_address[:8]}...`",
                        "inline": True,
                    },
                ],
                "footer": {"text": "rugbot cabal execution • paper/observe"},
                "timestamp": datetime.now(UTC).isoformat(),
            }
        ]
    }

    try:
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "rugbot/2.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=DISCORD_WEBHOOK_TIMEOUT_SECONDS):
            return True
    except Exception as exc:
        logger.warning("Failed sending Discord exit alert: %s", exc)
        return False


def send_telegram_exit_alert(
    token: str,
    chat_id: str | int,
    position: CabalActivePosition,
    *,
    token_symbol: str = "",
) -> bool:
    """Send position exit notification to Telegram."""
    if not token.strip() or not chat_id:
        return False

    is_profit = position.realized_pnl_sol > 0
    emoji = "🟢 <b>PROFIT EXIT</b>" if is_profit else "🔴 <b>ADVERSE / STOP EXIT</b>"
    roi_pct = (
        (position.realized_pnl_sol) / max(0.001, position.entry_sol_amount)
    ) * 100.0

    text = (
        f"{emoji} <b>Position Closed</b>\n"
        f"<b>Token:</b> {token_symbol or position.mint[:8]} (<code>{position.mint}</code>)\n"
        f"<b>Reason:</b> <code>{position.exit_reason}</code>\n"
        f"<b>Realized PnL:</b> <b>{position.realized_pnl_sol:+.4f} SOL</b> ({roi_pct:+.1f}%)\n"
        f"<b>Entry Size:</b> {position.entry_sol_amount:.3f} SOL\n"
        f"<b>Peak ROI:</b> +{position.current_roi_pct:.1f}%\n"
    )

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TELEGRAM_TIMEOUT_SECONDS):
            return True
    except Exception as exc:
        logger.warning("Failed sending Telegram exit alert: %s", exc)
        return False


@dataclass
class CabalPipeline:
    """Autonomous monitoring, filtering, alerting, and paper execution pipeline."""

    store: CabalStore = field(default_factory=CabalStore)
    signal_filter: SignalFilter = field(default_factory=SignalFilter)
    executor: CabalExecutor = field(default_factory=CabalExecutor)
    pump_client: PumpFunApiClient = field(default_factory=get_client)
    discord_webhook_url: str = field(
        default_factory=lambda: (
            os.environ.get("DISCORD_WEBHOOK_URL")
            or os.environ.get("DISCORD_ENTITY_WEBHOOK_URL", "")
        )
    )
    telegram_token: str = field(
        default_factory=lambda: os.environ.get("TELEGRAM_TOKEN", "")
    )
    telegram_chat_id: str = field(
        default_factory=lambda: os.environ.get("TELEGRAM_CHAT_ID", "")
    )
    sizing_config: PositionSizingConfig = field(default_factory=PositionSizingConfig)
    on_trade_evaluated: Any = None
    on_insider_dump: Any = None
    on_status_update: Any = None
    _wallet_to_cluster: dict[str, CabalCluster] = field(default_factory=dict)
    _seen_signatures: set[str] = field(default_factory=set)
    _last_seen_signatures: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Link CabalStore into CabalExecutor if not explicitly provided."""
        if getattr(self.executor, "store", None) is None:
            self.executor.store = self.store

    def reload_clusters(self, registry_path: Path | str | None = None) -> int:
        """Load tracked cabals from store and map wallets to clusters, cross-referencing WalletRegistry."""
        clusters = self.store.list_clusters()
        self._wallet_to_cluster.clear()
        for c in clusters:
            for w in c.wallets:
                self._wallet_to_cluster[w] = c

        # Cross-reference with enabled wallets in WalletRegistry
        reg_path = Path(registry_path or ".state/copytrade/registry.sqlite3")
        if reg_path.exists():
            try:
                from rugbot.analysis.wallet_registry import (  # noqa: PLC0415
                    WalletRegistry,
                )

                registry = WalletRegistry(reg_path)
                for reg_w in registry.list(enabled_only=True):
                    if reg_w.wallet not in self._wallet_to_cluster:
                        synthetic = CabalCluster(
                            cluster_id=f"reg-{reg_w.wallet[:8]}",
                            funder=reg_w.wallet,
                            wallets=frozenset([reg_w.wallet]),
                            winner_tokens=frozenset(),
                            token_count=1,
                            median_ath=2.0,
                            mean_ath=2.0,
                            winrate_2x=50.0,
                            winrate_5x=25.0,
                            avg_time_to_peak_sec=60.0,
                            typical_buy_sol=reg_w.quote_sol or 0.10,
                            tokens=(),
                            discovered_at=reg_w.added_at
                            or datetime.now(UTC).isoformat(),
                        )
                        self._wallet_to_cluster[reg_w.wallet] = synthetic
                registry.close()
            except Exception as exc:
                logger.debug("Could not cross-reference with WalletRegistry: %s", exc)

        logger.info(
            "Loaded %d cabal clusters with %d tracked wallets",
            len(clusters),
            len(self._wallet_to_cluster),
        )
        return len(self._wallet_to_cluster)

    @property
    def tracked_wallets(self) -> frozenset[str]:
        """Return the set of all monitored cabal wallets."""
        return frozenset(self._wallet_to_cluster.keys())

    @property
    def session_stats(self) -> dict[str, Any]:
        """Return real-time financial and trade statistics for the paper session."""
        return self.executor.get_session_stats()

    async def handle_detected_trade(
        self,
        mint: str,
        buyer_wallet: str,
        amount_sol: float,
        *,
        token_created_time: float | None = None,
        market_cap_usd: float | None = None,
        token_symbol: str = "",
        token_name: str = "",
        initial_price_sol: float = 0.00003,
    ) -> tuple[SignalDecision, CabalActivePosition | None]:
        """Process one incoming buyer transaction through signal filter and execution."""
        cluster = self._wallet_to_cluster.get(buyer_wallet)

        is_banned = False
        boost_mode = ""
        # Resolve token info if not passed
        if token_created_time is None or market_cap_usd is None:
            try:
                token_meta = self.pump_client.fetch_token(mint)
                if token_meta:
                    created_ms = float(token_meta.get("created_timestamp") or 0.0)
                    token_created_time = created_ms / 1000.0 if created_ms > 0 else 0.0
                    market_cap_usd = float(
                        token_meta.get("usd_market_cap")
                        or token_meta.get("market_cap")
                        or 0.0
                    )
                    token_symbol = token_symbol or str(token_meta.get("symbol") or "")
                    token_name = token_name or str(token_meta.get("name") or "")
                    is_banned = bool(token_meta.get("is_banned"))
                    boost_mode = str(token_meta.get("boost_mode") or "")
            except Exception as exc:
                logger.debug("Failed fetching token meta for %s: %s", mint, exc)

        created_ts = token_created_time or 0.0
        mcap = market_cap_usd or 0.0

        # Step 1: Filter & Confluence scoring
        decision = self.signal_filter.evaluate_signal(
            mint=mint,
            wallet=buyer_wallet,
            amount_sol=amount_sol,
            token_created_time=created_ts,
            current_liquidity_usd=mcap,
            cabal_cluster=cluster,
            is_banned=is_banned,
            boost_mode=boost_mode,
        )

        # Step 2: Push Alerts (Discord / Telegram)
        if self.discord_webhook_url:
            send_discord_cabal_alert(
                self.discord_webhook_url,
                decision,
                cluster,
                token_name=token_name,
                token_symbol=token_symbol,
                market_cap_usd=mcap,
            )

        if self.telegram_token and self.telegram_chat_id:
            send_telegram_cabal_alert(
                self.telegram_token,
                self.telegram_chat_id,
                decision,
                cluster,
                token_symbol=token_symbol,
            )

        # Step 3: Paper Execution on BUY
        position: CabalActivePosition | None = None
        if decision.action == "BUY":
            cluster_id = cluster.cluster_id if cluster else "cabal-adhoc"
            entry_amount_sol = self.sizing_config.calculate_entry_size(
                target_buy_sol=amount_sol,
                available_balance_sol=self.executor.cash_balance_sol,
            )
            if entry_amount_sol > self.executor.cash_balance_sol:
                logger.warning(
                    "Insufficient paper balance (%.4f SOL) for entry %.4f SOL",
                    self.executor.cash_balance_sol,
                    entry_amount_sol,
                )
                return decision, None

            position, _ = await self.executor.enter_position(
                mint=mint,
                cabal_cluster_id=cluster_id,
                amount_sol=entry_amount_sol,
                initial_price_sol=initial_price_sol,
            )
            logger.info(
                "Entered paper copytrade for %s via wallet %s (position %s, entry %.4f SOL, cash left %.4f SOL)",
                mint,
                position.wallet_address,
                position.position_id,
                entry_amount_sol,
                self.executor.cash_balance_sol,
            )

        return decision, position

    async def handle_stream_payload(
        self, payload: dict[str, Any]
    ) -> tuple[SignalDecision | None, CabalActivePosition | None]:
        """Handle parsed trade payload from PumpPortal or Helius RPC."""
        tx_type = str(payload.get("txType") or "").lower()
        mint = str(payload.get("mint") or "")
        trader = str(payload.get("traderPublicKey") or "")
        sol_amount = float(payload.get("solAmount") or 0.0)
        sig = str(payload.get("signature") or "")

        if not mint or not trader:
            return None, None

        if sig:
            self._seen_signatures.add(sig)

        if tx_type == "buy":
            token_amount = float(payload.get("tokenAmount") or 0.0)
            initial_price_sol = (
                (sol_amount / token_amount) if token_amount > 0 else 0.00003
            )
            decision, position = await self.handle_detected_trade(
                mint=mint,
                buyer_wallet=trader,
                amount_sol=sol_amount,
                initial_price_sol=initial_price_sol,
            )
            if self.on_trade_evaluated:
                res = self.on_trade_evaluated(decision, position, payload)
                if asyncio.iscoroutine(res):
                    await res
            return decision, position

        if tx_type == "sell":
            for pos in self.executor.active_positions:
                if pos.mint == mint and not pos.is_closed:
                    logger.warning(
                        "Cabal insider %s selling %s; triggering adverse exit for %s",
                        trader,
                        mint,
                        pos.position_id,
                    )
                    token_amount = float(payload.get("tokenAmount") or 0.0)
                    sell_price_sol = (
                        (sol_amount / token_amount)
                        if token_amount > 0
                        else pos.high_price_seen * 0.50
                    )
                    closed_pos, _ = await self.executor.exit_position(
                        pos.position_id,
                        current_price_sol=sell_price_sol,
                        reason="insider_dump_detected",
                    )
                    if self.discord_webhook_url:
                        send_discord_exit_alert(self.discord_webhook_url, closed_pos)
                    if self.telegram_token and self.telegram_chat_id:
                        send_telegram_exit_alert(
                            self.telegram_token, self.telegram_chat_id, closed_pos
                        )
                    if self.on_insider_dump:
                        dump_res = self.on_insider_dump(trader, mint, closed_pos)
                        if asyncio.iscoroutine(dump_res):
                            await dump_res
            return None, None

        return None, None

    async def poll_onchain_trades(
        self,
        interval_seconds: float = 3.0,
        stop_event: asyncio.Event | None = None,
        endpoints: Any = None,
    ) -> None:
        """Continuously poll Solana RPC for new transactions from tracked cabal wallets."""
        resolved_endpoints = endpoints or resolve_rpc_endpoints()
        wallets = list(self.tracked_wallets)
        if not wallets:
            return

        # 1. Establish initial baseline signature for each wallet to avoid replaying history
        for w in wallets:
            try:
                sigs = _rpc_call(
                    "getSignaturesForAddress",
                    [w, {"limit": 1}],
                    endpoints=resolved_endpoints,
                    transport=None,
                )
                if isinstance(sigs, list) and sigs:
                    top_sig = str(sigs[0].get("signature") or "")
                    if top_sig:
                        self._last_seen_signatures[w] = top_sig
                        self._seen_signatures.add(top_sig)
            except Exception as exc:
                logger.debug(
                    "Failed initializing signature baseline for %s: %s", w, exc
                )

        if self.on_status_update:
            msg = f"Helius RPC Poller baseline established for {len(self._last_seen_signatures)} wallets."
            res = self.on_status_update("info", msg)
            if asyncio.iscoroutine(res):
                await res

        # 2. Main polling loop
        while stop_event is None or not stop_event.is_set():
            for w in list(self.tracked_wallets):
                if stop_event is not None and stop_event.is_set():
                    break
                try:
                    sigs = _rpc_call(
                        "getSignaturesForAddress",
                        [w, {"limit": 3}],
                        endpoints=resolved_endpoints,
                        transport=None,
                    )
                    if not isinstance(sigs, list) or not sigs:
                        continue

                    new_sigs: list[dict[str, Any]] = []
                    last_sig = self._last_seen_signatures.get(w)
                    for s in sigs:
                        sig_hash = str(s.get("signature") or "")
                        if not sig_hash or sig_hash in self._seen_signatures:
                            continue
                        if last_sig and sig_hash == last_sig:
                            break
                        new_sigs.append(s)

                    for s in reversed(new_sigs):
                        sig_hash = str(s.get("signature") or "")
                        self._seen_signatures.add(sig_hash)
                        if len(self._seen_signatures) > MAX_SEEN_SIGNATURES:
                            self._seen_signatures = set(
                                list(self._seen_signatures)[-PRUNE_SEEN_SIGNATURES:]
                            )
                        self._last_seen_signatures[w] = sig_hash

                        if s.get("err") is not None:
                            continue

                        tx = _rpc_call(
                            "getTransaction",
                            [
                                sig_hash,
                                {
                                    "encoding": "jsonParsed",
                                    "maxSupportedTransactionVersion": 1,
                                },
                            ],
                            endpoints=resolved_endpoints,
                            transport=None,
                        )
                        if not isinstance(tx, dict):
                            continue

                        meta = tx.get("meta", {})
                        if meta.get("err") is not None:
                            continue

                        keys = (
                            tx.get("transaction", {})
                            .get("message", {})
                            .get("accountKeys", [])
                        )
                        sol_delta = 0.0
                        for i, k in enumerate(keys):
                            pub = k.get("pubkey") if isinstance(k, dict) else k
                            if (
                                pub == w
                                and meta.get("preBalances")
                                and meta.get("postBalances")
                            ):
                                pre = meta["preBalances"][i] / 1e9
                                post = meta["postBalances"][i] / 1e9
                                sol_delta = post - pre
                                break

                        post_tokens = meta.get("postTokenBalances", [])
                        tokens = [
                            t["mint"]
                            for t in post_tokens
                            if t.get("mint")
                            and t.get("mint")
                            not in (
                                "So11111111111111111111111111111111111111112",
                                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                                "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
                            )
                        ]
                        if not tokens:
                            tokens = [
                                t["mint"]
                                for t in post_tokens
                                if t.get("mint")
                                and t.get("mint")
                                != "So11111111111111111111111111111111111111112"
                            ]
                        if not tokens:
                            continue

                        target_mint = tokens[0]
                        tx_type = (
                            "buy"
                            if sol_delta < -SOL_DELTA_THRESHOLD_SOL
                            else ("sell" if sol_delta > SOL_DELTA_THRESHOLD_SOL else "")
                        )
                        if not tx_type:
                            continue

                        amount_sol = abs(sol_delta)
                        payload = {
                            "txType": tx_type,
                            "mint": target_mint,
                            "traderPublicKey": w,
                            "solAmount": amount_sol,
                            "signature": sig_hash,
                            "source": "helius_rpc",
                        }
                        await self.handle_stream_payload(payload)
                except Exception as exc:
                    logger.debug("RPC poller error for wallet %s: %s", w, exc)

            try:
                await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                break

    async def stream_live(
        self,
        seconds: int = 0,
        stream_url: str = "wss://pumpportal.fun/api/data",
        stop_event: asyncio.Event | None = None,
        poll_interval_seconds: float = 3.0,
        enable_pumpportal: bool = True,
    ) -> None:
        """Stream live on-chain trades for tracked cabal wallets via Helius RPC and WebSocket."""
        wallets = list(self.tracked_wallets)
        if not wallets:
            logger.warning("No cabal wallets tracked; stream cannot start.")
            return

        internal_stop = stop_event or asyncio.Event()

        timeout_task: asyncio.Task[None] | None = None
        if seconds > 0:

            async def _timeout() -> None:
                await asyncio.sleep(seconds)
                internal_stop.set()

            timeout_task = asyncio.create_task(_timeout())

        tasks: list[asyncio.Task[Any]] = []

        # 1. Helius RPC Poller (Primary robust on-chain feed)
        rpc_task = asyncio.create_task(
            self.poll_onchain_trades(
                interval_seconds=poll_interval_seconds,
                stop_event=internal_stop,
            )
        )
        tasks.append(rpc_task)

        # 2. PumpPortal WebSocket (Secondary feed, optional)
        if enable_pumpportal:
            stream = PumpPortalStream(ws_url=stream_url)

            async def _trade_handler(payload: dict[str, Any]) -> None:
                await self.handle_stream_payload(payload)

            async def _status_handler(level: str, message: str) -> None:
                if self.on_status_update:
                    res = self.on_status_update(level, message)
                    if asyncio.iscoroutine(res):
                        await res

            async def _pumpportal_runner() -> None:
                try:
                    await stream.listen_account_trades(
                        wallets=wallets,
                        callback=_trade_handler,
                        on_status=_status_handler,
                        stop_event=internal_stop,
                    )
                except Exception as exc:
                    logger.debug("PumpPortal stream stopped: %s", exc)

            tasks.append(asyncio.create_task(_pumpportal_runner()))

        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if timeout_task is not None and not timeout_task.done():
                timeout_task.cancel()
            for t in tasks:
                if not t.done():
                    t.cancel()


__all__ = [
    "CabalPipeline",
    "PositionSizingConfig",
    "SizingMode",
    "send_discord_cabal_alert",
    "send_discord_exit_alert",
    "send_telegram_cabal_alert",
    "send_telegram_exit_alert",
]
