"""Theme-wave measurement over recent launches (measurements only).

Tests the hypothesis that memecoin outcomes are driven by THEME WAVES:
a topic heats up, copycats follow, and copycats ride the wave. Per coin
(name/symbol/description on the listing row) a distinctive theme signature
is derived; wave features are computed leakage-safe (strict created_at
order, only coins j < i); outcomes come from the post-bundle trajectory.
No models, no verdicts.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import time
from collections.abc import Mapping, Sequence
from typing import Any

from rugbot.backtest.runners.entry_resolver import trajectory_from_1s_candles
from rugbot.integrations.pumpfun_api import get_client
from rugbot.interfaces.cli.offenders import collect_recent_launches
from rugbot.runtime.config import load_provider_settings, resolve_dotenv
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

TOKEN_RE = re.compile(r"[a-z0-9]+")

# Small common-word list: tokens excluded from distinctiveness even when
# frequent. Reported in output so the signature rule is auditable.
COMMON_WORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "coin",
        "token",
        "meme",
        "memecoin",
        "crypto",
        "sol",
        "solana",
        "pump",
        "fun",
        "moon",
        "safe",
        "official",
    }
)

# Launch boilerplate / stopwords dropped before signature extraction.
STOPWORDS: frozenset[str] = frozenset(
    {
        "launched",
        "launch",
        "join",
        "joining",
        "welcome",
        "tg",
        "discord",
        "telegram",
        "twitter",
        "website",
        "community",
        "memecoin",
        "meme",
        "coin",
        "token",
        "pump",
        "fun",
        "pumpfun",
        "buy",
        "hold",
        "holders",
        "moon",
        "tothemoon",
        "please",
        "visit",
        "check",
        "link",
        "links",
        "http",
        "https",
        "www",
        "com",
        "tme",
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "our",
        "your",
        "you",
        "are",
        "all",
        "new",
        "best",
        "just",
        "get",
        "got",
    }
)

WIN_2X_MULTIPLE = 2.0
WIN_3X_MULTIPLE = 3.0
MIN_TOKEN_LEN = 3
MIN_DOC_FREQ = 2
WINDOW_60M_S = 3600.0
WINDOW_S: tuple[tuple[int, float], ...] = ((15, 900.0), (60, 3600.0), (240, 14400.0))

WAVE_FEATURES: tuple[str, ...] = (
    "theme_prior_count_15m",
    "theme_prior_count_60m",
    "theme_prior_count_240m",
    "theme_prior_winners_60m",
    "theme_best_prior_multiple_60m",
    "theme_seconds_since_prior",
    "is_first_of_theme",
    "is_copycat",
)


def tokenize(text: object) -> set[str]:
    """Normalise text to a token set (lowercase, len>=3, no boilerplate).

    Args:
        text: Raw text value; non-strings yield an empty set.

    Returns:
        Token set after lowercasing, punctuation split, length and
        stopword filtering. Never raises.
    """
    if not isinstance(text, str) or not text:
        return set()
    tokens = set(TOKEN_RE.findall(text.lower()))
    return {t for t in tokens if len(t) >= MIN_TOKEN_LEN and t not in STOPWORDS}


def coin_tokens(coin: Mapping[str, Any]) -> set[str]:
    """Return the raw token set for one listing row.

    Args:
        coin: Listing coin dict with name/symbol/description.

    Returns:
        Union of tokens from the three text fields.
    """
    parts: set[str] = set()
    if isinstance(coin, Mapping):
        for key in ("name", "symbol", "description"):
            parts |= tokenize(coin.get(key))
    return parts


def derive_signatures(coins: Sequence[Mapping[str, Any]]) -> list[set[str]]:
    """Derive distinctive theme signatures, one set per coin.

    A token is distinctive when it appears in >= 2 of the collected
    launches and is NOT in COMMON_WORDS. Each coin's signature is its
    own token set restricted to distinctive tokens (may be empty).

    Args:
        coins: Listing coin dicts in any order (index-aligned output).

    Returns:
        List of signature sets aligned with ``coins``.
    """
    token_sets = [coin_tokens(c) for c in coins]
    doc_freq: dict[str, int] = {}
    for tokens in token_sets:
        for token in tokens:
            doc_freq[token] = doc_freq.get(token, 0) + 1
    distinctive = {
        t for t, n in doc_freq.items() if n >= MIN_DOC_FREQ and t not in COMMON_WORDS
    }
    return [tokens & distinctive for tokens in token_sets]


def _created_ms(coin: Mapping[str, Any]) -> int | None:
    raw = coin.get("created_timestamp")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        try:
            return int(raw)
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def _fetch_outcome(
    mint: str, created_ms: int | None
) -> tuple[bool | None, bool | None, float | None, bool]:
    """Resolve one mint to (reached_2x, reached_3x, max_multiple, has_candles).

    Fail-soft: any fetch/parse failure yields (None, None, None, False).
    """
    try:
        candles = get_client().fetch_candlesticks(
            mint, interval="1s", limit=300, created_ts=0
        )
    except Exception as exc:  # noqa: BLE001 - fail-soft per launch
        logger.debug("candles failed for %s: %s", mint[:8], exc)
        return None, None, None, False
    if not isinstance(candles, list) or not candles:
        return None, None, None, False
    try:
        _points, ath = trajectory_from_1s_candles(candles, created_ms=created_ms)
    except Exception as exc:  # noqa: BLE001 - fail-soft per launch
        logger.debug("trajectory failed for %s: %s", mint[:8], exc)
        return None, None, None, False
    if ath is None:
        return None, None, None, False
    try:
        multiple = float(ath)
    except (TypeError, ValueError):
        return None, None, None, False
    return (
        bool(multiple >= WIN_2X_MULTIPLE),
        bool(multiple >= WIN_3X_MULTIPLE),
        multiple,
        True,
    )


def _prior_gap_s(created: object, prev_created: object) -> float | None:
    """Return the prior-to-current gap in seconds, or None if unusable."""
    if not isinstance(created, int) or not isinstance(prev_created, int):
        return None
    gap_s = (created - prev_created) / 1000.0
    return gap_s if gap_s >= 0 else None


def _accumulate_prior(
    state: dict[str, Any], gap_s: float, prev: Mapping[str, Any]
) -> None:
    """Fold one same-theme prior into the running wave state (in place)."""
    state["has_prior_same"] = True
    for window_min, window_s in WINDOW_S:
        if gap_s <= window_s:
            state["counts"][window_min] += 1
    if gap_s <= WINDOW_60M_S:
        if prev.get("reached_2x") is True:
            state["prior_winners"] += 1
        mult = prev.get("max_multiple")
        if isinstance(mult, (int, float)) and not isinstance(mult, bool):
            best = state["best_multiple"]
            state["best_multiple"] = (
                float(mult) if best is None else max(best, float(mult))
            )
    current = state["seconds_since"]
    if current is None or gap_s < current:
        state["seconds_since"] = gap_s


def compute_wave_features(
    ordered: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compute leakage-safe wave features in strict created_at order.

    Args:
        ordered: Per-coin dicts sorted by created_at ascending, each with
            ``signature`` (set), ``created_ms`` (int|None) and outcome
            fields ``reached_2x`` (bool|None), ``max_multiple`` (float|None).

    Returns:
        One feature dict per coin using ONLY coins j < i. Coins with
        unknown timestamps are treated as having no prior window overlap
        (counts 0, gap None, first/copycat from signature overlap only
        against timestamped priors is skipped -> first=1/copycat=0 when
        no comparable prior exists).
    """
    rows: list[dict[str, Any]] = []
    for i, coin in enumerate(ordered):
        sig: set[str] = coin.get("signature", set())
        created = coin.get("created_ms")
        state: dict[str, Any] = {
            "counts": {15: 0, 60: 0, 240: 0},
            "prior_winners": 0,
            "best_multiple": None,
            "seconds_since": None,
            "has_prior_same": False,
        }
        if sig:
            for prev in ordered[:i]:
                psig = prev.get("signature", set())
                if not (sig & psig):
                    continue
                gap_s = _prior_gap_s(created, prev.get("created_ms"))
                if gap_s is None:
                    continue
                _accumulate_prior(state, gap_s, prev)
        counts = state["counts"]
        has_prior_same = bool(state["has_prior_same"])
        rows.append(
            {
                "theme_prior_count_15m": counts[15],
                "theme_prior_count_60m": counts[60],
                "theme_prior_count_240m": counts[240],
                "theme_prior_winners_60m": state["prior_winners"],
                "theme_best_prior_multiple_60m": state["best_multiple"],
                "theme_seconds_since_prior": state["seconds_since"],
                "is_first_of_theme": 0 if has_prior_same else 1,
                "is_copycat": 1 if has_prior_same else 0,
            }
        )
    return rows


