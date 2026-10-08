import csv
import json
import os
import re
import time
from datetime import datetime, timezone

import requests

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"

TAG_ID = None

START_DATE = None
END_DATE = None

CLOSED_ONLY = True

PRICE_HISTORY_FIDELITY_MINUTES = 60

SLEEP_BETWEEN_CALLS = 0.05
MAX_RETRIES = 8

TEST_MODE = True
TEST_START_DATE = datetime(2026, 6, 1, tzinfo=timezone.utc)
TEST_END_DATE = datetime(2026, 9, 5, tzinfo=timezone.utc)
TEST_MAX_MARKETS = 5

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_polymarket")
TRADES_DIR = os.path.join(OUT_DIR, "trades")
PRICES_DIR = os.path.join(OUT_DIR, "prices")

def _get(base_url: str, path: str, params: dict = None) -> dict:

    url = base_url + path
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=90)
        except requests.RequestException as e:
            wait = 2 ** attempt
            print(f"  ! {type(e).__name__} on {url}, retrying in {wait}s...", flush=True)
            time.sleep(wait)
            continue
        if resp.status_code == 200:
            time.sleep(SLEEP_BETWEEN_CALLS)
            return resp.json()
        if resp.status_code in (429, 500, 502, 503, 504):
            wait = 2 ** attempt
            print(f"  ! {resp.status_code} on {url}, retrying in {wait}s...", flush=True)
            time.sleep(wait)
            continue
        raise RuntimeError(f"Polymarket API error {resp.status_code} on {url}: {resp.text}")
    raise RuntimeError(f"Gave up after {MAX_RETRIES} retries on {url}")

def to_ts(dt: datetime) -> int:

    return int(dt.timestamp())

def to_iso(dt: datetime) -> str:

    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

def _parse_iso(s: str):

    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    m = re.search(r'([+-]\d{2})$', s)
    if m:
        s = s + ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def list_markets(closed: bool = True, start_dt: datetime = None, end_dt: datetime = None,
                  tag_id: int = None, limit: int = 100):

    offset = 0
    while True:
        params = {"limit": limit, "offset": offset, "closed": str(closed).lower(),
                  "order": "closedTime", "ascending": "false"}
        if tag_id is not None:
            params["tag_id"] = tag_id

        try:
            batch = _get(GAMMA_BASE, "/markets", params=params)
        except RuntimeError as e:
            if "offset too large" in str(e):
                print("  (hit Gamma's max pagination depth -- stopping here, see caveat #13)")
                break
            raise

        if not batch:
            break

        stop = False
        for raw in batch:
            m = parse_market(raw)
            m_closed = _parse_iso(m["closed_time"])

            if m_closed is None:

                yield m
                continue
            if end_dt and m_closed > end_dt:
                continue
            if start_dt and m_closed < start_dt:
                stop = True
                break

            yield m

        if stop or len(batch) < limit:
            break
        offset += limit

def parse_market(raw: dict) -> dict:

    def _safe_json_list(field_name):
        val = raw.get(field_name)
        if val is None:
            return []
        if isinstance(val, list):
            return val
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return []

    outcomes = _safe_json_list("outcomes")
    outcome_prices = _safe_json_list("outcomePrices")
    clob_token_ids = _safe_json_list("clobTokenIds")

    category = ""
    events = raw.get("events") or []
    if events:
        tags = events[0].get("tags") or []
        if tags:
            category = tags[0].get("label", "")

    return {
        "id": raw.get("id"),
        "slug": raw.get("slug"),
        "condition_id": raw.get("conditionId"),
        "question": raw.get("question"),
        "outcomes": outcomes,
        "outcome_prices": outcome_prices,
        "clob_token_ids": clob_token_ids,
        "category": category,
        "start_date": raw.get("startDateIso") or raw.get("startDate"),
        "end_date": raw.get("endDateIso") or raw.get("endDate"),
        "closed_time": raw.get("closedTime"),
        "closed": raw.get("closed"),
        "active": raw.get("active"),
        "volume": raw.get("volumeNum") or raw.get("volume"),
        "liquidity": raw.get("liquidityNum") or raw.get("liquidity"),
    }

