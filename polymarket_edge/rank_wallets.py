#!/usr/bin/env python3
"""
Rank Polymarket wallets by *actual edge*, not win rate.

Win rate is a bad proxy for skill in a prediction market: a wallet that only
buys heavy favorites at 0.95 will "win" most of the time and still bleed
money, while a wallet that buys underdogs at 0.20 and wins a third of the
time can be hugely profitable. What matters is how much better a wallet's
entry price was than the probability that actually played out, weighted by
how much capital it put behind that view.

This script pulls each wallet's resolved positions from Polymarket's public
Data API (https://data-api.polymarket.com) and computes, per wallet:

  - roi_edge      realized PnL / capital risked, dollar-weighted across all
                  resolved positions. This is "edge" in the sense a bettor
                  means it: return on capital, independent of how often you
                  were right.
  - shrunk_edge   roi_edge regularized toward 0 with a pseudo-capital term,
                  so a wallet with one lucky $50 bet doesn't outrank a
                  wallet that has proven a real edge over $500k of volume.
                  This is what the wallets are ranked by, by default.
  - prob_edge     share-weighted (outcome − entry_price), in probability
                  points. This isolates *calibration* skill (edge per unit
                  of exposure) from bet sizing.
  - win_rate      included only for contrast with the above.

Because this environment's network egress is restricted, the exact field
names below are based on Polymarket's documented/observed Data API schema
but have not been exercised against a live response from here. Field access
goes through FIELD-mapped .get() lookups with safe fallbacks, and --debug
dumps a raw record so you can adjust POSITION_FIELDS quickly if Polymarket
has changed something by the time you run this.

Usage:
    python3 rank_wallets.py --wallets 0xabc...,0xdef...
    python3 rank_wallets.py --wallets-file wallets.txt --min-volume 500
    python3 rank_wallets.py --leaderboard 100 --window month
    python3 rank_wallets.py --wallets-file wallets.txt --output json > ranked.json
"""

import argparse
import csv
import json
import sys
import time
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    sys.exit("This script needs the 'requests' package: pip install requests")

DATA_API = "https://data-api.polymarket.com"

# Candidate leaderboard endpoints to try, in order, for --leaderboard.
# Polymarket does not publish a stable leaderboard API contract; if these
# all fail, pass wallet addresses explicitly via --wallets/--wallets-file.
LEADERBOARD_CANDIDATES = [
    "/leaderboard",
    "/v1/leaderboard",
]

# Maps our internal metric names to the position-record field(s) we expect
# from the Data API, in preference order. Adjust here if the live schema
# differs from what's assumed.
POSITION_FIELDS = {
    "size": ["size"],
    "avg_price": ["avgPrice", "averagePrice"],
    "cur_price": ["curPrice", "currentPrice"],
    "cost": ["totalBought", "initialValue"],
    "cash_pnl": ["cashPnl"],
    "realized_pnl": ["realizedPnl"],
    "redeemable": ["redeemable"],
    "title": ["title", "market"],
    "condition_id": ["conditionId"],
}

REQUEST_TIMEOUT = 20
MAX_PAGES = 20
PAGE_LIMIT = 500


def _get_field(record, key):
    for name in POSITION_FIELDS[key]:
        if name in record and record[name] is not None:
            return record[name]
    return None


def _request_json(path, params=None, retries=3):
    url = f"{DATA_API}{path}"
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_err = exc
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"Request to {url} failed after {retries} attempts: {last_err}")


def fetch_positions(wallet):
    """Fetch all positions for a wallet, paginated."""
    positions = []
    offset = 0
    for _ in range(MAX_PAGES):
        params = {"user": wallet, "limit": PAGE_LIMIT, "offset": offset}
        page = _request_json("/positions", params)
        if not page:
            break
        positions.extend(page)
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    return positions


def fetch_leaderboard(window, limit):
    params = {"window": window, "limit": limit, "orderBy": "pnl"}
    last_err = None
    for path in LEADERBOARD_CANDIDATES:
        try:
            data = _request_json(path, params)
            wallets = []
            for row in data:
                addr = row.get("proxyWallet") or row.get("user") or row.get("wallet")
                if addr:
                    wallets.append(addr)
            if wallets:
                return wallets
        except Exception as exc:  # noqa: BLE001 - trying multiple candidates
            last_err = exc
            continue
    raise RuntimeError(
        "Could not fetch a leaderboard from any known endpoint "
        f"({LEADERBOARD_CANDIDATES}). Last error: {last_err}. "
        "Pass wallet addresses explicitly with --wallets/--wallets-file instead."
    )


def is_resolved(pos):
    cur_price = _get_field(pos, "cur_price")
    if cur_price is None:
        return False
    redeemable = _get_field(pos, "redeemable")
    if redeemable is True:
        return True
    # A market is settled when its current price has collapsed to (near) 0 or 1.
    return cur_price <= 0.001 or cur_price >= 0.999


def outcome_of(pos):
    cur_price = _get_field(pos, "cur_price")
    return 1.0 if cur_price >= 0.5 else 0.0


def position_pnl_and_cost(pos):
    cost = _get_field(pos, "cost") or 0.0
    cash_pnl = _get_field(pos, "cash_pnl") or 0.0
    realized_pnl = _get_field(pos, "realized_pnl") or 0.0
    pnl = cash_pnl + realized_pnl
    return pnl, cost


