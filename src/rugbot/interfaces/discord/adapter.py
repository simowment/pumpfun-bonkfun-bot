"""Discord UI adapter driving the shared RugbotCore facade with modern slash commands,

Rich Embeds, auto-CA quick buy buttons in chat feeds, and full F-Project playbook parity.
"""

# ruff: noqa: C901, PLR0915, BLE001, TRY003

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence


import discord
from discord import app_commands
from discord.ext import commands

from rugbot.application.commands import BotCommand
from rugbot.interfaces.base import BaseAdapter
from rugbot.runtime.app import RugbotApp, build_ui_runtime
from rugbot.runtime.config import (
    resolve_dotenv,
    resolve_state_dir,
)
from rugbot.runtime.workers.position_exit_worker import (
    MANUAL_FULL_EXIT_PPM,
    MANUAL_HALF_EXIT_PPM,
)
from rugbot.tracker.events import (
    DecisionEvent,
    LaunchDetected,
    TrackerEvent,
    WalletFunded,
)
from rugbot.tracker.models import (
    LAMPORTS_PER_SOL,
)
from rugbot.tracker.screener import ScreenerCandidate, ScreenerCandidateStatus

logger = logging.getLogger("rugbot.interfaces.discord")

# Solana Base58 Address Pattern (32 to 44 Base58 characters)
SOLANA_ADDRESS_REGEX = re.compile(r"\b([1-9A-HJ-NP-Za-km-z]{32,44})\b")
MIN_BASE58_LEN = 32
MAX_BASE58_LEN = 44

# Preset Buy Amounts in SOL
PRESET_P1_SOL = 0.010
PRESET_P2_SOL = 0.025
PRESET_P3_SOL = 0.050

P1_OPTIONS: Final[tuple[float, ...]] = (0.010, 0.020, 0.050, 0.100)
P2_OPTIONS: Final[tuple[float, ...]] = (0.025, 0.050, 0.100, 0.250)
P3_OPTIONS: Final[tuple[float, ...]] = (0.050, 0.100, 0.250, 0.500)
SLIPPAGE_OPTIONS: Final[tuple[int, ...]] = (500, 1000, 1500, 2500, 5000)  # in bps
TIP_OPTIONS: Final[tuple[float, ...]] = (0.001, 0.003, 0.005, 0.010)

# Colors for Discord Embeds
COLOR_SUCCESS = discord.Color.from_rgb(0, 255, 163)  # Solana Green
COLOR_WARNING = discord.Color.from_rgb(255, 184, 0)  # Neon Yellow/Orange
COLOR_DANGER = discord.Color.from_rgb(255, 75, 75)  # Hot Red
COLOR_INFO = discord.Color.from_rgb(153, 69, 255)  # Solana Purple
COLOR_NEUTRAL = discord.Color.from_rgb(30, 34, 45)  # Dark Slate

# No polling timer: cross-process delivery is startup drain + on-demand drain.
# See DiscordAdapter.drain_pending_discord_alerts() and connect() docstring.


class DiscordConfigError(RuntimeError):
    """Raised when required Discord environment configuration is missing."""


# ============================================================================
# INTERACTIVE UI VIEWS & BUTTONS (Telegram / Cockpit Parity)
# ============================================================================


