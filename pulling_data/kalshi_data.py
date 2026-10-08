import csv
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

SERIES_TICKERS = ["KXHIGHNY"]
START_DATE = datetime(2023, 9, 1, tzinfo=timezone.utc)
END_DATE = datetime(2026, 9, 1, tzinfo=timezone.utc)
CANDLE_PERIOD_MINUTES = 1

TEST_MODE = True
TEST_START_DATE = datetime(2026, 7, 1, tzinfo=timezone.utc)
TEST_END_DATE = datetime(2026, 9, 5, tzinfo=timezone.utc)
TEST_MAX_MARKETS = 5

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
REQUEST_DELAY_SECONDS = 0.05
MAX_RETRIES = 5

DATA_DIR = Path(__file__).parent / "data"
MARKETS_CSV = DATA_DIR / "markets.csv"
TRADES_DIR = DATA_DIR / "trades"
CANDLES_DIR = DATA_DIR / "candles"

def _get(path: str, params: dict | None = None) -> dict:

    url = f"{BASE_URL}{path}"
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=60)
        except requests.RequestException as e:
            wait = 2 ** attempt
            print(f"  [retry] {type(e).__name__} on {path}, waiting {wait}s (attempt {attempt + 1})")
            time.sleep(wait)
            continue
        if resp.status_code == 200:
            time.sleep(REQUEST_DELAY_SECONDS)
            return resp.json()
        if resp.status_code in (429, 500, 502, 503, 504):
            wait = 2 ** attempt
            print(f"  [retry] {resp.status_code} on {path}, waiting {wait}s (attempt {attempt + 1})")
            time.sleep(wait)
            continue
        raise RuntimeError(f"Kalshi API error {resp.status_code} on {url}: {resp.text[:500]}")
    raise RuntimeError(f"Gave up after {MAX_RETRIES} retries on {url}")

def list_markets(series_ticker=None, event_ticker=None, status=None,
                  min_close_ts=None, max_close_ts=None):

    cursor = None
    while True:
        params = {"limit": 1000}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if status:
            params["status"] = status
        if min_close_ts:
            params["min_close_ts"] = min_close_ts
        if max_close_ts:
            params["max_close_ts"] = max_close_ts
        if cursor:
            params["cursor"] = cursor

        data = _get("/markets", params=params)
        for m in data.get("markets", []):
            yield m

        cursor = data.get("cursor")
        if not cursor:
            break

def get_series(series_ticker: str) -> dict:

    data = _get(f"/series/{series_ticker}")
    return data.get("series", data)

def get_market_trades(ticker=None, min_ts=None, max_ts=None):

    cursor = None
    while True:
        params = {"limit": 1000}
        if ticker:
            params["ticker"] = ticker
        if min_ts:
            params["min_ts"] = min_ts
        if max_ts:
            params["max_ts"] = max_ts
        if cursor:
            params["cursor"] = cursor

        data = _get("/markets/trades", params=params)
        trades = data.get("trades", [])
        for t in trades:
            yield t

        cursor = data.get("cursor")
        if not cursor or not trades:
            break

def normalize_trade(raw: dict) -> dict:

    outcome = raw["taker_outcome_side"]
    book_side = raw["taker_book_side"]
    price = float(raw["yes_price_dollars"] if outcome == "yes" else raw["no_price_dollars"])
    side = "sell" if book_side == "bid" else "buy"

    return {
        "trade_id": raw["trade_id"],
        "ticker": raw["ticker"],
        "created_time": raw["created_time"],
        "price_dollars": price,
        "side": side,
        "contracts": float(raw["count_fp"]),
        "is_block_trade": bool(raw.get("is_block_trade", False)),
    }

def get_candlesticks(series_ticker: str, ticker: str, start_ts: int, end_ts: int, period_interval: int = 1) -> list:

    if period_interval not in (1, 60, 1440):
        raise ValueError("period_interval must be 1, 60, or 1440 (minutes)")

    params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval}
    data = _get(f"/series/{series_ticker}/markets/{ticker}/candlesticks", params=params)
    return data.get("candlesticks", [])

def to_ts(dt: datetime) -> int:
    return int(dt.timestamp())

def write_markets_csv(all_markets: list) -> None:
    MARKETS_CSV.parent.mkdir(parents=True, exist_ok=True)
    fields = ["ticker", "series_ticker", "category", "status", "result",
              "open_time", "close_time", "settlement_ts", "latest_expiration_time",
              "yes_bid_dollars", "yes_ask_dollars",
              "no_bid_dollars", "no_ask_dollars"]
    with open(MARKETS_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_markets)
    print(f"Wrote {len(all_markets)} markets -> {MARKETS_CSV}")

