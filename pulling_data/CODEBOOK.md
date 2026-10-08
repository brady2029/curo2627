# Data Codebook — Kalshi & Polymarket Pulls

Plain-language guide to what `kalshi_data.py` and `polymarket_data.py` actually pull, what every column means, and what's still open. Written so you can read this instead of re-reading the code every time you forget what a column is. This also doubles as a rough draft for your Week 6 "Data Pipeline and Cleaning Report" deliverable — steal from it freely.

---

## The big picture

Both scripts do the same three-step thing, just against different APIs:

1. **List markets** — get the metadata for the 15 analysis markets (what it's about, when it opened/closed, who won) and save it to one `markets.csv`.
2. **Pull trades** — every individual trade for those markets, one CSV per market.
3. **Pull price/bid-ask history** — for the same markets, get how the price (or bid/ask) moved over time, one CSV per market.

Everything lands in `data_kalshi/` and `data_polymarket/`. The analysis sample is the 15 paired markets in `sample_15.json` (5 political, 5 economic, 5 sports). `pull_sample.py` is what pulls them. The old weather/test files are not part of the sample. `kalshi_data.py` and `polymarket_data.py` still hold the API helpers, but running them directly no longer does the test pull.

A couple of general things worth knowing before the column-by-column stuff:

- **"Timestamp"** just means a specific point in time. Most of these come as either a human-readable date-and-time string (like `2026-09-04T05:00:00Z`, where the `Z` means UTC time zone) or a plain number called a "Unix timestamp" (seconds since Jan 1, 1970 — ugly to read, easy for code to do math on).
- **CSV** = a spreadsheet file, basically. Open any of these in Excel/Numbers/Google Sheets and it'll look like a normal table.
- If a script crashes partway through a real pull, re-running it **picks up where it left off** instead of starting over — it skips any market whose trade/price file already exists on disk.

---

## Kalshi (`kalshi_data.py`)

Kalshi is the centralized, regulated one — no wallet addresses, just anonymous trade records.

### `data_kalshi/markets.csv` — one row per market

| Column | Plain meaning | Why we need it |
|---|---|---|
| `ticker` | The market's unique ID (e.g. `KXHIGHNY-26SEP03-B85.5`) | Primary key — links this market to its trades/candles files |
| `series_ticker` | The broader series this market belongs to (e.g. `KXHIGHNY` = "highest temp in NY") | Groups related markets together |
| `category` | Topic area Kalshi assigns (Politics, Climate and Weather, etc.) | Lets you filter/compare by topic later |
| `status` | Where the market is in its lifecycle (`open`, `closed`, `settled`, `finalized`) | Confirms it's actually done trading before you trust the data |
| `result` | Which side won — `yes` or `no` | **This is your "resolution outcome"** — needed for a bunch of your hypotheses |
| `open_time` | When the market started trading | One of the three timestamps your proposal asks for |
| `close_time` | When trading stopped | Second of the three |
| `settlement_ts` | When Kalshi officially settled/resolved it (can be a bit after close_time) | Third of the three — the *actual* resolution moment, not just when trading ended |
| `latest_expiration_time` | The furthest-out deadline Kalshi allows for this market | Minor — mostly just metadata |
| `yes_bid_dollars` / `yes_ask_dollars` / `no_bid_dollars` / `no_ask_dollars` | The best bid/ask prices **at the moment we pulled this list** | A single snapshot — NOT the full history (that's what the candles files are for) |

### `data_kalshi/trades/<ticker>.csv` — one row per individual trade

| Column | Plain meaning |
|---|---|
| `trade_id` | Unique ID for this specific trade |
| `ticker` | Which market it happened in |
| `created_time` | When the trade happened |
| `price_dollars` | Price paid, in dollars (Kalshi contracts trade between $0.00 and $1.00) |
| `side` | `buy` or `sell` — **see the honesty note below, this one's a best guess** |
| `contracts` | How many contracts changed hands (the "size" of the trade) |
| `is_block_trade` | Whether this was a large negotiated trade instead of a normal market order |

**Honesty note on `side`:** Kalshi's raw trade data doesn't literally hand you a clean "buy" or "sell" label the way you'd expect. The script infers it from which price the trade happened closer to (a trade near the ask price looks like a "buy," near the bid looks like a "sell"). This is a standard technique, but it's an inference, not a guarantee — worth spot-checking a sample against what you'd expect before trusting it at scale.

### `data_kalshi/candles/<ticker>.csv` — price history over time (your "bid/ask at various timestamps" data)

| Column | Plain meaning |
|---|---|
| `end_period_ts` | The timestamp this row's data covers (each row = one time bucket, e.g. one minute) |
| `yes_bid_open/high/low/close` | The best "yes" bid price at the start/highest/lowest/end of this time bucket |
| `yes_ask_open/high/low/close` | Same, but for the "yes" ask price |
| `last_price_open/high/low/close/mean` | The actual last-traded price during this bucket (separate from bid/ask — useful as a sanity check against your G-M predicted spread) |
| `volume` | How many contracts traded during this bucket |
| `open_interest` | How many contracts were still outstanding (not yet resolved) at the end of this bucket |

This was **broken until recently** — it was pulling real data but writing every bid/ask cell as blank because of a wrong field name in the code. That's fixed now; the old blank files were moved to `candles_stale_backup/` and need to be re-pulled fresh.

---

## Polymarket (`polymarket_data.py`)

Polymarket is the decentralized one — every trade is tied to a public blockchain wallet address, which is what makes the institutional-vs-retail wallet classifier possible here (and not on Kalshi).

### `data_polymarket/markets.csv` — one row per market

| Column | Plain meaning | Why we need it |
|---|---|---|
| `id` / `slug` / `condition_id` | Three different ways Polymarket identifies the same market | `condition_id` is what links this market to its trades |
| `question` | The actual market question in plain English (e.g. "Will X win?") | Human-readable context |
| `outcomes` | The possible outcomes (usually `["Yes", "No"]`) | |
| `outcome_prices` | The final settled price for each outcome (near 1.00 = won, near 0.00 = lost) | Raw material for figuring out who won |
| `resolved_outcome` | Which outcome actually won, spelled out (e.g. `"Yes"`) | **Your "resolution outcome"** — computed from outcome_prices since Polymarket doesn't hand you this directly |
| `clob_token_ids` | The internal IDs for each outcome's tradeable token | Needed to pull that outcome's price history |
| `category` | Best-effort topic label | Same idea as Kalshi's category, just less reliable here (see gaps below) |
| `start_date` | When the market opened | First of your three timestamps |
| `end_date` | The market's **originally scheduled** close date | NOT always when it actually closed — see gaps below |
| `closed_time` | When the market **actually** closed/resolved | This is the one that reflects reality — use this over `end_date` |
| `closed` / `active` | True/false flags for market status | |
| `volume` / `liquidity` | Total dollar trading volume / available liquidity | General market-size context |

### `data_polymarket/trades/<label>.csv` — one row per individual trade

| Column | Plain meaning |
|---|---|
| `transaction_hash` | The blockchain transaction ID — unique per trade, good for spotting duplicates |
| `wallet` | The trader's wallet address (this is what feeds your institutional/retail classifier) |
| `side` | `BUY` or `SELL` — this one IS given directly by Polymarket, no guessing needed |
| `outcome` | Which outcome they traded (e.g. `"Yes"`) |
| `asset_token_id` | Which specific outcome-token this trade was in |
| `condition_id` | Which market this trade belongs to |
| `price` | Price paid, 0.00–1.00 |
| `size` | Number of shares/contracts traded |
| `usdc_size` | The **dollar value** of the trade (price × size) | Needed for your classifier's Feature 2 and Feature 3 |
| `timestamp` | When the trade happened |
| `title` | The market's title, for readability |

### `data_polymarket/prices/<label>__<outcome>.csv` — price history over time

| Column | Plain meaning |
|---|---|
| `timestamp` | Point in time |
| `price` | The price of that outcome's token at that moment |

Polymarket does not archive the order book. The price files are a midpoint: at each hour, the Yes price and the No price sum to 1. That gap is not a spread.

### `data_polymarket/spreads/<slug>.csv` — heuristic bid-ask spread

| Column | Plain meaning |
|---|---|
| `timestamp` | Start of the hour |
| `mid_yes` | Yes midpoint in that hour |
| `heuristic_effective_spread` | Average, over trades in that hour, of `2 * abs(trade price − that outcome's midpoint)` |
| `n_trades` | How many trades fell in the hour and had a midpoint |

This is an effective-spread heuristic, not the quoted book. Kalshi candles are the actual bid and ask.

---

## What's confirmed working vs. still a guess

**Confirmed by actually running the scripts and checking the output:**
- Market listing, category tagging, and the settled/closed filtering — working on both platforms.
- Trades pulling correctly on both platforms (including wallet addresses on Polymarket).
- Kalshi candles now populate real bid/ask values (after the recent fix — worth a final re-check).
- Polymarket price history pulls correctly per outcome token.

**Still assumptions, worth spot-checking before you trust them at scale:**
- Kalshi's `side` (buy/sell) is inferred from price proximity, not given directly.
- Polymarket's `usdc_size` is computed as price × size; we couldn't 100% confirm Polymarket's API hands you an equivalent field directly under a different name, though the math is the same either way.
- Polymarket's `resolved_outcome` is inferred from settled prices (≥0.99 = winner), not a field Polymarket gives you directly.
- Polymarket's `category` is pulled from the market's parent event and will be blank sometimes — it's not as clean/reliable as Kalshi's.

---

## What we still need to add (real gaps, not bugs)

1. **News event timestamps.** Your Feature 5 (reaction time to news) and the VPIN-around-news-events analysis both need a list of *when actual news events happened*. Neither API gives you this — it's not their job. This has to come from somewhere else (a news API, or a hand-curated list of the specific events you're studying).
2. **Sample is set.** The pull is the 15 markets in `sample_15.json`, not a test window and not every market in a series. High versus low activity is ranked on Kalshi contract volume (3 high and 2 low inside each category). Fed pairs are the same FOMC meeting with different contract wording on each platform; that is noted on each row as `question_match`.
3. **The wallet-labeling step.** The classifier needs ~50–100 hand-labeled wallets (institutional vs. retail) to train on, per your proposal. That's a manual step that happens after data pulling, not something either script does.
4. **VPIN bucketing code.** Not written yet — this takes the trades CSVs and groups them into volume buckets (e.g. every $1,000 traded = one bucket) to compute VPIN.
5. **The Glosten-Milgrom MLE alpha estimation.** Also not written yet — this is the step that actually estimates informed-trading probability from the trade sequences.

Items 4 and 5 are your next real coding milestones once the data pull itself is solid — happy to help whenever you're ready to start on those.

---

## Quick glossary

- **API** — a way for one program to ask another program (here, Kalshi's or Polymarket's servers) for data over the internet.
- **Endpoint** — a specific "address" within an API you send a request to (e.g. the one that lists markets vs. the one that lists trades).
- **JSON** — the text format APIs usually send data back in; the script converts it into the CSV rows you see.
- **Pagination** — APIs usually won't hand you 10,000 results in one shot; they give you a page at a time (e.g. 100 at a time), and you ask for more pages until you've got everything.
- **Ticker / condition_id** — a market's unique name/ID, same idea as a stock ticker.
- **Bid/ask spread** — the gap between the highest price a buyer will pay (bid) and the lowest price a seller will accept (ask). Narrower spread = more agreement on price / less uncertainty.
- **Wallet address** — on Polymarket (which runs on a blockchain), every trader has a public address instead of a username. Anyone can see what a wallet has traded, which is what makes the institutional/retail classifier possible there.
