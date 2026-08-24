# ruff: noqa: S106, TRY003

"""Comprehensive integration tests for the Discord Bot Interface and F-Project features."""

import asyncio
from pathlib import Path

import pytest

from rugbot.domain.transfers import SolTransfer
from rugbot.ingest.pump.models import TokenLaunch
from rugbot.interfaces.discord import (
    SOLANA_ADDRESS_REGEX,
    DiscordAdapter,
    PositionActionView,
    QuickBuyView,
)
from rugbot.runtime.app import build_ui_runtime
from rugbot.tracker.events import (
    DecisionEvent,
    LaunchDetected,
    TransferDetected,
    WalletFunded,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def core_instance(tmp_path: Path):
    return build_ui_runtime(state_dir=tmp_path)


@pytest.fixture
def discord_adapter(core_instance):
    return DiscordAdapter(
        core=core_instance,
        token="mock-token-for-test",
        channel_id=123456789,
        allowed_user_ids=(),
    )


@pytest.mark.anyio
async def test_discord_adapter_initialization(discord_adapter):
    """Verify that DiscordAdapter mounts commands and slash commands cleanly."""
    assert discord_adapter.bot is not None
    commands_names = [cmd.name for cmd in discord_adapter.bot.commands]
    assert "scan" in commands_names
    assert "screener" in commands_names
    assert "watch" in commands_names
    assert "unwatch" in commands_names
    assert "targets" in commands_names
    assert "status" in commands_names
    assert "kill" in commands_names

    # Check registered slash commands in tree
    slash_names = [cmd.name for cmd in discord_adapter.bot.tree.get_commands()]
    assert "scan" in slash_names
    assert "screener" in slash_names
    assert "watch" in slash_names
    assert "unwatch" in slash_names
    assert "targets" in slash_names
    assert "snipe" in slash_names
    assert "positions" in slash_names
    assert "status" in slash_names
    assert "kill" in slash_names
    assert "help" in slash_names


@pytest.mark.anyio
async def test_quick_buy_and_position_views(core_instance):
    """Verify QuickBuyView (Presets P1, P2, P3, Enroll) and PositionActionView (Sell Initials, 50%, 100%)."""
    target_addr = "E9CqsGL5uXPASB853f87ox8nZVgW7ucoeYMC4bN8pump"
    quick_buy_view = QuickBuyView(core_instance, target_addr)
    assert len(quick_buy_view.children) >= 4

    pos_view = PositionActionView(core_instance, "test_market_id")
    assert len(pos_view.children) == 3


@pytest.mark.anyio
async def test_solana_ca_regex_feed_detection():
    """Verify regex detects pump.fun contract addresses and dev wallets in chat feeds."""
    sample_text_1 = "Hey look at this gem E9CqsGL5uXPASB853f87ox8nZVgW7ucoeYMC4bN8pump going to the moon!"
    match_1 = SOLANA_ADDRESS_REGEX.search(sample_text_1)
    assert match_1 is not None
    assert match_1.group(1) == "E9CqsGL5uXPASB853f87ox8nZVgW7ucoeYMC4bN8pump"

    sample_text_2 = (
        "Dev wallet: 61mbw2ts9hzNGRN5PjZfBP15yxw6BGbHwkHhB1fkB8N3 created another token"
    )
    match_2 = SOLANA_ADDRESS_REGEX.search(sample_text_2)
    assert match_2 is not None
    assert match_2.group(1) == "61mbw2ts9hzNGRN5PjZfBP15yxw6BGbHwkHhB1fkB8N3"


@pytest.mark.anyio
async def test_event_embed_builders(discord_adapter):
    """Verify Discord rich embed generation for live events."""
    # 1. LaunchDetected event
    launch_evt = LaunchDetected(
        root_funder="61mbw2ts9hzNGRN5PjZfBP15yxw6BGbHwkHhB1fkB8N3",
        wallet="61mbw2ts9hzNGRN5PjZfBP15yxw6BGbHwkHhB1fkB8N3",
        timestamp=1700000000,
        event_type="LAUNCH_DETECTED",
        data={
            "symbol": "RUGGY",
            "name": "Ruggy Token",
            "mint": "E9CqsGL5uXPASB853f87ox8nZVgW7ucoeYMC4bN8pump",
            "creator": "61mbw2ts9hzNGRN5PjZfBP15yxw6BGbHwkHhB1fkB8N3",
            "market_cap_sol": 32.5,
        },
    )
    embed, view = discord_adapter._build_event_embed(launch_evt)
    assert embed is not None
    assert "RUGGY" in embed.title
    assert "Ruggy Token" in embed.description
    assert view is None

    # 2. DecisionEvent
    dec_evt = DecisionEvent(
        root_funder="61mbw2ts9hzNGRN5PjZfBP15yxw6BGbHwkHhB1fkB8N3",
        wallet="61mbw2ts9hzNGRN5PjZfBP15yxw6BGbHwkHhB1fkB8N3",
        timestamp=1700000001,
        event_type="EXEC_BUY",
        reason="Memecoin Bible B0 Sniper Condition Met",
    )
    embed_dec, _ = discord_adapter._build_event_embed(dec_evt)
    assert embed_dec is not None
    assert "EXEC_BUY" in embed_dec.title

    # 3. WalletFunded
    funded_evt = WalletFunded(
        root_funder="61mbw2ts9hzNGRN5PjZfBP15yxw6BGbHwkHhB1fkB8N3",
        wallet="61mbw2ts9hzNGRN5PjZfBP15yxw6BGbHwkHhB1fkB8N3",
        timestamp=1700000002,
        event_type="WALLET_FUNDED",
        data={"amount_lamports": 1_000_000_000},
    )
    embed_funded, _ = discord_adapter._build_event_embed(funded_evt)
    assert embed_funded is not None


@pytest.mark.anyio
async def test_scan_embed_generation(discord_adapter, core_instance):
    """Verify /scan on-chain evaluation embed with TP optimization and Memecoin Bible verdict."""
    sample_mint = "Anq6scgnxpMZvQN19XMSEUYQiDYuqNeh6cMZnN3Cpump"
    candidate = core_instance.screener.scan_and_evaluate(sample_mint)
    embed, view = discord_adapter._build_scan_embed(candidate)

    assert embed is not None
    assert candidate.token_symbol in embed.title
    assert candidate.creator_wallet[:8] in embed.fields[0].value
    assert f"{candidate.winrate_pct:.1f}%" in embed.fields[3].value
    assert candidate.optimal_tp_label in embed.fields[4].value
    assert view is not None


class _FakeChannel:
    """Minimal stand-in for a discord.TextChannel that records outbound sends."""

    def __init__(self) -> None:
        self.sent: list[tuple[object, object]] = []

    async def send(self, *, embed=None, view=None) -> None:
        self.sent.append((embed, view))


def _launch_event(
    mint: str = "E9CqsGL5uXPASB853f87ox8nZVgW7ucoeYMC4bN8pump",
    signature: str = "5x" * 20,
) -> LaunchDetected:
    """Build a LaunchDetected event carrying the canonical engine evidence fields."""
    return LaunchDetected(
        root_funder="61mbw2ts9hzNGRN5PjZfBP15yxw6BGbHwkHhB1fkB8N3",
        wallet="61mbw2ts9hzNGRN5PjZfBP15yxw6BGbHwkHhB1fkB8N3",
        timestamp=1700000000,
        event_type="LAUNCH_DETECTED",
        data={
            "symbol": "RUGGY",
            "name": "Ruggy Token",
            "mint": mint,
            "creator": "61mbw2ts9hzNGRN5PjZfBP15yxw6BGbHwkHhB1fkB8N3",
            "root_funder": "61mbw2ts9hzNGRN5PjZfBP15yxw6BGbHwkHhB1fkB8N3",
            "depth": 1,
            "slot": 12345,
            "signature": signature,
        },
    )


@pytest.mark.anyio
async def test_tracker_event_callback_renders_read_only_launch(discord_adapter):
    """Verify the adapter forwards a LaunchDetected as a read-only embed with links."""
    channel = _FakeChannel()
    discord_adapter.bot.get_channel = lambda _cid: channel

    await discord_adapter._on_tracker_event(_launch_event())

    assert len(channel.sent) == 1
    embed, view = channel.sent[0]
    assert embed is not None
    assert view is None
    assert "RUGGY" in embed.title
    assert "Ruggy Token" in embed.description
    field_values = [f.value for f in embed.fields]
    assert any("dexscreener.com" in v for v in field_values)
    assert any("solscan.io" in v for v in field_values)


@pytest.mark.anyio
async def test_tracker_event_callback_skips_unrenderable_events(discord_adapter):
    """Verify the adapter only forwards events it can render to the alerts channel."""
    channel = _FakeChannel()
    discord_adapter.bot.get_channel = lambda _cid: channel

    transfer = TransferDetected(
        root_funder="61mbw2ts9hzNGRN5PjZfBP15yxw6BGbHwkHhB1fkB8N3",
        wallet="61mbw2ts9hzNGRN5PjZfBP15yxw6BGbHwkHhB1fkB8N3",
        timestamp=1700000003,
        event_type="TRANSFER_DETECTED",
    )
    await discord_adapter._on_tracker_event(transfer)

    assert channel.sent == []


@pytest.mark.anyio
async def test_tracker_event_callback_dedupes_same_mint(discord_adapter):
    """Verify delivering the same launch mint twice renders only once."""
    channel = _FakeChannel()
    discord_adapter.bot.get_channel = lambda _cid: channel

    await discord_adapter._on_tracker_event(_launch_event())
    await discord_adapter._on_tracker_event(_launch_event())

    assert len(channel.sent) == 1


@pytest.mark.anyio
async def test_tracker_event_callback_renders_distinct_mints_same_wallet(
    discord_adapter,
):
    """Verify two distinct mints from the same wallet/root/timestamp both render."""
    channel = _FakeChannel()
    discord_adapter.bot.get_channel = lambda _cid: channel

    await discord_adapter._on_tracker_event(_launch_event())
    await discord_adapter._on_tracker_event(
        _launch_event(
            mint="E9CqsGL5uXPASB853f87ox8nZVgW7ucoeYMC4bN8pump2",
            signature="6y" * 20,
        )
    )

    assert len(channel.sent) == 2


@pytest.mark.anyio
async def test_tracker_event_callback_retries_when_channel_unavailable(
    discord_adapter,
):
    """Verify a missing channel is an explicit non-delivery that allows a retry."""
    discord_adapter.bot.get_channel = lambda _cid: None
    with pytest.raises(RuntimeError):
        await discord_adapter._on_tracker_event(_launch_event())

    channel = _FakeChannel()
    discord_adapter.bot.get_channel = lambda _cid: channel
    await discord_adapter._on_tracker_event(_launch_event())

    assert len(channel.sent) == 1


@pytest.mark.anyio
async def test_tracker_event_callback_retries_after_send_exception(discord_adapter):
    """Verify a send exception does not mark the event seen, allowing a retry."""

    class _FailingChannel:
        def __init__(self) -> None:
            self.sent: list[tuple[object, object]] = []
            self.fail = True

        async def send(self, *, embed=None, view=None) -> None:
            if self.fail:
                raise RuntimeError("send failed")
            self.sent.append((embed, view))

    channel = _FailingChannel()
    discord_adapter.bot.get_channel = lambda _cid: channel
    with pytest.raises(RuntimeError):
        await discord_adapter._on_tracker_event(_launch_event())

    channel.fail = False
    await discord_adapter._on_tracker_event(_launch_event())

    assert len(channel.sent) == 1


@pytest.mark.anyio
async def test_tracked_launch_footer_is_finalized_rpc(discord_adapter):
    """Verify the tracked-launch footer cites finalized RPC evidence, not PumpPortal."""
    embed, view = discord_adapter._build_event_embed(_launch_event())

    assert embed is not None
    assert view is None
    assert "Finalized" in embed.footer.text
    assert "RPC" in embed.footer.text
    assert "PumpPortal" not in embed.footer.text


@pytest.mark.anyio
async def test_tracked_launch_embed_has_no_buy_or_snipe_controls(discord_adapter):
    """Verify the tracked-launch alert carries no QuickBuyView or snipe dispatch."""
    embed, view = discord_adapter._build_event_embed(_launch_event())

    assert embed is not None
    assert view is None
    assert "Buy" not in embed.title
    assert "Snipe" not in embed.title
    assert "snipe" not in embed.description.lower()


# --- Phase 3: durable outbox consumer (Discord on-demand drain) ---


def _seed_discord_launch(repo, service):
    """Seed one funder/creator and emit a launch, returning its mint."""
    funder = "FunderDiscordA111111111111111111111111111111"
    creator = "CreatorDiscordA1111111111111111111111111111"
    service.add_funder(funder, label="Funder")
    service.handle_transfer(
        SolTransfer(
            signature="sig_fund_creator",
            instruction_index=0,
            slot=100,
            timestamp=1000,
            sender=funder,
            recipient=creator,
            lamports=50_000_000,
        )
    )
    mint = "MintDiscordA111111111111111111111111111111"
    service.handle_launch(
        TokenLaunch(
            signature="sig_launch",
            slot=200,
            timestamp=2000,
            creator=creator,
            mint=mint,
            symbol="SYM",
            name="Sym",
        )
    )
    return mint, funder, creator


@pytest.mark.anyio
async def test_discord_on_demand_drain_drains_and_marks_on_success(tmp_path: Path):
    """Verify on-demand drain drains 'discord' rows and marks delivered only after send succeeds."""
    core = build_ui_runtime(state_dir=tmp_path)
    _seed_discord_launch(core.repository, core.service)
    assert len(core.repository.get_undelivered_alerts("discord")) == 1

    adapter = DiscordAdapter(core=core, token="mock-token", channel_id=123)
    channel = _FakeChannel()
    adapter.bot.get_channel = lambda _cid: channel

    await adapter._drain_discord_outbox()

    assert len(channel.sent) == 1
    embed, view = channel.sent[0]
    assert embed is not None
    assert view is None
    assert len(core.repository.get_undelivered_alerts("discord")) == 0
    # tui partition must remain independent
    assert len(core.repository.get_undelivered_alerts("tui")) == 1
    await core.close()


@pytest.mark.anyio
async def test_discord_outbox_not_marks_on_missing_channel(tmp_path: Path):
    """Verify missing channel does not mark delivered (retry on next on-demand drain)."""
    core = build_ui_runtime(state_dir=tmp_path)
    _seed_discord_launch(core.repository, core.service)
    adapter = DiscordAdapter(core=core, token="mock-token", channel_id=123)
    adapter.bot.get_channel = lambda _cid: None

    await adapter._drain_discord_outbox()

    assert len(core.repository.get_undelivered_alerts("discord")) == 1
    await core.close()


@pytest.mark.anyio
async def test_discord_outbox_not_marks_on_send_failure(tmp_path: Path):
    """Verify send failure does not mark delivered (retry on next on-demand drain)."""
    core = build_ui_runtime(state_dir=tmp_path)
    _seed_discord_launch(core.repository, core.service)
    adapter = DiscordAdapter(core=core, token="mock-token", channel_id=123)

    class _FailingChannel:
        async def send(self, *, embed=None, view=None) -> None:
            raise RuntimeError("send failed")

    adapter.bot.get_channel = lambda _cid: _FailingChannel()
    await adapter._drain_discord_outbox()
    assert len(core.repository.get_undelivered_alerts("discord")) == 1

    # Retry with a healthy channel succeeds and marks.
    channel = _FakeChannel()
    adapter.bot.get_channel = lambda _cid: channel
    await adapter._drain_discord_outbox()
    assert len(channel.sent) == 1
    assert len(core.repository.get_undelivered_alerts("discord")) == 0
    await core.close()


@pytest.mark.anyio
async def test_discord_outbox_restart_re_delivers_undelivered(tmp_path: Path):
    """Verify restart re-delivers undelivered but not already delivered alerts."""
    core = build_ui_runtime(state_dir=tmp_path)
    mint, _, _ = _seed_discord_launch(core.repository, core.service)
    # Mark as delivered (simulate previous successful drain)
    core.repository.mark_alerts_delivered("discord", (mint,))
    assert len(core.repository.get_undelivered_alerts("discord")) == 0

    # New launch remains undelivered
    mint2 = "MintDiscordB111111111111111111111111111111"
    core.service.handle_launch(
        TokenLaunch(
            signature="sig_launch2",
            slot=201,
            timestamp=2001,
            creator="CreatorDiscordA1111111111111111111111111111",
            mint=mint2,
            symbol="SYM2",
            name="Sym2",
        )
    )
    assert len(core.repository.get_undelivered_alerts("discord")) == 1
    await core.close()

    # Simulate restart with same state_dir (same SQLite file)
    core2 = build_ui_runtime(state_dir=tmp_path)
    adapter2 = DiscordAdapter(core=core2, token="mock-token", channel_id=123)
    channel = _FakeChannel()
    adapter2.bot.get_channel = lambda _cid: channel
    await adapter2._drain_discord_outbox()
    assert len(channel.sent) == 1
    assert channel.sent[0][0].description is not None
    assert mint2 in channel.sent[0][0].description
    assert len(core2.repository.get_undelivered_alerts("discord")) == 0
    await core2.close()


@pytest.mark.anyio
async def test_discord_live_event_marks_delivered_and_dedupes_on_demand_drain(
    tmp_path: Path,
):
    """Verify live LaunchDetected via EventBus marks delivered and dedupes on-demand drain."""
    core = build_ui_runtime(state_dir=tmp_path)
    adapter = DiscordAdapter(core=core, token="mock-token", channel_id=123)
    channel = _FakeChannel()
    adapter.bot.get_channel = lambda _cid: channel
    # Subscribe like connect() does (without starting bot)
    core.subscribe(adapter._on_tracker_event)
    _seed_discord_launch(core.repository, core.service)
    # Give event loop a tick for async handler
    await asyncio.sleep(0.08)
    # One WalletFunded + one LaunchDetected are sent live; filter launch
    launch_embeds = [e for e, _ in channel.sent if e and "Launch" in (e.title or "")]
    assert len(launch_embeds) == 1
    # Outbox should be drained (marked by live handler)
    assert len(core.repository.get_undelivered_alerts("discord")) == 0
    await adapter._drain_discord_outbox()
    launch_after = [e for e, _ in channel.sent if e and "Launch" in (e.title or "")]
    assert len(launch_after) == 1  # not resent
    await core.close()


@pytest.mark.anyio
async def test_discord_startup_drain_without_poller(tmp_path: Path):
    """Verify connect() drains on startup without a background polling loop."""
    core = build_ui_runtime(state_dir=tmp_path)
    # Seed one undelivered discord alert before connect
    _seed_discord_launch(core.repository, core.service)
    assert len(core.repository.get_undelivered_alerts("discord")) == 1
    adapter = DiscordAdapter(core=core, token="mock-token", channel_id=999)
    channel = _FakeChannel()
    adapter.bot.get_channel = lambda _cid: channel
    drained = False

    async def _fake_start(_token: str) -> None:
        nonlocal drained
        drained = True

    adapter.bot.start = _fake_start  # type: ignore[method-assign]
    adapter.bot.close = lambda: __import__("asyncio").sleep(0)  # type: ignore[method-assign]

    await adapter.connect()
    assert drained is True
    # Startup drain delivered without polling
    assert len(channel.sent) == 1
    assert len(core.repository.get_undelivered_alerts("discord")) == 0
    assert not hasattr(adapter, "_discord_poller_task")
    # drain_pending_discord_alerts is the on-demand entrypoint
    assert hasattr(adapter, "drain_pending_discord_alerts")
    count = await adapter.drain_pending_discord_alerts()
    assert count == 0
    await adapter.disconnect()
    await core.close()


@pytest.mark.anyio
async def test_discord_no_polling_timer(tmp_path: Path):
    """Verify the adapter does not create a periodic polling task."""
    core = build_ui_runtime(state_dir=tmp_path)
    adapter = DiscordAdapter(core=core, token="mock-token", channel_id=999)
    # No asyncio.sleep polling loop referencing outbox poll seconds
    assert not hasattr(adapter, "_DISCORD_OUTBOX_POLL_SECONDS")
    assert not hasattr(adapter, "_run_discord_outbox_poller")
    await core.close()