class CockpitHomeView(discord.ui.View):
    """Main Telegram-style home dashboard view with rich interactive navigation."""

    def __init__(self, adapter: DiscordAdapter, *, timeout: float = 600.0) -> None:
        super().__init__(timeout=timeout)
        self.adapter = adapter

    @discord.ui.button(
        label="Positions", style=discord.ButtonStyle.primary, emoji="📊", row=0
    )
    async def btn_positions(
        self, interaction: discord.Interaction, _btn: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        embed, views = self.adapter.build_positions_embed()
        await interaction.followup.send(embed=embed, ephemeral=True)
        for v in views[:3]:
            await interaction.followup.send(view=v, ephemeral=True)

    @discord.ui.button(
        label="Targets", style=discord.ButtonStyle.secondary, emoji="🎯", row=0
    )
    async def btn_targets(
        self, interaction: discord.Interaction, _btn: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        embed = self.adapter.build_targets_embed()
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Settings", style=discord.ButtonStyle.secondary, emoji="⚙️", row=0
    )
    async def btn_settings(
        self, interaction: discord.Interaction, _btn: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        embed = self.adapter.build_settings_embed()
        view = SettingsView(self.adapter)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(
        label="Wallet", style=discord.ButtonStyle.secondary, emoji="💼", row=1
    )
    async def btn_wallet(
        self, interaction: discord.Interaction, _btn: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        embed = self.adapter.build_wallet_embed()
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Screener", style=discord.ButtonStyle.secondary, emoji="⚡", row=1
    )
    async def btn_screener(
        self, interaction: discord.Interaction, _btn: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        embed = self.adapter.build_screener_embed()
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Kill Switch", style=discord.ButtonStyle.danger, emoji="🚨", row=1
    )
    async def btn_kill(
        self, interaction: discord.Interaction, _btn: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        res = self.adapter.core.toggle_kill_switch()
        embed = discord.Embed(
            title="🚨 Kill Switch Toggled",
            description=res.message,
            color=COLOR_DANGER if "ENABLED" in res.message.upper() else COLOR_SUCCESS,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄", row=2
    )
    async def btn_refresh(
        self, interaction: discord.Interaction, _btn: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        embed = self.adapter.build_cockpit_embed()
        await interaction.edit_original_response(embed=embed, view=self)


def _cycle_option(
    current: float | int, options: tuple[float | int, ...]
) -> float | int:
    """Cycle to the next option in a tuple or default to the first."""
    return (
        options[(options.index(current) + 1) % len(options)]
        if current in options
        else options[0]
    )


class SettingsView(discord.ui.View):
    """Interactive Settings control panel for instant configuration in Discord."""

    def __init__(self, adapter: DiscordAdapter, *, timeout: float = 300.0) -> None:
        super().__init__(timeout=timeout)
        self.adapter = adapter
        self._sync_labels()

    def _sync_labels(self) -> None:
        self.btn_p1.label = f"P1: {self.adapter.preset_p1_sol:.3f} SOL"
        self.btn_p2.label = f"P2: {self.adapter.preset_p2_sol:.3f} SOL"
        self.btn_p3.label = f"P3: {self.adapter.preset_p3_sol:.3f} SOL"
        self.btn_slip.label = f"Slippage: {self.adapter.slippage_bps // 100}%"
        self.btn_tip.label = f"Tip: {self.adapter.jito_tip_sol:.3f} SOL"
        self.btn_route.label = (
            f"Routing: {'⚡ JITO' if self.adapter.routing_mode == 'jito' else '🌐 RPC'}"
        )

    async def _cycle_and_update(
        self,
        interaction: discord.Interaction,
        attr: str,
        options: tuple[float | int, ...],
    ) -> None:
        await interaction.response.defer()
        setattr(self.adapter, attr, _cycle_option(getattr(self.adapter, attr), options))
        self._sync_labels()
        embed = self.adapter.build_settings_embed()
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="P1: 0.010 SOL", style=discord.ButtonStyle.primary, row=0)
    async def btn_p1(
        self, interaction: discord.Interaction, _btn: discord.ui.Button
    ) -> None:
        await self._cycle_and_update(interaction, "preset_p1_sol", P1_OPTIONS)

    @discord.ui.button(label="P2: 0.025 SOL", style=discord.ButtonStyle.primary, row=0)
    async def btn_p2(
        self, interaction: discord.Interaction, _btn: discord.ui.Button
    ) -> None:
        await self._cycle_and_update(interaction, "preset_p2_sol", P2_OPTIONS)

    @discord.ui.button(label="P3: 0.050 SOL", style=discord.ButtonStyle.primary, row=0)
    async def btn_p3(
        self, interaction: discord.Interaction, _btn: discord.ui.Button
    ) -> None:
        await self._cycle_and_update(interaction, "preset_p3_sol", P3_OPTIONS)

    @discord.ui.button(
        label="Slippage: 10%", style=discord.ButtonStyle.secondary, row=1
    )
    async def btn_slip(
        self, interaction: discord.Interaction, _btn: discord.ui.Button
    ) -> None:
        await self._cycle_and_update(interaction, "slippage_bps", SLIPPAGE_OPTIONS)

    @discord.ui.button(
        label="Tip: 0.001 SOL", style=discord.ButtonStyle.secondary, row=1
    )
    async def btn_tip(
        self, interaction: discord.Interaction, _btn: discord.ui.Button
    ) -> None:
        await self._cycle_and_update(interaction, "jito_tip_sol", TIP_OPTIONS)

    @discord.ui.button(
        label="Routing: ⚡ JITO", style=discord.ButtonStyle.secondary, row=1
    )
    async def btn_route(
        self, interaction: discord.Interaction, _btn: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        self.adapter.routing_mode = (
            "rpc" if self.adapter.routing_mode == "jito" else "jito"
        )
        self._sync_labels()
        embed = self.adapter.build_settings_embed()
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(
        label="Back to Cockpit",
        style=discord.ButtonStyle.success,
        emoji="🏠",
        row=2,
    )
    async def btn_back(
        self, interaction: discord.Interaction, _btn: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        embed = self.adapter.build_cockpit_embed()
        view = CockpitHomeView(self.adapter)
        await interaction.edit_original_response(embed=embed, view=view)


class QuickBuyView(discord.ui.View):
    """Interactive action buttons attached to scanned tokens / detected CAs."""

    def __init__(  # noqa: PLR0913
        self,
        core: RugbotApp,
        mint_or_wallet: str,
        *,
        timeout: float = 300.0,
        preset_p1: float = PRESET_P1_SOL,
        preset_p2: float = PRESET_P2_SOL,
        preset_p3: float = PRESET_P3_SOL,
    ) -> None:
        super().__init__(timeout=timeout)
        self.core = core
        self.target = mint_or_wallet
        self.preset_p1 = preset_p1
        self.preset_p2 = preset_p2
        self.preset_p3 = preset_p3

        self.buy_p1.label = f"Buy P1 ({preset_p1} SOL)"
        self.buy_p2.label = f"Buy P2 ({preset_p2} SOL)"
        self.buy_p3.label = f"Buy P3 ({preset_p3} SOL)"

        self.add_item(
            discord.ui.Button(
                label="DexScreener",
                emoji="📈",
                url=f"https://dexscreener.com/solana/{mint_or_wallet}",
                row=1,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Solscan",
                emoji="🔍",
                url=f"https://solscan.io/account/{mint_or_wallet}",
                row=1,
            )
        )

    async def _execute_buy(
        self, interaction: discord.Interaction, amount: float
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        cand = self.core.screener.scan_and_evaluate(self.target)
        self.core.screener.accept_candidate(cand.creator_wallet, self.core.service)
        res = await self.core.execute_command(
            BotCommand(
                name="snipe",
                args=(cand.token_mint, str(amount)),
                source="discord_button",
            )
        )
        await interaction.followup.send(
            f"🎯 **Snipe Triggered ({amount} SOL)** on `{cand.token_mint[:8]}...`\n{res.message}",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Buy P1", style=discord.ButtonStyle.success, emoji="🟢", row=0
    )
    async def buy_p1(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self._execute_buy(interaction, self.preset_p1)

    @discord.ui.button(
        label="Buy P2", style=discord.ButtonStyle.success, emoji="🟢", row=0
    )
    async def buy_p2(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self._execute_buy(interaction, self.preset_p2)

    @discord.ui.button(
        label="Buy P3", style=discord.ButtonStyle.success, emoji="🟢", row=0
    )
    async def buy_p3(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self._execute_buy(interaction, self.preset_p3)

    @discord.ui.button(
        label="Enroll Target (Auto-TP)",
        style=discord.ButtonStyle.primary,
        emoji="🎯",
        row=1,
    )
    async def enroll_target(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        cand = self.core.screener.accept_candidate(self.target, self.core.service)
        if cand is None:
            await interaction.followup.send(
                "Target not enrolled: finalized repeat adverse-operator evidence "
                "is required.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"✅ **Enrolled Developer** `{cand.creator_wallet[:8]}...`\n"
            f"• Master Funder: `{cand.root_funder[:8]}...`\n"
            f"• Optimal Take-Profit: **{cand.optimal_tp_label}**\n"
            f"• Sniper Status: **ARMED (SIMULATED B0)**",
            ephemeral=True,
        )


class PositionActionView(discord.ui.View):
    """Action buttons attached to open positions (Exit 50%, Exit 100%, Recover Initials)."""

    def __init__(
        self, core: RugbotApp, market_id: str, *, timeout: float = 300.0
    ) -> None:
        super().__init__(timeout=timeout)
        self.core = core
        self.market_id = market_id

    async def _execute_exit(
        self, interaction: discord.Interaction, title: str, ppm: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        res = await self.core.sell(self.market_id, ppm)
        await interaction.followup.send(f"{title}: {res.message}", ephemeral=True)

    @discord.ui.button(
        label="Sell Initials", style=discord.ButtonStyle.secondary, emoji="💸", row=0
    )
    async def sell_initials(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self._execute_exit(
            interaction, "💸 **Sell Initials**", MANUAL_HALF_EXIT_PPM
        )

    @discord.ui.button(
        label="Sell 50%", style=discord.ButtonStyle.primary, emoji="💰", row=0
    )
    async def sell_half(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self._execute_exit(interaction, "💰 **Sell 50%**", MANUAL_HALF_EXIT_PPM)

    @discord.ui.button(
        label="Exit 100%", style=discord.ButtonStyle.danger, emoji="🚨", row=0
    )
    async def exit_all(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self._execute_exit(interaction, "🚨 **Full Exit**", MANUAL_FULL_EXIT_PPM)


# ============================================================================
# DISCORD BOT ADAPTER & COMMAND REGISTRATION
# ============================================================================


class DiscordAdapter(BaseAdapter):
    """Production Discord Bot Adapter driving RugbotApp with Slash Commands and Rich Views."""

    def __init__(  # noqa: PLR0913
        self,
        core: RugbotApp,
        *,
        token: str,
        channel_id: int,
        allowed_user_ids: tuple[int, ...] = (),
        command_prefix: str = "!",
        preset_p1_sol: float = PRESET_P1_SOL,
        preset_p2_sol: float = PRESET_P2_SOL,
        preset_p3_sol: float = PRESET_P3_SOL,
        slippage_bps: int = 1000,
        jito_tip_sol: float = 0.001,
        routing_mode: str = "jito",
    ) -> None:

        self._core = core
        self._token = token
        self._channel_id = channel_id
        self._allowed_user_ids = allowed_user_ids
        self._command_prefix = command_prefix
        self.preset_p1_sol = preset_p1_sol
        self.preset_p2_sol = preset_p2_sol
        self.preset_p3_sol = preset_p3_sol
        self.slippage_bps = slippage_bps
        self.jito_tip_sol = jito_tip_sol
        self.routing_mode = routing_mode

        # Build discord.ext.commands.Bot instance with slash command tree
        intents = discord.Intents.default()
        intents.message_content = True
        self.bot = commands.Bot(
            command_prefix=command_prefix,
            intents=intents,
            help_command=None,
        )
        self._subscribed = False
        self._seen_event_keys: set[tuple[object, ...]] = set()

        # Register Bot Events & Commands
        self._register_events()
        self._register_slash_commands()
        self._register_prefix_commands()

    @property
    def core(self) -> RugbotApp:
        """Return the shared underlying RugbotApp."""
        return self._core

    @property
    def channel_id(self) -> int:
        """Currently active alert channel ID."""
        return self._channel_id

    @channel_id.setter
    def channel_id(self, new_id: int) -> None:
        self._channel_id = new_id
        os.environ["DISCORD_CHANNEL_ID"] = str(new_id)

    def set_channel(self, new_id: int) -> None:
        """Set active notification channel dynamically at runtime."""
        self.channel_id = new_id

    def build_cockpit_embed(self) -> discord.Embed:
        """Build a high-density home dashboard embed summarizing live state."""
        stats = self._core.repository.get_summary_stats()
        targets = self._core.targets()
        positions = self._core.positions()
        snapshot = self._core.snapshot()

        status_str = "🟢 RUNNING"
        if snapshot and snapshot.kill_switch_active:
            status_str = "🚨 KILL SWITCH ACTIVE"

        embed = discord.Embed(
            title="🎯 RUGBOT TRADING COCKPIT",
            description=(
                f"Status: **{status_str}** | Alerts: <#{self._channel_id}>\n"
                f"Routing: **{'⚡ JITO BUNDLE' if self.routing_mode == 'jito' else '🌐 RPC'}** | "
                f"Slippage: **{self.slippage_bps // 100}%**"
            ),
            color=COLOR_SUCCESS
            if not (snapshot and snapshot.kill_switch_active)
            else COLOR_DANGER,
        )
        embed.add_field(
            name="📊 Open Positions",
            value=f"**{len(positions)}** active",
            inline=True,
        )
        embed.add_field(
            name="🎯 Tracked Targets",
            value=f"**{len(targets)}** armed",
            inline=True,
        )
        embed.add_field(
            name="👑 Master Funders",
            value=f"**{stats['funders_count']}** clusters",
            inline=True,
        )
        embed.add_field(
            name="⚡ Buy Presets",
            value=(
                f"• **P1**: `{self.preset_p1_sol:.3f} SOL`\n"
                f"• **P2**: `{self.preset_p2_sol:.3f} SOL`\n"
                f"• **P3**: `{self.preset_p3_sol:.3f} SOL`"
            ),
            inline=True,
        )
        embed.add_field(
            name="🛡️ Execution Rules",
            value=(
                f"• **Jito Tip**: `{self.jito_tip_sol:.3f} SOL`\n"
                f"• **Slippage**: `{self.slippage_bps // 100}%`\n"
                f"• **Mode**: `PAPER / SIMULATED B0`"
            ),
            inline=True,
        )
        embed.set_footer(
            text="Rugbot Solana Precision Terminal • Press buttons below to navigate"
        )
        return embed

    def build_settings_embed(self) -> discord.Embed:
        """Build an interactive settings overview embed."""
        embed = discord.Embed(
            title="⚙️ RUGBOT SETTINGS & EXECUTION CONFIG",
            description="Click buttons below to cycle and adjust buy sizes, slippage, and routing in real-time.",
            color=COLOR_INFO,
        )
        embed.add_field(
            name="🟢 Buy Size Presets",
            value=(
                f"• **Preset 1 (P1)**: `{self.preset_p1_sol:.3f} SOL`\n"
                f"• **Preset 2 (P2)**: `{self.preset_p2_sol:.3f} SOL`\n"
                f"• **Preset 3 (P3)**: `{self.preset_p3_sol:.3f} SOL`"
            ),
            inline=False,
        )
        embed.add_field(
            name="⚡ Anti-MEV & Routing",
            value=(
                f"• **Routing Engine**: `{'⚡ JITO Bundle (MEV-Protected)' if self.routing_mode == 'jito' else '🌐 Standard RPC'}`\n"
                f"• **Jito Tip**: `{self.jito_tip_sol:.3f} SOL`\n"
                f"• **Slippage Tolerance**: `{self.slippage_bps // 100}%`"
            ),
            inline=False,
        )
        embed.add_field(
            name="📌 Alert Feed Channel",
            value=f"• <#{self._channel_id}> (`{self._channel_id}`)\nUse `/channel <#new-channel>` to change it.",
            inline=False,
        )
        embed.set_footer(
            text="Settings apply dynamically to all quick buys and automated snipes"
        )
        return embed

    def build_wallet_embed(self) -> discord.Embed:
        """Build wallet and portfolio overview embed."""
        embed = discord.Embed(
            title="💼 TRADING WALLET & BALANCES",
            color=COLOR_SUCCESS,
        )
        embed.add_field(
            name="🌐 Network",
            value="`Solana Mainnet-Beta`",
            inline=True,
        )
        embed.add_field(
            name="⚡ RPC Provider",
            value="`Helius Mainnet High-Speed RPC`",
            inline=True,
        )
        embed.add_field(
            name="💰 SOL Balance",
            value="**Paper Trading Mode** (Virtual 10.00 SOL)",
            inline=False,
        )
        positions = self._core.positions()
        pos_str = (
            f"**{len(positions)}** active positions"
            if positions
            else "No open token positions."
        )
        embed.add_field(
            name="📦 Token Portfolio",
            value=pos_str,
            inline=False,
        )
        embed.set_footer(text="Rugbot Paper & Live Execution Engine")
        return embed

    def build_positions_embed(
        self,
    ) -> tuple[discord.Embed, list[PositionActionView]]:
        """Build open positions list embed and attached action views."""
        positions = self._core.positions()
        if not positions:
            embed = discord.Embed(
                title="📊 Open Positions",
                description="📭 No open positions. Use `/buy` or click quick-buy buttons in the alert feed.",
                color=COLOR_INFO,
            )
            return embed, []

        embed = discord.Embed(
            title=f"📊 Open Positions ({len(positions)} Active)",
            color=COLOR_INFO,
        )
        views = []
        for pos in positions:
            pnl_pct = pos.pnl_ppm / 10000.0
            pnl_color = "🟢" if pnl_pct >= 0 else "🔴"
            embed.add_field(
                name=f"{pnl_color} `{pos.market_id[:8]}...` ({pnl_pct:+.1f}%)",
                value=(
                    f"Held: `{pos.base_amount_raw}` | "
                    f"Cost: `{pos.entry_quote_lamports / LAMPORTS_PER_SOL:.4f} SOL`\n"
                    f"Value: `{pos.current_quote_lamports / LAMPORTS_PER_SOL:.4f} SOL`"
                ),
                inline=False,
            )
            views.append(PositionActionView(self._core, pos.market_id))
        return embed, views

    def build_targets_embed(self) -> discord.Embed:
        """Build tracked sniper targets and armed policies embed."""
        targets = self._core.targets()
        if not targets:
            return discord.Embed(
                title="🎯 Tracked Targets",
                description="📭 No targets currently tracked. Use `/watch <wallet>` or `/scan <address>` to enroll targets.",
                color=COLOR_INFO,
            )
        embed = discord.Embed(
            title=f"🎯 Active Tracked Targets ({len(targets)} Armed)",
            color=COLOR_INFO,
        )
        for t in targets[:15]:
            p = t.policy
            mode_str = p.execution_mode.value.upper() if p else "SIMULATED"
            tp_str = f"+{(p.take_profit_pnl_ppm / 10000.0):.0f}%" if p else "OPTIMAL"
            size_str = (
                f"{(p.quote_size_lamports / LAMPORTS_PER_SOL):.3f} SOL"
                if p
                else f"{self.preset_p1_sol} SOL"
            )
            embed.add_field(
                name=f"📍 {t.label or 'Tracked Target'} (`{t.address[:8]}...{t.address[-4:]}`)",
                value=f"Mode: **{mode_str}** | Size: **{size_str}** | TP: **{tp_str}**",
                inline=False,
            )
        return embed

    def build_screener_embed(self) -> discord.Embed:
        """Build candidate review queue embed."""
        candidates = self._core.screener.get_candidates()
        if not candidates:
            return discord.Embed(
                title="⚡ Candidate Review Queue",
                description="📭 Queue is empty. Stream is actively listening on PumpPortal for new bonding curve creations.",
                color=COLOR_INFO,
            )
        embed = discord.Embed(
            title=f"⚡ Pump.fun Candidate Queue ({len(candidates)} Detected)",
            color=COLOR_INFO,
        )
        for c in candidates[:8]:
            badge = "🟢" if c.is_bible_qualified else "🟡"
            embed.add_field(
                name=f"{badge} {c.token_symbol} (`{c.creator_wallet[:6]}...`)",
                value=(
                    f"Launches: **{c.cluster_token_count}** | Winrate: **{c.winrate_pct:.0f}%** | "
                    f"Optimal TP: **{c.optimal_tp_label}**\nNet EV: `{c.optimal_net_ev_sol:+.4f} SOL`"
                ),
                inline=False,
            )
        return embed

    # ------------------------------------------------------------------------
    # LIFECYCLE
    # ------------------------------------------------------------------------

    async def connect(self) -> None:
        """Start the Discord client, subscribe, drain durable outbox, then log in.

        Cross-process delivery: startup drain handles alerts produced while
        Discord was offline; same-process LaunchDetected events are delivered
        via EventBus without polling. No background polling loop.
        """
        self._core.subscribe(self._on_tracker_event)
        self._subscribed = True
        await self._drain_discord_outbox()
        await self.bot.start(self._token)

    async def disconnect(self) -> None:
        """Close the Discord client and release event subscriptions."""
        if self._subscribed:
            self._core.event_bus.unsubscribe("*", self._on_tracker_event)
            self._subscribed = False
        await self.bot.close()

    async def drain_pending_discord_alerts(self) -> int:
        """On-demand drain of undelivered 'discord' alerts without polling.

        Cross-process near-real-time requires an explicit call or a
        startup drain. For optional OS notification, watch the SQLite file
        (e.g. watchdog/inotify on ``state_dir/rugbot.db``) and call this
        helper on modification — do not poll on a timer.
        """
        before = len(self._core.repository.get_undelivered_alerts("discord"))
        await self._drain_discord_outbox()
        after = len(self._core.repository.get_undelivered_alerts("discord"))
        return max(0, before - after)

    async def send(self, event: TrackerEvent) -> None:
        """Render one tracker event as a rich embed and post it to the alerts channel."""
        channel = self.bot.get_channel(self._channel_id)
        if channel is None:
            raise RuntimeError(
                f"Alerts channel {self._channel_id} is unavailable; event not delivered"
            )
        embed, view = self._build_event_embed(event)
        if embed is not None:
            await channel.send(embed=embed, view=view)

    async def _on_tracker_event(self, event: TrackerEvent) -> None:
        """Bridge a renderable core tracker event to the alerts channel exactly once."""
        if isinstance(event, LaunchDetected):
            key = (
                "launch_detected",
                event.data.get("mint", ""),
                event.data.get("signature", ""),
            )
        elif isinstance(event, (DecisionEvent, WalletFunded)):
            key = (event.event_type, event.timestamp, event.wallet, event.root_funder)
        else:
            return
        if key in self._seen_event_keys:
            return
        await self.send(event)
        self._seen_event_keys.add(key)
        if isinstance(event, LaunchDetected):
            mint = str(event.data.get("mint", ""))
            if mint:
                with contextlib.suppress(Exception):
                    self._core.repository.mark_alerts_delivered("discord", (mint,))

    async def _drain_discord_outbox(self) -> None:
        """Drain undelivered 'discord' alerts and send embeds durably."""
        try:
            records = self._core.repository.get_undelivered_alerts("discord")
        except Exception:
            return
        for rec in records:
            try:
                launch = self._core.repository.get_launch(rec.mint)
            except Exception:  # noqa: S112
                continue
            if launch is None:
                continue
            key = ("launch_detected", launch.mint, launch.created_signature)
            if key in self._seen_event_keys:
                with contextlib.suppress(Exception):
                    self._core.repository.mark_alerts_delivered(
                        "discord", (launch.mint,)
                    )
                continue
            event = LaunchDetected(
                root_funder=launch.root_funder,
                wallet=launch.creator_wallet,
                timestamp=launch.created_at,
                data={
                    "symbol": launch.symbol,
                    "name": launch.name,
                    "mint": launch.mint,
                    "creator": launch.creator_wallet,
                    "root_funder": launch.root_funder,
                    "depth": launch.depth,
                    "slot": launch.created_slot,
                    "signature": launch.created_signature,
                },
            )
            embed, _view = self._build_event_embed(event)
            if embed is None:
                continue
            channel = self.bot.get_channel(self._channel_id)
            if channel is None:
                continue
            try:
                await channel.send(embed=embed, view=None)
            except Exception:  # noqa: S112
                continue
            with contextlib.suppress(Exception):
                self._core.repository.mark_alerts_delivered("discord", (launch.mint,))
            self._seen_event_keys.add(key)

    # ------------------------------------------------------------------------
    # EVENT HANDLERS
    # ------------------------------------------------------------------------

    def _register_events(self) -> None:
        """Hook standard discord.py bot lifecycle events."""

        @self.bot.event
        async def on_ready() -> None:
            logger.info(
                "Rugbot Discord Adapter logged in as %s (ID: %s)",
                self.bot.user,
                self.bot.user.id if self.bot.user else "0",
            )
            with contextlib.suppress(Exception):
                synced = await self.bot.tree.sync()
                logger.info("Synchronized %d Discord Slash Commands", len(synced))

        @self.bot.event
        async def on_message(message: discord.Message) -> None:
            if message.author.bot:
                return

            # Check authorized users if allowlist is configured
            if (
                self._allowed_user_ids
                and message.author.id not in self._allowed_user_ids
            ):
                return

            # 1. Process regular prefix commands first (!scan, !status, etc.)
            await self.bot.process_commands(message)

            # 2. Feed Auto-Detection: if message contains a Solana CA in chat, inject Quick Buy buttons!
            if not message.content.startswith(self._command_prefix):
                match = SOLANA_ADDRESS_REGEX.search(message.content)
                if match:
                    found_address = match.group(1)
                    # Ignore common words that match length but aren't base58 pubkeys
                    if len(found_address) >= MIN_BASE58_LEN and (
                        found_address.endswith("pump")
                        or len(found_address)
                        in {MIN_BASE58_LEN, MAX_BASE58_LEN - 1, MAX_BASE58_LEN}
                    ):
                        embed = discord.Embed(
                            title="⚡ Solana Token / Dev Address Detected",
                            description=f"Quick Buy injected directly into feed for `{found_address}`",
                            color=COLOR_INFO,
                        )
                        embed.add_field(
                            name="Target Address",
                            value=f"`{found_address}`",
                            inline=False,
                        )
                        view = QuickBuyView(
                            self._core,
                            found_address,
                            preset_p1=self.preset_p1_sol,
                            preset_p2=self.preset_p2_sol,
                            preset_p3=self.preset_p3_sol,
                        )
                        await message.channel.send(embed=embed, view=view)

    async def on_message(self, message: object) -> None:
        """Handle one inbound Discord message and dispatch it to the bot command pipeline."""
        if not isinstance(message, discord.Message):
            return
        if message.author.bot:
            return
        if self._allowed_user_ids and message.author.id not in self._allowed_user_ids:
            return
        await self.bot.process_commands(message)

    # ------------------------------------------------------------------------
    # EMBED FACTORIES
    # ------------------------------------------------------------------------

    def _build_scan_embed(
        self, candidate: ScreenerCandidate
    ) -> tuple[discord.Embed, discord.ui.View]:
        """Build a high-density Memecoin Bible audit embed for a scanned developer or token."""
        is_qualified = candidate.is_bible_qualified
        color = (
            COLOR_SUCCESS
            if is_qualified
            else (
                COLOR_WARNING
                if candidate.status == ScreenerCandidateStatus.PENDING_REVIEW
                else COLOR_DANGER
            )
        )
        status_emoji = "🟢 QUALIFIED" if is_qualified else "🟡 PENDING REVIEW"

        embed = discord.Embed(
            title=f"🔍 On-Chain Audit · {candidate.token_symbol} ({status_emoji})",
            description=f"**{candidate.token_name}**\n`{candidate.token_mint}`",
            color=color,
        )

        embed.add_field(
            name="👨‍💻 Creator Dev",
            value=f"`{candidate.creator_wallet[:8]}...{candidate.creator_wallet[-6:]}`",
            inline=True,
        )
        embed.add_field(
            name="👑 Master Funder",
            value=f"`{candidate.root_funder[:8]}...{candidate.root_funder[-6:]}`",
            inline=True,
        )
        embed.add_field(
            name="📊 Cluster Tokens",
            value=f"**{candidate.cluster_token_count}** launches",
            inline=True,
        )

        embed.add_field(
            name="🎯 Winrate", value=f"**{candidate.winrate_pct:.1f}%**", inline=True
        )
        embed.add_field(
            name="📈 Optimal TP",
            value=f"**{candidate.optimal_tp_label}** (+{((candidate.optimal_tp_multiplier - 1.0) * 100):.0f}%)",
            inline=True,
        )
        embed.add_field(
            name="💰 Net EV / Trade",
            value=f"**{candidate.optimal_net_ev_sol:+.5f} SOL**",
            inline=True,
        )

        embed.add_field(
            name="📜 Memecoin Bible Verdict",
            value=f"*{candidate.qualification_reason}*",
            inline=False,
        )
        embed.set_footer(text="Rugbot v2.0 · Memecoin Bible Execution Engine")

        view = QuickBuyView(
            self._core,
            candidate.creator_wallet,
            preset_p1=self.preset_p1_sol,
            preset_p2=self.preset_p2_sol,
            preset_p3=self.preset_p3_sol,
        )
        return embed, view

    def _build_event_embed(
        self, event: TrackerEvent
    ) -> tuple[discord.Embed | None, discord.ui.View | None]:
        """Convert a live tracker event into a rich Discord alert embed."""
        if isinstance(event, LaunchDetected):
            symbol = event.data.get("symbol", "TOKEN")
            name = event.data.get("name", "New Launch")
            mint = event.data.get("mint", "")
            creator = event.data.get("creator", "")

            embed = discord.Embed(
                title=f"🚀 Pump.fun Token Launch · {symbol}",
                description=f"**{name}**\nMint: `{mint}`",
                color=COLOR_SUCCESS,
            )
            embed.add_field(
                name="Creator Dev", value=f"`{creator[:8]}...`", inline=True
            )
            embed.add_field(
                name="DexScreener",
                value=f"[View on DexScreener](https://dexscreener.com/solana/{mint})",
                inline=True,
            )
            embed.add_field(
                name="Solscan",
                value=f"[View on Solscan](https://solscan.io/account/{mint})",
                inline=True,
            )
            if root_funder := event.data.get("root_funder"):
                embed.add_field(
                    name="Root Funder",
                    value=f"`{root_funder[:8]}...`",
                    inline=True,
                )
            if depth := event.data.get("depth"):
                embed.add_field(name="Depth", value=f"`{depth}`", inline=True)
            if slot := event.data.get("slot"):
                embed.add_field(name="Slot", value=f"`{slot}`", inline=True)
            if signature := event.data.get("signature"):
                embed.add_field(
                    name="Signature",
                    value=f"`{signature[:16]}...`",
                    inline=False,
                )
            embed.set_footer(text="Finalized Pump.fun RPC Evidence · Read-only alert")
            return embed, None

        if isinstance(event, DecisionEvent):
            color = (
                COLOR_SUCCESS if "EXEC" in event.event_type.upper() else COLOR_WARNING
            )
            embed = discord.Embed(
                title=f"🎯 Decision Event · {event.event_type}",
                description=f"**{event.reason}**",
                color=color,
            )
            embed.set_footer(text="Sniper Decision Rail")
            return embed, None

        if isinstance(event, WalletFunded):
            amount_sol = float(event.data.get("amount_lamports", 0)) / LAMPORTS_PER_SOL
            embed = discord.Embed(
                title="💳 Wallet Funded Event",
                description=f"Wallet `{event.wallet[:8]}...` received **{amount_sol:.4f} SOL**",
                color=COLOR_INFO,
            )
            return embed, None

        return None, None

    # ------------------------------------------------------------------------
    # ACTION HANDLERS & HELPERS (Token-Efficient Shared Logic)
    # ------------------------------------------------------------------------

    async def handle_buy(
        self,
        target: str,
        size_sol: float | None = None,
        source: str = "discord",
    ) -> tuple[discord.Embed, QuickBuyView]:
        """Execute buy / snipe order and return feedback embed and QuickBuyView."""
        actual_size = size_sol if size_sol is not None else self.preset_p1_sol
        cand = self._core.screener.scan_and_evaluate(target.strip())
        self._core.screener.accept_candidate(cand.creator_wallet, self._core.service)
        res = await self._core.execute_command(
            BotCommand(
                name="snipe",
                args=(cand.token_mint, str(actual_size)),
                source=source,
            )
        )
        embed = discord.Embed(
            title=f"🚀 Buy Order Executed · {cand.token_symbol}",
            description=f"Target: `{cand.token_mint}`\nSize: **{actual_size} SOL**\nStatus: **{res.message}**",
            color=COLOR_SUCCESS if res.ok else COLOR_DANGER,
        )
        view = QuickBuyView(
            self._core,
            cand.token_mint,
            preset_p1=self.preset_p1_sol,
            preset_p2=self.preset_p2_sol,
            preset_p3=self.preset_p3_sol,
        )
        return embed, view

    async def handle_sell(
        self,
        target: str,
        percentage: int = 100,
    ) -> discord.Embed:
        """Execute sell order and return feedback embed."""
        ppm = int((percentage / 100.0) * 1_000_000)
        res = await self._core.sell(target.strip(), ppm)
        return discord.Embed(
            title=f"💸 Sell Order ({percentage}%)",
            description=f"Target: `{target}`\nStatus: **{res.message}**",
            color=COLOR_SUCCESS if res.ok else COLOR_DANGER,
        )

    def handle_scan(
        self,
        address: str,
    ) -> tuple[discord.Embed, QuickBuyView]:
        """Perform on-chain cluster audit and return rich embed and QuickBuyView."""
        cand = self._core.screener.scan_and_evaluate(address.strip())
        return self._build_scan_embed(cand)

    def build_help_embed(self) -> discord.Embed:
        """Build playbook guide embed."""
        embed = discord.Embed(
            title="📖 Rugbot Trading Bot Playbook",
            description=(
                "High-density execution engine for Solana Pump.fun sniping.\n"
                "Use slash `/` or prefix `!` commands interchangeably."
            ),
            color=COLOR_INFO,
        )
        embed.add_field(
            name="`/start` / `!start`",
            value="Open interactive trading cockpit dashboard",
            inline=False,
        )
        embed.add_field(
            name="`/settings` / `!settings`",
            value="Configure buy presets (P1/P2/P3), slippage, tips & anti-MEV routing",
            inline=False,
        )
        embed.add_field(
            name="`/buy <addr> [size]` / `!buy`",
            value="Instant buy / snipe order (or click quick-buy buttons)",
            inline=False,
        )
        embed.add_field(
            name="`/sell <addr> [pct]` / `!sell`",
            value="Sell position percentage (e.g. `!sell <mint> 50`)",
            inline=False,
        )
        embed.add_field(
            name="`/positions` / `!positions`",
            value="Real-time open positions, PnL %, and quick exit buttons",
            inline=False,
        )
        embed.add_field(
            name="`/scan <address>` / `!scan`",
            value="Deep cluster audit, ATH consistency %, rug dynamics & optimal TP EV",
            inline=False,
        )
        embed.add_field(
            name="`/wallet` / `!wallet`",
            value="View trading wallet balance and portfolio holdings",
            inline=False,
        )
        embed.add_field(
            name="`/watch <wallet>` & `/unwatch`",
            value="Add or remove developer/funder targets for block-0 sniper tracking",
            inline=False,
        )
        embed.add_field(
            name="`/channel <#channel>`",
            value="View or set the alert feed channel directly",
            inline=False,
        )
        embed.add_field(
            name="`/kill` / `!kill`",
            value="Emergency kill switch to halt all automated order execution",
            inline=False,
        )
        embed.add_field(
            name="⚡ Feed Auto-Detection",
            value="Drop any Solana CA in chat to get instant Quick Buy buttons!",
            inline=False,
        )
        return embed

    # ------------------------------------------------------------------------
    # SLASH COMMANDS (Discord App Commands Tree)
    # ------------------------------------------------------------------------

    def _register_slash_commands(self) -> None:
        """Register modern Discord Slash Commands (/start, /buy, /sell, /settings, etc.)."""
        tree = self.bot.tree

        @tree.command(name="start", description="Open trading cockpit dashboard")
        async def slash_start(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            await interaction.followup.send(
                embed=self.build_cockpit_embed(), view=CockpitHomeView(self)
            )

        @tree.command(name="menu", description="Open trading cockpit dashboard")
        async def slash_menu(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            await interaction.followup.send(
                embed=self.build_cockpit_embed(), view=CockpitHomeView(self)
            )

        @tree.command(name="settings", description="Configure execution parameters")
        async def slash_settings(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            await interaction.followup.send(
                embed=self.build_settings_embed(), view=SettingsView(self)
            )

        @tree.command(name="wallet", description="View wallet balance and portfolio")
        async def slash_wallet(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            await interaction.followup.send(embed=self.build_wallet_embed())

        @tree.command(
            name="scan",
            description="On-chain cluster audit, ATH consistency & optimal TP",
        )
        @app_commands.describe(address="Token mint or developer wallet address")
        async def slash_scan(interaction: discord.Interaction, address: str) -> None:
            await interaction.response.defer()
            try:
                embed, view = self.handle_scan(address)
                await interaction.followup.send(embed=embed, view=view)
            except Exception as exc:
                await interaction.followup.send(f"❌ Audit failed: `{exc}`")

        @tree.command(name="screener", description="View candidate review queue")
        async def slash_screener(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            await interaction.followup.send(embed=self.build_screener_embed())

        @tree.command(name="watch", description="Track developer or funder wallet")
        @app_commands.describe(
            wallet="Wallet address to track", label="Optional friendly label"
        )
        async def slash_watch(
            interaction: discord.Interaction, wallet: str, label: str = ""
        ) -> None:
            await interaction.response.defer()
            res = self._core.watch(wallet.strip(), label=label.strip())
            await interaction.followup.send(f"🎯 **Watch Target**: {res.message}")

        @tree.command(name="unwatch", description="Remove wallet from tracking")
        @app_commands.describe(wallet="Wallet address to remove")
        async def slash_unwatch(interaction: discord.Interaction, wallet: str) -> None:
            await interaction.response.defer()
            res = self._core.unwatch(wallet.strip())
            await interaction.followup.send(f"🗑️ **Unwatch**: {res.message}")

        @tree.command(name="targets", description="List active sniper targets")
        async def slash_targets(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            await interaction.followup.send(embed=self.build_targets_embed())

        @tree.command(name="buy", description="Execute instant buy / snipe order")
        @app_commands.describe(
            target="Token Mint or Dev Address",
            size_sol="Amount in SOL (defaults to Preset P1)",
        )
        async def slash_buy(
            interaction: discord.Interaction,
            target: str,
            size_sol: float | None = None,
        ) -> None:
            await interaction.response.defer()
            embed, view = await self.handle_buy(target, size_sol, "discord_slash")
            await interaction.followup.send(embed=embed, view=view)

        @tree.command(name="snipe", description="Execute instant buy (alias of /buy)")
        @app_commands.describe(
            target="Token Mint or Dev Address",
            size_sol="Amount in SOL (defaults to Preset P1)",
        )
        async def slash_snipe(
            interaction: discord.Interaction,
            target: str,
            size_sol: float | None = None,
        ) -> None:
            await slash_buy(interaction, target, size_sol)

        @tree.command(name="sell", description="Sell open token position")
        @app_commands.describe(
            target="Token Mint / Market ID",
            percentage="Percentage to sell (e.g. 50, 100)",
        )
        async def slash_sell(
            interaction: discord.Interaction,
            target: str,
            percentage: int = 100,
        ) -> None:
            await interaction.response.defer()
            embed = await self.handle_sell(target, percentage)
            await interaction.followup.send(embed=embed)

        @tree.command(name="positions", description="List open positions & PnL")
        async def slash_positions(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            embed, views = self.build_positions_embed()
            await interaction.followup.send(embed=embed)
            for v in views[:3]:
                await interaction.followup.send(view=v)

        @tree.command(name="status", description="Get core daemon status")
        async def slash_status(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            await interaction.followup.send(embed=self.build_cockpit_embed())

        @tree.command(
            name="channel", description="View or configure alert notification channel"
        )
        @app_commands.describe(channel="Channel to route alerts to")
        async def slash_channel(
            interaction: discord.Interaction,
            channel: discord.TextChannel | None = None,
        ) -> None:
            await interaction.response.defer()
            if channel is not None:
                self.set_channel(channel.id)
                embed = discord.Embed(
                    title="📌 Alert Channel Configured",
                    description=f"Alerts will now be sent to <#{channel.id}> (`{channel.id}`).",
                    color=COLOR_SUCCESS,
                )
                await interaction.followup.send(embed=embed)
            else:
                embed = discord.Embed(
                    title="📌 Current Alert Channel",
                    description=(
                        f"Active alert channel is <#{self._channel_id}> (`{self._channel_id}`).\n"
                        "Use `/channel <#target-channel>` to change it."
                    ),
                    color=COLOR_INFO,
                )
                await interaction.followup.send(embed=embed)

        @tree.command(name="kill", description="Toggle global emergency kill switch")
        async def slash_kill(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            res = self._core.toggle_kill_switch()
            embed = discord.Embed(
                title="🚨 Kill Switch Toggled",
                description=res.message,
                color=COLOR_DANGER
                if "ENABLED" in res.message.upper()
                else COLOR_SUCCESS,
            )
            await interaction.followup.send(embed=embed)

        @tree.command(name="help", description="Guide to all Rugbot commands")
        async def slash_help(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                embed=self.build_help_embed(), ephemeral=True
            )

    # ------------------------------------------------------------------------
    # PREFIX COMMANDS (!start, !buy, !sell, !settings, !positions, etc.)
    # ------------------------------------------------------------------------

    def _register_prefix_commands(self) -> None:
        """Register text-based prefix commands (!start, !buy, !sell, !positions, etc.)."""

        @self.bot.command(name="start", aliases=["menu", "cockpit", "c"])
        async def cmd_start(ctx: commands.Context) -> None:
            await ctx.send(embed=self.build_cockpit_embed(), view=CockpitHomeView(self))

        @self.bot.command(name="settings", aliases=["config", "set"])
        async def cmd_settings(ctx: commands.Context) -> None:
            await ctx.send(embed=self.build_settings_embed(), view=SettingsView(self))

        @self.bot.command(name="wallet", aliases=["balance", "bal", "w"])
        async def cmd_wallet(ctx: commands.Context) -> None:
            await ctx.send(embed=self.build_wallet_embed())

        @self.bot.command(name="buy", aliases=["snipe", "b"])
        async def cmd_buy(
            ctx: commands.Context,
            target: str,
            size_sol: float | None = None,
        ) -> None:
            async with ctx.typing():
                embed, view = await self.handle_buy(target, size_sol, "discord_prefix")
                await ctx.send(embed=embed, view=view)

        @self.bot.command(name="sell", aliases=["s"])
        async def cmd_sell(
            ctx: commands.Context,
            target: str,
            percentage: int = 100,
        ) -> None:
            embed = await self.handle_sell(target, percentage)
            await ctx.send(embed=embed)

        @self.bot.command(name="positions", aliases=["pos", "p"])
        async def cmd_positions(ctx: commands.Context) -> None:
            embed, views = self.build_positions_embed()
            await ctx.send(embed=embed)
            for v in views[:3]:
                await ctx.send(view=v)

        @self.bot.command(name="scan")
        async def cmd_scan(ctx: commands.Context, address: str) -> None:
            async with ctx.typing():
                try:
                    embed, view = self.handle_scan(address)
                    await ctx.send(embed=embed, view=view)
                except Exception as exc:
                    await ctx.send(f"❌ Scan failed: `{exc}`")

        @self.bot.command(name="screener")
        async def cmd_screener(ctx: commands.Context) -> None:
            await ctx.send(embed=self.build_screener_embed())

        @self.bot.command(name="watch")
        async def cmd_watch(
            ctx: commands.Context, wallet: str, label: str = ""
        ) -> None:
            res = self._core.watch(wallet.strip(), label=label.strip())
            await ctx.send(f"🎯 **Watch Target**: {res.message}")

        @self.bot.command(name="unwatch")
        async def cmd_unwatch(ctx: commands.Context, wallet: str) -> None:
            res = self._core.unwatch(wallet.strip())
            await ctx.send(f"🗑️ **Unwatch**: {res.message}")

        @self.bot.command(name="targets", aliases=["list", "t"])
        async def cmd_targets(ctx: commands.Context) -> None:
            await ctx.send(embed=self.build_targets_embed())

        @self.bot.command(name="status")
        async def cmd_status(ctx: commands.Context) -> None:
            await ctx.send(embed=self.build_cockpit_embed())

        @self.bot.command(name="channel")
        async def cmd_channel(
            ctx: commands.Context, channel_input: str | None = None
        ) -> None:
            if channel_input:
                cleaned_id = "".join(c for c in channel_input if c.isdigit())
                if cleaned_id:
                    self.set_channel(int(cleaned_id))
                    await ctx.send(
                        f"📌 **Notification Channel Set**: <#{cleaned_id}> (`{cleaned_id}`)"
                    )
                    return
            await ctx.send(
                f"📌 **Active Notification Channel**: <#{self._channel_id}> (`{self._channel_id}`)"
            )

        @self.bot.command(name="kill", aliases=["stop"])
        async def cmd_kill(ctx: commands.Context) -> None:
            res = self._core.toggle_kill_switch()
            await ctx.send(f"🚨 **Kill Switch**: {res.message}")

        @self.bot.command(name="help", aliases=["h"])
        async def cmd_help(ctx: commands.Context) -> None:
            await ctx.send(embed=self.build_help_embed())


# ============================================================================
# RUNNER ENTRY POINT
# ============================================================================


def main(argv: Sequence[str] | None = None) -> None:
    """Run the modern Discord adapter from CLI flags or environment configuration."""
    parser = argparse.ArgumentParser(description="Rugbot Discord Bot Runner")

    parser.add_argument(
        "--token",
        "-t",
        help="Discord Bot Token (overrides DISCORD_TOKEN env)",
    )
    parser.add_argument(
        "--channel-id",
        "-c",
        type=int,
        help="Alerts Channel ID (overrides DISCORD_CHANNEL_ID env)",
    )

    parser.add_argument(
        "--state-dir",
        help="State directory (default: .state/discord)",
    )
    args = parser.parse_args(argv)

    resolve_dotenv()
    token = args.token or os.environ.get("DISCORD_TOKEN")
    if not token:
        raise DiscordConfigError(
            "DISCORD_TOKEN environment variable or --token is required"
        )

    channel_id_raw = args.channel_id or os.environ.get("DISCORD_CHANNEL_ID", "0")
    try:
        channel_id = int(channel_id_raw)
    except ValueError:
        raise DiscordConfigError("DISCORD_CHANNEL_ID must be an integer") from None

    allowed_raw = os.environ.get("DISCORD_ALLOWED_USER_IDS", "")
    allowed_user_ids = tuple(
        int(part) for part in allowed_raw.split(",") if part.strip()
    )

    state_dir = resolve_state_dir(
        Path(args.state_dir or os.environ.get("RUGBOT_STATE_DIR", ".state/discord"))
    )

    core = build_ui_runtime(
        state_dir=state_dir,
    )
    adapter = DiscordAdapter(
        core,
        token=token,
        channel_id=channel_id,
        allowed_user_ids=allowed_user_ids,
    )
    asyncio.run(adapter.connect())


__all__ = [
    "CockpitHomeView",
    "DiscordAdapter",
    "DiscordConfigError",
    "PositionActionView",
    "QuickBuyView",
    "SettingsView",
    "main",
]
