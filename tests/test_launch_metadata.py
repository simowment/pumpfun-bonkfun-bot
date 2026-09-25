"""Unit tests for launch metadata generator."""

import pytest

from rugbot.execution.launch.metadata_generator import (
    generate_template_metadata,
    sanitize_name,
    sanitize_ticker,
)


def test_sanitize_ticker():
    """Verify ticker sanitization and formatting."""
    assert sanitize_ticker("btc") == "BTC"
    assert sanitize_ticker("s-o-l") == "SOL"
    assert sanitize_ticker("a") == "ACOIN"  # Enforces min 2 chars
    assert sanitize_ticker("VERYLONGTICKERNAME") == "VERYLONGTI"  # Max 10 chars


def test_sanitize_name():
    """Verify name sanitization and formatting."""
    assert sanitize_name("Solana Doge") == "Solana Doge"
    assert sanitize_name("Moon & Stars!!!") == "Moon  Stars"
    assert sanitize_name("") == "Pump Token"


def test_generate_template_metadata():
    """Verify deterministic metadata generation from topic string."""
    meta = generate_template_metadata("Autonomous AI Agent")
    assert meta.name == "Autonomous AI Agent"
    assert meta.symbol == "AAA"
    assert "Autonomous AI Agent" in meta.description
    assert meta.metadata_uri.startswith("https://ipfs.io/ipfs/")

    # Single word topic
    single = generate_template_metadata("velocity")
    assert single.name == "Velocity"
    assert single.symbol == "VELOC"

    # Empty topic raises ValueError
    with pytest.raises(ValueError, match="cannot be empty"):
        generate_template_metadata("   ")