def auc_rank(scores: Sequence[float | None], labels: Sequence[bool]) -> float | None:
    """Compute AUC via mean-rank (Mann-Whitney), None-safe.

    Args:
        scores: Feature values (None entries are dropped with their label).
        labels: Win booleans aligned with scores.

    Returns:
        AUC in [0, 1], or None when fewer than one winner and one loser
        remain after null filtering.
    """
    paired = [(s, b) for s, b in zip(scores, labels, strict=False) if s is not None]
    if not paired:
        return None
    n_pos = sum(1 for _, b in paired if b)
    n_neg = len(paired) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    ordered = sorted(paired, key=lambda p: float(p[0]))
    rank_sum = 0.0
    i = 0
    n = len(ordered)
    while i < n:
        j = i
        while j + 1 < n and float(ordered[j + 1][0]) == float(ordered[i][0]):
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            if ordered[k][1]:
                rank_sum += avg_rank
        i = j + 1
    return (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def summarize_feature(
    values: Sequence[float | None],
    labels: Sequence[bool],
    *,
    name: str,
    base_rate: float | None,
) -> dict[str, Any]:
    """Summarise one wave feature: winner/loser medians, AUC, artifact flag.

    Args:
        values: Feature values aligned with labels (None = missing).
        labels: Win booleans for scored coins only.
        name: Feature name.
        base_rate: Overall 2x base rate for context.

    Returns:
        Summary dict with medians (or winner/loser shares for binary
        features), AUC, separation and artifact flag. Binary features
        (is_first_of_theme/is_copycat) report shares instead of medians.
    """
    binary = name in ("is_first_of_theme", "is_copycat")
    win_vals = [v for v, b in zip(values, labels, strict=False) if b and v is not None]
    lose_vals = [
        v for v, b in zip(values, labels, strict=False) if not b and v is not None
    ]
    auc = auc_rank(values, labels)
    separation = abs(auc - 0.5) if auc is not None else 0.0
    artifact: str | None = None
    if name == "theme_seconds_since_prior":
        artifact = "null-when-first (no prior); medians over non-null only"
    if name == "theme_best_prior_multiple_60m":
        artifact = "null-when-no-prior-in-window; medians over non-null only"
    if binary:
        win_share = sum(win_vals) / len(win_vals) if win_vals else None
        lose_share = sum(lose_vals) / len(lose_vals) if lose_vals else None
        return {
            "feature": name,
            "n_winners": len(win_vals),
            "n_losers": len(lose_vals),
            "winner_share": win_share,
            "loser_share": lose_share,
            "auc": auc,
            "separation": separation,
            "base_rate": base_rate,
            "artifact": artifact,
        }
    return {
        "feature": name,
        "n_winners": len(win_vals),
        "n_losers": len(lose_vals),
        "winner_median": float(statistics.median(win_vals)) if win_vals else None,
        "loser_median": float(statistics.median(lose_vals)) if lose_vals else None,
        "auc": auc,
        "separation": separation,
        "base_rate": base_rate,
        "artifact": artifact,
    }


def two_by_two(
    has_prior_winner: Sequence[bool],
    reached_2x: Sequence[bool],
) -> dict[str, Any]:
    """2x2 test: prior theme winner (60m) vs this coin reached 2x.

    Args:
        has_prior_winner: True when theme_prior_winners_60m >= 1.
        reached_2x: Outcome booleans (scored coins only).

    Returns:
        Counts (a=prior&win, b=prior&loss, c=noprior&win, d=noprior&loss),
        group rates, lift (rate ratio) and a Wald log-RR 95% CI, or a
        reason when a cell is zero.
    """
    a = sum(1 for p, w in zip(has_prior_winner, reached_2x, strict=False) if p and w)
    b = sum(
        1 for p, w in zip(has_prior_winner, reached_2x, strict=False) if p and not w
    )
    c = sum(
        1 for p, w in zip(has_prior_winner, reached_2x, strict=False) if not p and w
    )
    d = sum(
        1 for p, w in zip(has_prior_winner, reached_2x, strict=False) if not p and not w
    )
    rate_prior = a / (a + b) if (a + b) > 0 else None
    rate_noprior = c / (c + d) if (c + d) > 0 else None
    lift = (
        (rate_prior / rate_noprior)
        if rate_prior is not None and rate_noprior not in (None, 0)
        else None
    )
    ci: list[float] | None = None
    ci_reason: str | None = None
    if min(a, b, c, d) <= 0 or lift is None:
        ci_reason = "zero cell: Wald CI undefined"
    else:
        try:
            se = math.sqrt(1.0 / a - 1.0 / (a + b) + 1.0 / c - 1.0 / (c + d))
            log_rr = math.log(float(lift))
            ci = [math.exp(log_rr - 1.96 * se), math.exp(log_rr + 1.96 * se)]
        except (ValueError, ZeroDivisionError) as exc:
            ci_reason = f"CI failed: {exc}"
    return {
        "a_prior_win": a,
        "b_prior_loss": b,
        "c_noprior_win": c,
        "d_noprior_loss": d,
        "rate_prior_winner": rate_prior,
        "rate_no_prior_winner": rate_noprior,
        "lift": lift,
        "ci95": ci,
        "ci_reason": ci_reason,
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the themes command."""
    parser = argparse.ArgumentParser(
        prog="rug_themes",
        description="Theme-wave measurement (leakage-safe, no verdicts).",
    )
    parser.add_argument("--limit", type=int, default=1500)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="listing page guard (default: ceil(limit/70)+2)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the theme-wave measurement report.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv``.

    Returns:
        Process exit code (0 normally).
    """
    args = build_parser().parse_args(argv)
    limit = max(1, int(args.limit))

    resolve_dotenv()
    providers = load_provider_settings()
    _ = providers.rpc_http if providers else ""

    coins, pages_fetched = collect_recent_launches(
        limit,
        max_pages=int(args.max_pages) if args.max_pages is not None else None,
    )
    signatures = derive_signatures(coins)
    ordered: list[dict[str, Any]] = []
    for coin, sig in zip(coins, signatures, strict=False):
        if not isinstance(coin, Mapping):
            continue
        mint = coin.get("mint")
        if not isinstance(mint, str) or not mint:
            continue
        created_ms = _created_ms(coin)
        w2, w3, mult, has_candles = _fetch_outcome(mint, created_ms)
        ordered.append(
            {
                "mint": mint,
                "created_ms": created_ms,
                "signature": sig,
                "reached_2x": w2,
                "reached_3x": w3,
                "max_multiple": mult,
                "has_candles": has_candles,
            }
        )
        time.sleep(0.02)
    # Strict created_at order; unknown timestamps sort last (never leak).
    ordered.sort(
        key=lambda r: (r["created_ms"] is None, r["created_ms"] or 0, str(r["mint"]))
    )
    wave_rows = compute_wave_features(ordered)

    scored_idx = [i for i, r in enumerate(ordered) if r.get("reached_2x") is not None]
    labels_2x = [bool(ordered[i]["reached_2x"]) for i in scored_idx]
    labels_3x = [bool(ordered[i]["reached_3x"]) for i in scored_idx]
    n_scored = len(scored_idx)
    base_2x = sum(labels_2x) / n_scored if n_scored else None
    base_3x = sum(labels_3x) / n_scored if n_scored else None
    coverage = {
        "rows": len(ordered),
        "launches_listed": len(coins),
        "pages_fetched": pages_fetched,
        "candles": sum(1 for r in ordered if r.get("has_candles")),
        "no_candles": sum(1 for r in ordered if not r.get("has_candles")),
        "scored": n_scored,
        "reached_2x_hits": sum(labels_2x),
        "reached_3x_hits": sum(labels_3x),
        "reached_2x_rate": base_2x,
        "reached_3x_rate": base_3x,
    }

    summaries: list[dict[str, Any]] = []
    for name in WAVE_FEATURES:
        values = [wave_rows[i].get(name) for i in scored_idx]
        summaries.append(
            summarize_feature(values, labels_2x, name=name, base_rate=base_2x)
        )
    summaries.sort(key=lambda s: float(s.get("separation") or 0.0), reverse=True)

    has_prior = [
        int(wave_rows[i].get("theme_prior_winners_60m") or 0) >= 1 for i in scored_idx
    ]
    contingency = two_by_two(has_prior, labels_2x)

    payload = {
        "coverage": coverage,
        "features": summaries,
        "prior_winner_2x2": contingency,
        "params": {"limit": limit},
        "rules": {
            "common_words": sorted(COMMON_WORDS),
            "stopwords": sorted(STOPWORDS),
            "distinctive_rule": "freq>=2 and not in common_words",
        },
    }
    if bool(args.json):
        print(json.dumps(payload, sort_keys=True, default=str))
        return 0
    print("=" * 78)
    print(" RUG THEMES  (theme-wave measurements, no verdicts)")
    print("=" * 78)
    print(
        f"[coverage] rows={coverage['rows']} listed={coverage['launches_listed']} "
        f"pages={coverage['pages_fetched']} candles={coverage['candles']} "
        f"no_candles={coverage['no_candles']} scored={coverage['scored']} "
        f"2x_rate={coverage['reached_2x_rate']} "
        f"({coverage['reached_2x_hits']}/{coverage['scored']}) "
        f"3x_rate={coverage['reached_3x_rate']} "
        f"({coverage['reached_3x_hits']}/{coverage['scored']})"
    )
    for s in summaries:
        if "winner_share" in s:
            print(
                f"  {s['feature']}: win_share={s['winner_share']} "
                f"lose_share={s['loser_share']} auc={s['auc']} "
                f"base={s['base_rate']} artifact={s['artifact']}"
            )
        else:
            print(
                f"  {s['feature']}: win_med={s.get('winner_median')} "
                f"lose_med={s.get('loser_median')} auc={s['auc']} "
                f"base={s['base_rate']} artifact={s['artifact']}"
            )
    c = contingency
    print(
        f"[2x2 prior-theme-winner-60m x reached-2x] a={c['a_prior_win']} "
        f"b={c['b_prior_loss']} c={c['c_noprior_win']} d={c['d_noprior_loss']} "
        f"rate_prior={c['rate_prior_winner']} rate_noprior={c['rate_no_prior_winner']} "
        f"lift={c['lift']} ci95={c['ci95']} ci_reason={c['ci_reason']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
