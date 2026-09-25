"""Unit tests for atomic launch bundle assembler."""

from solders.hash import Hash
from solders.keypair import Keypair

from rugbot.execution.launch.bundle_assembler import (
    SOLANA_TX_MTU_BYTES,
    assemble_launch_bundle,
    calculate_initial_buy_tokens,
)


def test_calculate_initial_buy_tokens():
    """Verify initial bonding curve token calculation with 100 bps protocol fee."""
    tokens = calculate_initial_buy_tokens(100_000_000)
    assert tokens > 3_000_000_000_000
    assert tokens < 4_000_000_000_000
    assert calculate_initial_buy_tokens(0) == 0


def test_assemble_launch_bundle_create_only():
    """Verify bundle assembly for create + jito tip without dev buy."""
    payer = Keypair()
    mint = Keypair()
    blockhash = Hash.default()

    bundle = assemble_launch_bundle(
        payer=payer,
        mint=mint,
        name="Test Launch",
        symbol="TLNCH",
        uri="https://ipfs.io/ipfs/QmTest",
        recent_blockhash=blockhash,
        buy_sol_lamports=0,
        jito_tip_lamports=3_000_000,
    )

    assert bundle.mint_pubkey == str(mint.pubkey())
    assert bundle.payer_pubkey == str(payer.pubkey())
    assert bundle.name == "Test Launch"
    assert bundle.symbol == "TLNCH"
    assert len(bundle.transactions) == 1
    assert bundle.instructions_count == 2
    assert bundle.expected_tokens == 0
    assert bundle.fits_mtu is True
    assert bundle.max_wire_size_bytes <= SOLANA_TX_MTU_BYTES
    assert len(bundle.transactions[0].signatures) == 2


def test_assemble_launch_bundle_atomic_buy_and_tip():
    """Verify atomic 2-transaction Jito bundle containing create_v2, ATAs, buy_v2, and Jito tip."""
    payer = Keypair()
    mint = Keypair()
    blockhash = Hash.default()

    bundle = assemble_launch_bundle(
        payer=payer,
        mint=mint,
        name="Moon Cat",
        symbol="MCAT",
        uri="https://ipfs.io/ipfs/QmCat",
        recent_blockhash=blockhash,
        buy_sol_lamports=100_000_000,  # 0.1 SOL
        jito_tip_lamports=5_000_000,  # 0.005 SOL
    )

    # 2 sequential transactions for Jito block inclusion:
    # Tx 1: create_v2
    # Tx 2: 2 ATAs + buy_v2 + Jito tip
    assert len(bundle.transactions) == 2
    assert bundle.instructions_count == 5
    assert bundle.expected_tokens > 0
    assert bundle.buy_sol_lamports == 100_000_000
    assert bundle.jito_tip_lamports == 5_000_000
    assert bundle.fits_mtu is True
    assert bundle.max_wire_size_bytes <= SOLANA_TX_MTU_BYTES
    for size in bundle.wire_sizes:
        assert size <= SOLANA_TX_MTU_BYTES
    assert len(bundle.transactions_base64) == 2
