# Polymarket wallet edge ranker

Ranks Polymarket wallets by **actual edge**, not win rate.

## Why not win rate

Win rate ignores price. A wallet that only buys favorites at $0.95 wins
constantly and can still lose money; a wallet that buys underdogs at $0.20
and wins a third of the time can be very profitable. What matters is how
much better a wallet's entry price was than the probability that actually
played out, weighted by how much money was behind that view.

## Metrics computed per wallet

| Metric | Meaning |
|---|---|
| `roi_edge` | Realized PnL ÷ capital risked, dollar-weighted across all resolved positions. This is "edge" the way a bettor means it — return on capital. |
| `shrunk_edge` | `roi_edge` regularized toward 0 with a pseudo-capital term (`--shrinkage-capital`, default $500) added to the denominator, so a wallet with one lucky $50 bet can't outrank one with a proven edge over $500k of volume. **Wallets are ranked by this.** |
| `prob_edge` | Share-weighted `(outcome − entry_price)` in probability points. Isolates calibration skill from bet sizing. |
| `win_rate` | Included for contrast only — this is the number the ranking deliberately does *not* use. |

## Usage

```bash
pip install -r requirements.txt

# Explicit wallets
python3 rank_wallets.py --wallets 0xabc...,0xdef...

# From a file (one address per line)
python3 rank_wallets.py --wallets-file wallets.txt

# Auto-discover candidates from Polymarket's leaderboard, then rank those by edge
python3 rank_wallets.py --leaderboard 100 --window month

# JSON/CSV output
python3 rank_wallets.py --wallets-file wallets.txt --output json > ranked.json
```

Useful flags: `--min-trades` / `--min-volume` filter out wallets with too
little history to trust, `--shrinkage-capital` controls how aggressively
small samples are discounted, `--top` caps how many rows are printed.

## Data source & a note on verification

Data comes from Polymarket's public Data API (`https://data-api.polymarket.com`),
specifically the `/positions` endpoint for resolved position history. This
sandbox's network egress does not permit reaching `polymarket.com`, so the
field mapping (`POSITION_FIELDS` in `rank_wallets.py`) is based on
Polymarket's documented/observed schema but has **not** been exercised
against a live response from this environment. Run with `--debug` to dump a
raw record to stderr and quickly adjust `POSITION_FIELDS` if the live schema
has drifted.

The `--leaderboard` auto-discovery tries a couple of candidate endpoint
paths and may not resolve — if it fails, pass wallet addresses directly via
`--wallets`/`--wallets-file`.
