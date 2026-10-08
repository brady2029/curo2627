import csv
import json
import os
import sys
from datetime import datetime, timezone

import kalshi_data
import polymarket_data

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_PATH = os.path.join(HERE, "sample_15.json")

KALSHI_DIR = os.path.join(HERE, "data_kalshi")
KALSHI_TRADES = os.path.join(KALSHI_DIR, "trades")
KALSHI_CANDLES = os.path.join(KALSHI_DIR, "candles")

POLY_DIR = polymarket_data.OUT_DIR
POLY_TRADES = polymarket_data.TRADES_DIR
POLY_PRICES = polymarket_data.PRICES_DIR
POLY_SPREADS = os.path.join(POLY_DIR, "spreads")

CANDLE_CHUNK_SECONDS = 120 * 24 * 3600
CANDLE_PERIOD = 60

PRICE_CHUNK_SECONDS = 13 * 24 * 3600
TRADE_PAGE = 2000

def _load_sample():
    with open(SAMPLE_PATH, encoding="utf-8") as f:
        return json.load(f)["markets"]

def _has_rows(path):
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as f:
        next(f, None)
        return next(f, None) is not None

def _kalshi_get(path, params=None):
    return kalshi_data._get(path, params=params)

def fetch_kalshi_market(ticker):

    try:
        data = _kalshi_get(f"/markets/{ticker}")
        market = data.get("market", data)
        if market.get("ticker"):
            return market
    except RuntimeError as e:
        if "404" not in str(e):
            raise
    data = _kalshi_get(f"/historical/markets/{ticker}")
    return data.get("market", data)

def _write_trade_pages(path, ticker, endpoint, seen_ids):

    fields = ["trade_id", "ticker", "created_time", "price_dollars", "side", "contracts", "is_block_trade"]
    new_file = not os.path.exists(path)
    written = 0
    cursor = None
    try:
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            if new_file:
                writer.writeheader()
            while True:
                params = {"limit": 1000, "ticker": ticker}
                if cursor:
                    params["cursor"] = cursor
                data = _kalshi_get(endpoint, params=params)
                batch = data.get("trades") or []
                if not batch:
                    break
                for raw in batch:
                    trade_id = raw.get("trade_id")
                    if trade_id in seen_ids:
                        continue
                    seen_ids.add(trade_id)
                    writer.writerow(kalshi_data.normalize_trade(raw))
                    written += 1
                cursor = data.get("cursor")
                if not cursor:
                    break
    except RuntimeError as e:
        if "404" not in str(e):
            raise
        print(f"    {endpoint} has no trades for this market")
    return written

def pull_kalshi_trades(ticker):
    path = os.path.join(KALSHI_TRADES, f"{ticker}.csv")
    if _has_rows(path):
        print(f"    trades already on disk, skipping")
        return
    os.makedirs(KALSHI_TRADES, exist_ok=True)
    partial = path + ".partial"
    if os.path.exists(partial):
        os.remove(partial)
    seen = set()

    n_hist = _write_trade_pages(partial, ticker, "/historical/trades", seen)
    n_live = _write_trade_pages(partial, ticker, "/markets/trades", seen)
    os.replace(partial, path)
    print(f"    trades: {n_hist + n_live} (historical {n_hist}, live {n_live})")

def _candle_rows(ticker, candles):
    rows = []
    for c in candles:
        rows.append({
            "ticker": ticker,
            "end_period_ts": c.get("end_period_ts"),
            "yes_bid_open": (c.get("yes_bid") or {}).get("open"),
            "yes_bid_high": (c.get("yes_bid") or {}).get("high"),
            "yes_bid_low": (c.get("yes_bid") or {}).get("low"),
            "yes_bid_close": (c.get("yes_bid") or {}).get("close"),
            "yes_ask_open": (c.get("yes_ask") or {}).get("open"),
            "yes_ask_high": (c.get("yes_ask") or {}).get("high"),
            "yes_ask_low": (c.get("yes_ask") or {}).get("low"),
            "yes_ask_close": (c.get("yes_ask") or {}).get("close"),
            "volume": c.get("volume"),
            "open_interest": c.get("open_interest"),
        })
    return rows