def write_markets_csv(all_markets: list) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "markets.csv")
    fields = ["id", "slug", "condition_id", "question", "outcomes", "outcome_prices",
              "clob_token_ids", "category", "start_date", "end_date", "closed_time",
              "closed", "active", "volume", "liquidity"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for m in all_markets:
            row = dict(m)

            row["outcomes"] = json.dumps(row["outcomes"])
            row["outcome_prices"] = json.dumps(row["outcome_prices"])
            row["clob_token_ids"] = json.dumps(row["clob_token_ids"])
            writer.writerow(row)
    print(f"Wrote {len(all_markets)} markets -> {path}")

def get_market_trades(condition_id: str, start_ts: int = None, end_ts: int = None,
                       page_size: int = 500):

    offset = 0
    while True:
        params = {"market": condition_id, "limit": page_size, "offset": offset,
                  "takerOnly": "true"}
        if start_ts is not None:
            params["start"] = start_ts
        if end_ts is not None:
            params["end"] = end_ts

        batch = _get(DATA_API_BASE, "/trades", params=params)
        if not batch:
            break

        for raw in batch:
            yield normalize_trade(raw)

        if len(batch) < page_size:
            break
        offset += page_size

def normalize_trade(raw: dict) -> dict:

    return {
        "transaction_hash": raw.get("transactionHash"),
        "wallet": raw.get("proxyWallet"),
        "side": raw.get("side"),
        "outcome": raw.get("outcome"),
        "asset_token_id": raw.get("asset"),
        "condition_id": raw.get("conditionId"),
        "price": raw.get("price"),
        "size": raw.get("size"),
        "usdc_size": raw.get("usdcSize") if raw.get("usdcSize") is not None else (
            float(raw["price"]) * float(raw["size"])
            if raw.get("price") is not None and raw.get("size") is not None else None
        ),
        "timestamp": raw.get("timestamp"),
        "title": raw.get("title"),
    }

def pull_trades_for_market(condition_id: str, label: str, start_ts: int = None,
                            end_ts: int = None) -> int:
    os.makedirs(TRADES_DIR, exist_ok=True)
    path = os.path.join(TRADES_DIR, f"{label}.csv")
    fields = ["transaction_hash", "wallet", "side", "outcome", "asset_token_id",
              "condition_id", "price", "size", "usdc_size", "timestamp", "title"]
    count = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for trade in get_market_trades(condition_id, start_ts=start_ts, end_ts=end_ts):
            writer.writerow(trade)
            count += 1
    print(f"    {label}: {count} trades -> {path}")
    return count

def get_price_history(token_id: str, start_ts: int, end_ts: int,
                       fidelity_minutes: int = 60) -> list:

    params = {
        "market": token_id,
        "startTs": start_ts,
        "endTs": end_ts,
        "fidelity": fidelity_minutes,
    }
    data = _get(CLOB_BASE, "/prices-history", params=params)
    return data.get("history", [])

def pull_price_history_for_market(market: dict, label: str, start_ts: int, end_ts: int) -> None:

    os.makedirs(PRICES_DIR, exist_ok=True)
    token_ids = market.get("clob_token_ids") or []
    outcomes = market.get("outcomes") or []

    if not token_ids:
        print(f"    !! no clob_token_ids for {label}, skipping price history")
        return

    for i, token_id in enumerate(token_ids):
        outcome_name = outcomes[i] if i < len(outcomes) else f"token{i}"
        safe_outcome = str(outcome_name).replace("/", "_").replace(" ", "_")
        path = os.path.join(PRICES_DIR, f"{label}__{safe_outcome}.csv")
        try:
            history = get_price_history(token_id, start_ts, end_ts,
                                         fidelity_minutes=PRICE_HISTORY_FIDELITY_MINUTES)
        except RuntimeError as e:
            print(f"    !! failed price history for {label} ({outcome_name}): {e}")
            continue

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["timestamp", "price"])
            writer.writeheader()
            for point in history:
                writer.writerow({"timestamp": point.get("t"), "price": point.get("p")})
        print(f"    {label} ({outcome_name}): {len(history)} price points -> {path}")

def main():
    if TEST_MODE:
        start_dt, end_dt = TEST_START_DATE, TEST_END_DATE
        max_markets = TEST_MAX_MARKETS
        print(f"*** TEST_MODE is on -- using {start_dt.date()} to {end_dt.date()}, "
              f"capped at {max_markets} markets for trades/prices. "
              f"Set TEST_MODE = False for a real pull. ***")
    else:
        if not START_DATE or not END_DATE:
            raise ValueError("Set START_DATE and END_DATE (or TEST_MODE = True) before running.")
        start_dt = _parse_iso(START_DATE)
        end_dt = _parse_iso(END_DATE)
        max_markets = None

    start_ts, end_ts = to_ts(start_dt), to_ts(end_dt)

    print("Step 1: listing markets...")
    all_markets = list(list_markets(
        closed=CLOSED_ONLY,
        start_dt=start_dt,
        end_dt=end_dt,
        tag_id=TAG_ID,
    ))
    print(f"  found {len(all_markets)} markets in window")
    write_markets_csv(all_markets)

    markets_to_pull = all_markets[:max_markets] if max_markets else all_markets
    print(f"Step 2: pulling trades + price history for {len(markets_to_pull)} of {len(all_markets)} markets...")

    for i, m in enumerate(markets_to_pull):
        label = m["slug"] or m["condition_id"] or str(m["id"])
        print(f"  [{i+1}/{len(markets_to_pull)}] {label}")
        if not m["condition_id"]:
            print(f"    !! no condition_id for {label}, skipping")
            continue
        pull_trades_for_market(m["condition_id"], label, start_ts=start_ts, end_ts=end_ts)
        pull_price_history_for_market(m, label, start_ts=start_ts, end_ts=end_ts)

    print(f"Done. Check {OUT_DIR}/markets.csv, {TRADES_DIR}/*.csv, {PRICES_DIR}/*.csv")

if __name__ == "__main__":
    raise SystemExit(
        "The test pull is retired. Run pull_sample.py for the 15-market sample."
    )
