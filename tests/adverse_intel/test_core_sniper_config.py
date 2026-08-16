"""Tests for the minimal Pump wallet watcher configuration."""

import json
import tempfile
import unittest
from pathlib import Path

from rugbot.domain.decisions import AbstainResult
from rugbot.domain.launches import LaunchCreatedV2
from rugbot.ingest.pump_create_fixture_decode import (
    decode_pump_create_v2_fixture_artifact,
)
from rugbot.runtime.config import (
    CoreSniperConfig,
    SniperConfigError,
    TrackingMode,
    load_sniper_config,
    load_sniper_document,
    parse_sniper_config,
    parse_wallet_portfolio,
    save_sniper_document,
)
from rugbot.runtime.matcher import match_launch_target

FIXTURE = Path(
    "fixtures/finalized_transactions/pump_create_v2/"
    "4HbY43S9UigSctrfxY5nszgf3ozN1f4kPQYaqaFLZaCDhwa55rauuRmhP85u67U7dBvGFwB5C6stmkH2b1TNxgQh.json"
)
ZERO_ADDRESS = "11111111111111111111111111111111"


class CoreSniperConfigTests(unittest.TestCase):
    """Verify strict target and non-submitting execution configuration."""

    def test_wallet_and_token_targets_match_proven_launch(self) -> None:
        launch = _launch()

        wallet = match_launch_target(
            config=_config("wallet", launch.creator_pubkey), launch=launch
        )
        token = match_launch_target(
            config=_config("token", launch.mint_pubkey), launch=launch
        )

        self.assertTrue(wallet)
        self.assertTrue(token)

    def test_wrong_target_is_a_clean_non_match(self) -> None:
        result = match_launch_target(
            config=_config("wallet", ZERO_ADDRESS), launch=_launch()
        )

        self.assertFalse(result)

    def test_malformed_launch_abstains(self) -> None:
        result = match_launch_target(
            config=_config("token", ZERO_ADDRESS), launch=object()
        )

        self.assertIsInstance(result, AbstainResult)

    def test_config_has_only_target_and_execution(self) -> None:
        config = parse_sniper_config(_yaml())

        self.assertIsInstance(config, CoreSniperConfig)
        self.assertEqual(config.target.id, ZERO_ADDRESS)
        self.assertEqual(config.execution.quote_size_lamports, 1_000_000)
        with self.assertRaises(SniperConfigError):
            parse_sniper_config(_yaml() + "\nfeatures: {}\n")
        with self.assertRaises(SniperConfigError):
            parse_sniper_config(_yaml() + "\nplatforms: [pump_fun]\n")

    def test_tracking_mode_distinguishes_creation_and_buy_tracking(self) -> None:
        creations = parse_sniper_config(
            _yaml() + "\ntracking_mode: new_token_creations\n"
        )
        buys = parse_sniper_config(_yaml() + "\ntracking_mode: track_buys\n")

        self.assertIs(creations.tracking_mode, TrackingMode.NEW_TOKEN_CREATIONS)
        self.assertIs(buys.tracking_mode, TrackingMode.TRACK_BUYS)
        with self.assertRaises(SniperConfigError):
            parse_sniper_config(_yaml() + "\ntracking_mode: track_sells\n")

    def test_config_rejects_missing_fields_live_mode_and_float_money(self) -> None:
        with self.assertRaises(SniperConfigError):
            parse_sniper_config(f'target:\n  kind: token\n  id: "{ZERO_ADDRESS}"\n')
        with self.assertRaises(SniperConfigError):
            parse_sniper_config(_yaml(mode="live"))
        with self.assertRaises(SniperConfigError):
            parse_sniper_config(_yaml(quote_size="1.0"))

    def test_config_parses_big_buy_exit_ranges(self) -> None:
        config = parse_sniper_config(
            _yaml()
            + """rules:
  sell:
    auto_sell_big_buy:
      levels:
        - min_quote_base_units: 100
          max_quote_base_units: 200
          sell_fraction_ppm: 500000
"""
        )

        self.assertEqual(len(config.rules.sell.auto_sell_big_buy_levels), 1)
        self.assertEqual(
            config.rules.sell.auto_sell_big_buy_levels[0].sell_fraction_ppm,
            500_000,
        )

    def test_wallet_portfolio_is_ordered_and_strict(self) -> None:
        portfolio = parse_wallet_portfolio(
            "wallets:\n"
            '  - "11111111111111111111111111111111"\n'
            '  - "So11111111111111111111111111111111111111112"\n'
        )

        self.assertEqual(
            portfolio.wallets,
            (
                "11111111111111111111111111111111",
                "So11111111111111111111111111111111111111112",
            ),
        )

    def test_wallet_portfolio_rejects_duplicates_and_unknown_fields(self) -> None:
        with self.assertRaises(SniperConfigError):
            parse_wallet_portfolio(
                'wallets: ["11111111111111111111111111111111", '
                '"11111111111111111111111111111111"]\n'
            )
        with self.assertRaises(SniperConfigError):
            parse_wallet_portfolio(
                'wallets: ["11111111111111111111111111111111"]\nversion: 1\n'
            )

    def test_strategy_settings_are_configurable(self) -> None:
        config = parse_sniper_config(
            _yaml()
            + """strategy:
  min_volume_usd_micro: 30000000000
  max_creator_pairs: 10
  history_sample_count: 10
  min_win_rate_ppm: 500000
  max_buys_per_hour: 1
  max_entry_transaction_index: 0
  max_entry_deviation_ppm: 250000
  require_bundle_match: true
"""
        )

        self.assertEqual(config.strategy.min_volume_usd_micro, 30_000_000_000)
        self.assertEqual(config.strategy.max_creator_pairs, 10)
        self.assertEqual(config.strategy.max_entry_transaction_index, 0)
        self.assertTrue(config.strategy.require_bundle_match)

    def test_config_document_round_trip_rejects_duplicate_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watch.yaml"
            path.write_text(_yaml(), encoding="utf-8")
            document = load_sniper_document(path)
            document["strategy"] = {"max_entry_transaction_index": 0}

            save_sniper_document(path, document)

            self.assertEqual(
                load_sniper_config(path).strategy.max_entry_transaction_index,
                0,
            )
            path.write_text(
                _yaml() + '\ntarget:\n  kind: wallet\n  id: "' + ZERO_ADDRESS + '"\n',
                encoding="utf-8",
            )
            with self.assertRaises(SniperConfigError):
                load_sniper_document(path)


def _launch() -> LaunchCreatedV2:
    artifact = json.loads(FIXTURE.read_text(encoding="utf-8"))
    launch = decode_pump_create_v2_fixture_artifact(artifact)
    if not isinstance(launch, LaunchCreatedV2):
        raise TypeError
    return launch


def _config(kind: str, target_id: str) -> CoreSniperConfig:
    return parse_sniper_config(_yaml(target_kind=kind, target_id=target_id))


def _yaml(
    *,
    target_kind: str = "token",
    target_id: str = ZERO_ADDRESS,
    mode: str = "observe",
    quote_size: str = "1000000",
) -> str:
    return f"""target:
  kind: {target_kind}
  id: "{target_id}"
execution:
  mode: {mode}
  quote_size_lamports: {quote_size}
"""


if __name__ == "__main__":
    unittest.main()