def pull_trades_for_market(ticker: str, min_ts: int, max_ts: int) -> None:
    out_path = TRADES_DIR / f"{ticker}.csv"
    if out_path.exists():
        return

    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    rows = [normalize_trade(raw) for raw in get_market_trades(ticker=ticker, min_ts=min_ts, max_ts=max_ts)]

    fields = ["trade_id", "ticker", "created_time", "price_dollars", "side", "contracts", "is_block_trade"]
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"    {ticker}: {len(rows)} trades -> {out_path.name}")

def pull_candles_for_market(series_ticker: str, ticker: str, start_ts: int, end_ts: int) -> None:
    out_path = CANDLES_DIR / f"{ticker}.csv"
    if out_path.exists():
        return

    CANDLES_DIR.mkdir(parents=True, exist_ok=True)
    candles = get_candlesticks(series_ticker, ticker, start_ts, end_ts, period_interval=CANDLE_PERIOD_MINUTES)

    rows = []
    for c in candles:
        rows.append({
            "ticker": ticker,
            "end_period_ts": c.get("end_period_ts"),
            "yes_bid_open": c.get("yes_bid", {}).get("open"),
            "yes_bid_high": c.get("yes_bid", {}).get("high"),
            "yes_bid_low": c.get("yes_bid", {}).get("low"),
            "yes_bid_close": c.get("yes_bid", {}).get("close"),
            "yes_ask_open": c.get("yes_ask", {}).get("open"),
            "yes_ask_high": c.get("yes_ask", {}).get("high"),
            "yes_ask_low": c.get("yes_ask", {}).get("low"),
            "yes_ask_close": c.get("yes_ask", {}).get("close"),
            "volume": c.get("volume"),
            "open_interest": c.get("open_interest"),
        })

    fields = ["ticker", "end_period_ts", "yes_bid_open", "yes_bid_high", "yes_bid_low", "yes_bid_close",
              "yes_ask_open", "yes_ask_high", "yes_ask_low", "yes_ask_close", "volume", "open_interest"]
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"    {ticker}: {len(rows)} candles -> {out_path.name}")

def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    effective_start = TEST_START_DATE if TEST_MODE else START_DATE
    effective_end = TEST_END_DATE if TEST_MODE else END_DATE
    if TEST_MODE:
        print(f"*** TEST_MODE is on — using {effective_start.date()} to {effective_end.date()}, "
              f"capped at {TEST_MAX_MARKETS} markets for trades/candles. Set TEST_MODE = False for a real pull. ***\n")

    print("Step 1: listing markets...")
    all_markets = []
    for series in SERIES_TICKERS:
        try:
            series_info = get_series(series)
            category = series_info.get("category")
        except Exception as e:
            print(f"  !! couldn't fetch category for series {series}: {e}")
            category = None

        markets = list(list_markets(
            series_ticker=series,
            status="settled",
            min_close_ts=to_ts(effective_start),
            max_close_ts=to_ts(effective_end),
        ))
        for m in markets:
            m["series_ticker"] = series
            m["category"] = category
        print(f"  {series}: found {len(markets)} markets in window (category: {category})")
        all_markets.extend(markets)
    write_markets_csv(all_markets)

    markets_to_pull = all_markets[:TEST_MAX_MARKETS] if TEST_MODE else all_markets
    print(f"\nStep 2: pulling trades + candles for {len(markets_to_pull)} of {len(all_markets)} markets...")
    for i, m in enumerate(markets_to_pull, 1):
        ticker = m["ticker"]
        open_dt, close_dt = m.get("open_time"), m.get("close_time")
        if not open_dt or not close_dt:
            continue

        start_ts = int(datetime.fromisoformat(open_dt.replace("Z", "+00:00")).timestamp())
        end_ts = int(datetime.fromisoformat(close_dt.replace("Z", "+00:00")).timestamp())

        print(f"  [{i}/{len(markets_to_pull)}] {ticker}")
        try:
            pull_trades_for_market(ticker, min_ts=start_ts, max_ts=end_ts)
            pull_candles_for_market(m["series_ticker"], ticker, start_ts=start_ts, end_ts=end_ts)
        except Exception as e:
            print(f"    !! failed on {ticker}: {e}")
            continue

    print("\nDone. Check data/markets.csv, data/trades/*.csv, data/candles/*.csv")

def sanity_check():
    import itertools
    markets = list(itertools.islice(list_markets(series_ticker=SERIES_TICKERS[0], status="closed"), 1))
    if not markets:
        print("No closed markets found for that series — try a different series_ticker.")
        return
    ticker = markets[0]["ticker"]
    print(f"Checking trades for {ticker}...")
    for raw in itertools.islice(get_market_trades(ticker=ticker), 5):
        print("RAW:       ", raw)
        print("NORMALIZED:", normalize_trade(raw))
        print()

if __name__ == "__main__":
    raise SystemExit(
        "The test pull is retired. Run pull_sample.py for the 15-market sample."
    )
