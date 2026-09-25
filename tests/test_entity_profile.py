"""Unit tests for the rug_entity pure helpers (no network)."""

from rugbot.interfaces.cli.entity_profile import (
    assemble_payload,
    assemble_timeline,
    build_parser,
    cex_fleet_payload,
    dedupe_burners,
    entity_stats,
    fresh_burners_pct,
    funding_band,
)
from rugbot.tracker.funding_chain import FundedTransfer

NOW_S = 1_789_152_000.0


def _transfer(recipient, amount, signature="sig"):
    return FundedTransfer(
        recipient=recipient, amount_sol=amount, signature=signature, slot=None
    )


def _entry(mint, burner, created_age_s, ath, amount=1.0):
    return {
        "mint": mint,
        "symbol": "SYM",
        "burner": burner,
        "funding_amount_sol": amount,
        "created_ms": int((NOW_S - created_age_s) * 1000),
        "ath_multiple": ath,
    }


def test_dedupe_burners_newest_first_and_cap():
    transfers = [
        _transfer("B1", 1.0, "sig-new"),
        _transfer("B2", 2.0),
        _transfer("B1", 0.5, "sig-old"),
        _transfer("B3", 3.0),
    ]
    burners = dedupe_burners(transfers, 10)
    assert [b["wallet"] for b in burners] == ["B1", "B2", "B3"]
    assert burners[0]["funding_amount_sol"] == 1.0
    assert burners[0]["funding_signature"] == "sig-new"
    capped = dedupe_burners(transfers, 2)
    assert [b["wallet"] for b in capped] == ["B1", "B2"]


def test_fresh_burners_pct_ignores_unresolved():
    burners = [
        {"wallet": "B1", "lifetime_creations": 0},
        {"wallet": "B2", "lifetime_creations": 0},
        {"wallet": "B3", "lifetime_creations": 4},
        {"wallet": "B4", "lifetime_creations": None},
    ]
    assert fresh_burners_pct(burners) == round(2 / 3 * 100, 2)
    assert fresh_burners_pct([{"wallet": "B9", "lifetime_creations": None}]) is None
    assert fresh_burners_pct([]) is None


def test_timeline_sorted_oldest_first_and_stats():
    entries = [
        _entry("m1", "B1", 300, 3.0),
        _entry("m2", "B2", 100, 1.2),
        _entry("m3", "B1", 200, None),
    ]
    timeline = assemble_timeline(entries)
    assert [t["mint"] for t in timeline] == ["m1", "m3", "m2"]
    stats = entity_stats(timeline, win_multiple=2.0, active_days=7.0, now_s=NOW_S)
    assert stats["launches"] == 3
    assert stats["scored"] == 2
    assert stats["wins"] == 1
    assert stats["winrate_pct"] == 50.0
    assert stats["median_ath_multiple"] == 2.1
    assert stats["max_ath_multiple"] == 3.0
    assert stats["cadence_seconds"] == 100.0
    assert stats["active"] is True
    assert stats["no_candles"] == 1


def test_no_candles_excluded_and_inactive():
    entries = [_entry("m1", "B1", 10 * 86400, None)]
    timeline = assemble_timeline(entries)
    stats = entity_stats(timeline, win_multiple=2.0, active_days=7.0, now_s=NOW_S)
    assert stats["scored"] == 0
    assert stats["winrate_pct"] is None
    assert stats["active"] is False


def test_funding_band():
    assert funding_band([1.0, 2.0, 3.0]) == {
        "min_sol": 1.0,
        "median_sol": 2.0,
        "max_sol": 3.0,
    }
    assert funding_band([]) is None


def test_cex_guard_payload_has_no_entity():
    payload = cex_fleet_payload(funder="F1", creation_count=0, recipient_count=120)
    assert payload["fleet"] == "unattributable (CEX-shaped funder)"
    assert "timeline" not in payload
    assert "burners" not in payload
    assert payload["funded_recipients"] == 120


def test_no_verdict_keys_in_payload():
    timeline = assemble_timeline(
        [_entry("m1", "B1", 300, 3.0), _entry("m2", "B2", 100, 1.2)]
    )
    stats = entity_stats(timeline, win_multiple=2.0, active_days=7.0, now_s=NOW_S)
    payload = assemble_payload(
        funder="F1",
        chain={"input_kind": "wallet"},
        burners=[
            {"wallet": "B1", "funding_amount_sol": 1.0, "lifetime_creations": 2},
            {"wallet": "B2", "funding_amount_sol": 1.0, "lifetime_creations": 0},
        ],
        timeline=timeline,
        stats=stats,
        snipability={
            "cadence_seconds": stats["cadence_seconds"],
            "active": True,
            "funding_band_sol": funding_band([1.0, 1.0]),
            "fresh_burners_pct": 50.0,
        },
        coverage={
            "pages_fetched": 3,
            "recipients": 2,
            "burners_with_launches": 1,
            "staged_no_launch": 1,
            "no_candles": 0,
        },
    )
    keys: set[str] = set()

    def _walk(obj: object) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                keys.add(str(key).lower())
                _walk(value)
        elif isinstance(obj, list):
            for value in obj:
                _walk(value)

    _walk(payload)
    assert not keys & {"qualified", "recommend", "verdict", "passed", "failed"}
    body = repr({k: v for k, v in payload.items() if k != "reference"}).lower()
    for forbidden in ("qualified", "recommend", "worth tracking"):
        assert forbidden not in body


def test_parser_defaults():
    args = build_parser().parse_args(["sometarget"])
    assert args.target == "sometarget"
    assert args.pages == 3
    assert args.min_sol == 0.2
    assert args.max_sol == 5.0
    assert args.max_burners == 60