def compute_wallet_edge(wallet, positions, shrinkage_capital):
    resolved = [p for p in positions if is_resolved(p)]

    total_cost = 0.0
    total_pnl = 0.0
    wins = 0
    weighted_prob_edge_num = 0.0
    total_size = 0.0

    for p in resolved:
        pnl, cost = position_pnl_and_cost(p)
        total_cost += cost
        total_pnl += pnl

        outcome = outcome_of(p)
        avg_price = _get_field(p, "avg_price") or 0.0
        size = _get_field(p, "size") or 0.0
        if outcome == 1.0:
            wins += 1
        weighted_prob_edge_num += size * (outcome - avg_price)
        total_size += size

    n = len(resolved)
    roi_edge = (total_pnl / total_cost) if total_cost > 0 else 0.0
    shrunk_edge = total_pnl / (total_cost + shrinkage_capital)
    prob_edge = (weighted_prob_edge_num / total_size) if total_size > 0 else 0.0
    win_rate = (wins / n) if n > 0 else 0.0

    return {
        "wallet": wallet,
        "resolved_trades": n,
        "volume": round(total_cost, 2),
        "realized_pnl": round(total_pnl, 2),
        "win_rate": round(win_rate, 4),
        "roi_edge": round(roi_edge, 4),
        "shrunk_edge": round(shrunk_edge, 4),
        "prob_edge": round(prob_edge, 4),
    }


def load_wallets(args):
    wallets = []
    if args.wallets:
        wallets.extend(a.strip() for a in args.wallets.split(",") if a.strip())
    if args.wallets_file:
        with open(args.wallets_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    wallets.append(line.split(",")[0].strip())
    if args.leaderboard:
        wallets.extend(fetch_leaderboard(args.window, args.leaderboard))
    # de-dupe, preserve order
    seen = set()
    unique = []
    for w in wallets:
        wl = w.lower()
        if wl not in seen:
            seen.add(wl)
            unique.append(w)
    return unique


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wallets", help="Comma-separated wallet addresses")
    parser.add_argument("--wallets-file", help="File with one wallet address per line")
    parser.add_argument("--leaderboard", type=int, help="Pull the top N wallets from Polymarket's leaderboard instead of / in addition to an explicit list")
    parser.add_argument("--window", default="all", choices=["day", "week", "month", "all"], help="Leaderboard window (default: all)")
    parser.add_argument("--min-trades", type=int, default=5, help="Minimum resolved positions required to be ranked (default: 5)")
    parser.add_argument("--min-volume", type=float, default=100.0, help="Minimum USD volume across resolved positions required to be ranked (default: 100)")
    parser.add_argument("--shrinkage-capital", type=float, default=500.0, help="Pseudo-capital added to the denominator of shrunk_edge to discount small samples (default: 500)")
    parser.add_argument("--top", type=int, default=25, help="Number of wallets to output (default: 25)")
    parser.add_argument("--output", choices=["table", "csv", "json"], default="table")
    parser.add_argument("--debug", action="store_true", help="Print one raw position record per wallet to stderr for schema inspection")
    args = parser.parse_args()

    if not (args.wallets or args.wallets_file or args.leaderboard):
        parser.error("Provide --wallets, --wallets-file, and/or --leaderboard")

    wallets = load_wallets(args)
    if not wallets:
        sys.exit("No wallet addresses to rank.")

    results = []
    for wallet in wallets:
        try:
            positions = fetch_positions(wallet)
        except Exception as exc:  # noqa: BLE001
            print(f"warning: failed to fetch positions for {wallet}: {exc}", file=sys.stderr)
            continue

        if args.debug and positions:
            print(f"--- raw sample position for {wallet} ---", file=sys.stderr)
            print(json.dumps(positions[0], indent=2), file=sys.stderr)

        row = compute_wallet_edge(wallet, positions, args.shrinkage_capital)
        if row["resolved_trades"] < args.min_trades or row["volume"] < args.min_volume:
            continue
        results.append(row)

    results.sort(key=lambda r: r["shrunk_edge"], reverse=True)
    results = results[: args.top]

    if not results:
        sys.exit("No wallets met --min-trades/--min-volume thresholds after fetching.")

    if args.output == "json":
        print(json.dumps(results, indent=2))
    elif args.output == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)
    else:
        headers = ["#", "wallet", "shrunk_edge", "roi_edge", "prob_edge", "win_rate", "resolved_trades", "volume", "realized_pnl"]
        widths = [3, 14, 12, 10, 10, 9, 8, 12, 13]
        print(" ".join(h.ljust(w) for h, w in zip(headers, widths)))
        for i, r in enumerate(results, 1):
            wallet_short = r["wallet"][:12] + ".." if len(r["wallet"]) > 14 else r["wallet"]
            print(" ".join(str(v).ljust(w) for v, w in zip(
                [i, wallet_short, r["shrunk_edge"], r["roi_edge"], r["prob_edge"],
                 r["win_rate"], r["resolved_trades"], r["volume"], r["realized_pnl"]],
                widths,
            )))


if __name__ == "__main__":
    main()