def _fetch_candle_chunk(series_ticker, ticker, start_ts, end_ts):
    params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": CANDLE_PERIOD}
    live_path = f"/series/{series_ticker}/markets/{ticker}/candlesticks"
    try:
        data = _kalshi_get(live_path, params=params)
        return data.get("candlesticks") or []
    except RuntimeError as e:
        if "404" not in str(e):
            raise
    data = _kalshi_get(f"/historical/markets/{ticker}/candlesticks", params=params)
    return data.get("candlesticks") or []

def pull_kalshi_candles(series_ticker, ticker, open_time, close_time):
    path = os.path.join(KALSHI_CANDLES, f"{ticker}.csv")
    if _has_rows(path):
        print(f"    candles already on disk, skipping")
        return
    os.makedirs(KALSHI_CANDLES, exist_ok=True)
    partial = path + ".partial"
    start = int(datetime.fromisoformat(open_time.replace("Z", "+00:00")).timestamp())
    end = int(datetime.fromisoformat(close_time.replace("Z", "+00:00")).timestamp())
    fields = ["ticker", "end_period_ts", "yes_bid_open", "yes_bid_high", "yes_bid_low", "yes_bid_close",
              "yes_ask_open", "yes_ask_high", "yes_ask_low", "yes_ask_close", "volume", "open_interest"]
    seen = set()
    n = 0
    with open(partial, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        t = start
        while t < end:
            t2 = min(t + CANDLE_CHUNK_SECONDS, end)
            for row in _candle_rows(ticker, _fetch_candle_chunk(series_ticker, ticker, t, t2)):
                key = row["end_period_ts"]
                if key in seen:
                    continue
                seen.add(key)
                writer.writerow(row)
                n += 1
            t = t2
    os.replace(partial, path)
    print(f"    candles: {n}")

def pull_kalshi(sample):
    os.makedirs(KALSHI_DIR, exist_ok=True)
    market_rows = []
    for i, row in enumerate(sample, 1):
        ticker = row["kalshi"]["ticker"]
        print(f"[kalshi {i}/{len(sample)}] {row['sample_id']} {ticker}")
        market = fetch_kalshi_market(ticker)
        market_rows.append({
            "sample_id": row["sample_id"],
            "study_category": row["category"],
            "activity": row["activity"],
            "question_match": row["question_match"],
            "ticker": market.get("ticker"),
            "series_ticker": row["kalshi"]["series_ticker"],
            "question": market.get("title"),
            "status": market.get("status"),
            "result": market.get("result"),
            "open_time": market.get("open_time"),
            "close_time": market.get("close_time"),
            "settlement_ts": market.get("settlement_ts"),
            "latest_expiration_time": market.get("latest_expiration_time"),
            "volume": market.get("volume_fp"),
            "yes_bid_dollars": market.get("yes_bid_dollars"),
            "yes_ask_dollars": market.get("yes_ask_dollars"),
            "no_bid_dollars": market.get("no_bid_dollars"),
            "no_ask_dollars": market.get("no_ask_dollars"),
        })
        if not market.get("open_time") or not market.get("close_time"):
            print("    !! missing open/close, skipping trades and candles")
            continue
        pull_kalshi_trades(ticker)
        pull_kalshi_candles(row["kalshi"]["series_ticker"], ticker, market["open_time"], market["close_time"])

    path = os.path.join(KALSHI_DIR, "markets.csv")
    fields = list(market_rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(market_rows)
    print(f"Wrote {len(market_rows)} Kalshi markets -> {path}")

def _resolved_outcome(outcomes, prices):
    winner = ""
    for outcome, price in zip(outcomes, prices):
        try:
            if float(price) >= 0.99:
                return outcome
        except (TypeError, ValueError):
            continue
    return winner

def fetch_poly_market(slug):
    raw = polymarket_data._get(polymarket_data.GAMMA_BASE, f"/markets/slug/{slug}")
    return polymarket_data.parse_market(raw), raw

def pull_poly_trades(condition_id, slug, start_ts, end_ts):
    path = os.path.join(POLY_TRADES, f"{slug}.csv")
    if _has_rows(path):
        print("    trades already on disk, skipping")
        return
    os.makedirs(POLY_TRADES, exist_ok=True)
    partial = path + ".partial"
    fields = ["transaction_hash", "wallet", "side", "outcome", "asset_token_id",
              "condition_id", "price", "size", "usdc_size", "timestamp", "title"]
    seen = set()
    end_cursor = end_ts
    n = 0
    resume = _has_rows(partial)
    if resume:
        oldest = None
        with open(partial, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                h = row.get("transaction_hash")
                if h:
                    seen.add(h)
                ts = row.get("timestamp")
                if ts:
                    ts = int(float(ts))
                    if oldest is None or ts < oldest:
                        oldest = ts
                n += 1
        if oldest is not None:
            end_cursor = oldest
        print(f"    resuming from {n} saved trades", flush=True)
    with open(partial, "a" if resume else "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not resume:
            writer.writeheader()
        while True:
            params = {"market": condition_id, "limit": TRADE_PAGE, "offset": 0, "takerOnly": "true"}
            if start_ts is not None:
                params["start"] = start_ts
            if end_cursor is not None:
                params["end"] = end_cursor
            batch = polymarket_data._get(polymarket_data.DATA_API_BASE, "/trades", params=params)
            if not batch:
                break
            min_ts = None
            for raw in batch:
                ts = raw.get("timestamp")
                if ts is not None and (min_ts is None or ts < min_ts):
                    min_ts = ts
                h = raw.get("transactionHash")
                if not h or h in seen:
                    continue
                seen.add(h)
                writer.writerow(polymarket_data.normalize_trade(raw))
                n += 1

            if len(batch) < TRADE_PAGE or min_ts is None:
                break
            nxt = min_ts - 1 if min_ts >= end_cursor else min_ts
            if start_ts is not None and nxt < start_ts:
                break
            if nxt >= end_cursor:
                break
            end_cursor = nxt
            if n % 50000 < len(batch):
                print(f"    ... {n} trades", flush=True)
    os.replace(partial, path)
    print(f"    trades: {n}")

def pull_poly_prices(market, slug, start_ts, end_ts):
    os.makedirs(POLY_PRICES, exist_ok=True)
    token_ids = market.get("clob_token_ids") or []
    outcomes = market.get("outcomes") or []
    for i, token_id in enumerate(token_ids):
        outcome = outcomes[i] if i < len(outcomes) else f"token{i}"
        safe = str(outcome).replace("/", "_").replace(" ", "_")
        path = os.path.join(POLY_PRICES, f"{slug}__{safe}.csv")
        if _has_rows(path):
            print(f"    prices {outcome} already on disk, skipping")
            continue
        seen = set()
        n = 0
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["timestamp", "price"])
            writer.writeheader()
            t = start_ts
            while t < end_ts:
                t2 = min(t + PRICE_CHUNK_SECONDS, end_ts)
                history = polymarket_data.get_price_history(token_id, t, t2, fidelity_minutes=60)
                for point in history:
                    ts = point.get("t")
                    if ts in seen:
                        continue
                    seen.add(ts)
                    writer.writerow({"timestamp": ts, "price": point.get("p")})
                    n += 1
                t = t2
        print(f"    prices {outcome}: {n}")

def _hour(ts):
    return (int(ts) // 3600) * 3600

def write_heuristic_spreads(slug):

    os.makedirs(POLY_SPREADS, exist_ok=True)
    path = os.path.join(POLY_SPREADS, f"{slug}.csv")
    if _has_rows(path):
        print("    spread already on disk, skipping")
        return

    mids = {}
    for name in os.listdir(POLY_PRICES):
        if not name.startswith(slug + "__") or not name.endswith(".csv"):
            continue
        outcome = name[len(slug) + 2:-4]
        by_hour = {}
        with open(os.path.join(POLY_PRICES, name), encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("timestamp") and row.get("price") not in (None, ""):
                    by_hour[_hour(row["timestamp"])] = float(row["price"])
        mids[outcome] = by_hour

    yes_hours = mids.get("Yes") or {}
    sums = {}
    counts = {}
    trade_path = os.path.join(POLY_TRADES, f"{slug}.csv")
    with open(trade_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            outcome = row.get("outcome") or ""
            book = mids.get(outcome)
            if not book or not row.get("timestamp") or row.get("price") in (None, ""):
                continue
            hour = _hour(row["timestamp"])
            mid = book.get(hour)
            if mid is None:
                continue
            spread = 2 * abs(float(row["price"]) - mid)
            sums[hour] = sums.get(hour, 0.0) + spread
            counts[hour] = counts.get(hour, 0) + 1

    hours = sorted(set(yes_hours) | set(sums))
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["timestamp", "mid_yes", "heuristic_effective_spread", "n_trades"]
        )
        writer.writeheader()
        for hour in hours:
            n = counts.get(hour, 0)
            writer.writerow({
                "timestamp": hour,
                "mid_yes": yes_hours.get(hour, ""),
                "heuristic_effective_spread": (sums[hour] / n) if n else "",
                "n_trades": n,
            })
    print(f"    heuristic spread hours: {len(hours)}")

def pull_polymarket(sample):
    os.makedirs(POLY_DIR, exist_ok=True)
    market_rows = []
    for i, row in enumerate(sample, 1):
        slug = row["polymarket"]["slug"]
        print(f"[polymarket {i}/{len(sample)}] {row['sample_id']} {slug}")
        market, raw = fetch_poly_market(slug)
        start = polymarket_data._parse_iso(market.get("start_date"))
        end = polymarket_data._parse_iso(market.get("closed_time")) or polymarket_data._parse_iso(market.get("end_date"))
        if start is None or end is None:
            print("    !! missing start/close, skipping trades and prices")
            start_ts = end_ts = None
        else:
            start_ts, end_ts = polymarket_data.to_ts(start), polymarket_data.to_ts(end)
        market_rows.append({
            "sample_id": row["sample_id"],
            "study_category": row["category"],
            "activity": row["activity"],
            "question_match": row["question_match"],
            "id": market.get("id"),
            "slug": market.get("slug"),
            "condition_id": market.get("condition_id"),
            "question": market.get("question"),
            "outcomes": json.dumps(market.get("outcomes")),
            "outcome_prices": json.dumps(market.get("outcome_prices")),
            "resolved_outcome": _resolved_outcome(market.get("outcomes") or [], market.get("outcome_prices") or []),
            "clob_token_ids": json.dumps(market.get("clob_token_ids")),
            "category": market.get("category"),
            "start_date": market.get("start_date"),
            "end_date": market.get("end_date"),
            "closed_time": market.get("closed_time"),
            "closed": market.get("closed"),
            "active": market.get("active"),
            "volume": market.get("volume"),
            "liquidity": market.get("liquidity"),
        })
        if not market.get("condition_id") or start_ts is None:
            continue
        pull_poly_trades(market["condition_id"], slug, start_ts, end_ts)
        pull_poly_prices(market, slug, start_ts, end_ts)
        write_heuristic_spreads(slug)

    path = os.path.join(POLY_DIR, "markets.csv")
    fields = list(market_rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(market_rows)
    print(f"Wrote {len(market_rows)} Polymarket markets -> {path}")

def main():
    sample = _load_sample()
    if len(sample) != 15:
        raise SystemExit(f"sample_15.json has {len(sample)} markets, expected 15")
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    print(f"Pulling {len(sample)} paired markets ({which})\n", flush=True)
    if which in ("both", "kalshi"):
        pull_kalshi(sample)
    if which in ("both", "polymarket"):
        pull_polymarket(sample)
    print("\nDone.", flush=True)

if __name__ == "__main__":
    main()
