"""Registry CRUD and address validation (no network)."""

import base58
import pytest

from rugbot.analysis.wallet_registry import (
    WalletRegistry,
    WalletRegistryError,
    canonical_wallet,
)

VALID_WALLET = "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"


def test_canonical_wallet_accepts_32_byte_base58() -> None:
    assert canonical_wallet(VALID_WALLET) == VALID_WALLET


def test_canonical_wallet_rejects_bad_input() -> None:
    with pytest.raises(WalletRegistryError):
        canonical_wallet("not-an-address")
    short = base58.b58encode(bytes(31)).decode("ascii")
    with pytest.raises(WalletRegistryError):
        canonical_wallet(short)
    with pytest.raises(WalletRegistryError):
        canonical_wallet("")


def test_registry_crud_roundtrip(tmp_path) -> None:
    registry = WalletRegistry(tmp_path / "registry.sqlite3")
    try:
        assert registry.count() == 0
        stored = registry.add(VALID_WALLET, quote_sol=0.1, note="decu")
        assert stored.wallet == VALID_WALLET
        assert stored.enabled is True
        assert stored.quote_sol == pytest.approx(0.1)
        assert registry.count() == 1
        assert [item.wallet for item in registry.list(enabled_only=True)] == [
            VALID_WALLET
        ]
        assert registry.set_enabled(VALID_WALLET, False) is True
        assert registry.list(enabled_only=True) == ()
        assert registry.get(VALID_WALLET) is not None
        assert registry.set_enabled(VALID_WALLET, True) is True
        assert registry.remove(VALID_WALLET) is True
        assert registry.count() == 0
        assert registry.remove(VALID_WALLET) is False
    finally:
        registry.close()


def test_registry_rejects_invalid_wallet_and_settings(tmp_path) -> None:
    registry = WalletRegistry(tmp_path / "registry.sqlite3")
    try:
        with pytest.raises(WalletRegistryError):
            registry.add("decu")
        with pytest.raises(WalletRegistryError):
            registry.add(VALID_WALLET, quote_sol=-1.0)
        with pytest.raises(WalletRegistryError):
            registry.add(VALID_WALLET, max_open=0)
        with pytest.raises(WalletRegistryError):
            registry.add(VALID_WALLET, bogus=1)
        assert registry.count() == 0
    finally:
        registry.close()


def test_registry_persists_across_reopen(tmp_path) -> None:
    path = tmp_path / "registry.sqlite3"
    first = WalletRegistry(path)
    first.add(VALID_WALLET)
    first.close()
    second = WalletRegistry(path)
    try:
        assert second.count() == 1
    finally:
        second.close()
