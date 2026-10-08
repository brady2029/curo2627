import json
import random
import re
from datetime import datetime, timedelta, timezone

import requests

import kalshi_data

SEED = 42
WAVE_SIZE = 40
MAX_WAVES = 10
MARKETS_PER_SERIES = 4
WINDOW_START = datetime(2023, 9, 1, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 9, 29, 23, 59, tzinfo=timezone.utc)
MIN_VOLUME = 1000
MAX_VOLUME = 5_000_000
DATE_TOLERANCE = timedelta(days=60)
OUT = "sample_15.json"

CATEGORY = {
    "Politics": "political",
    "Elections": "political",
    "Economics": "economic",
    "Sports": "sports",
}
STOP = set("""
will the a an of to in on be by for and or win wins winning after following
this that market yes no above below than more least exactly between during
have has was were from with into over under their there about who what
when where which contract price prices point points percent election elections
primary president presidential donald trump biden harris rate rates before after
january february march april may june july august september october november december
jan feb mar apr jun jul aug sep oct nov dec
""".split())

def tokens(text):
    out = set()
    for t in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if t.startswith("democrat"):
            t = "democrat"
        if t in STOP or len(t) <= 2 or t.isdigit():
            continue
        out.add(t)
    return out

def parse_dt(value):
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    if re.search(r"[+-]\d{2}$", s):
        s += ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def years(text):
    return {int(y) for y in re.findall(r"20\d\d", text or "")}

def all_series():
    pools = {"political": [], "economic": [], "sports": []}
    cursor = None
    seen = set()
    while True:
        params = {"limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = kalshi_data._get("/series", params=params)
        batch = data.get("series") or []
        for s in batch:
            ticker = s.get("ticker") or ""
            study = CATEGORY.get(s.get("category"))
            if ticker and study and ticker not in seen:
                seen.add(ticker)
                pools[study].append(ticker)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    rng = random.Random(SEED)
    for study, tickers in pools.items():
        rng.shuffle(tickers)
        print(f"{study}: {len(tickers)} series in random order", flush=True)
    return pools, rng

def markets_for_series(series_ticker):
    rows = []
    for path, params in (
        ("/historical/markets", {"series_ticker": series_ticker}),
        ("/markets", {"series_ticker": series_ticker, "status": "settled"}),
    ):
        cursor = None
        pages = 0
        while pages < 8:
            q = {"limit": 1000, **params}
            if cursor:
                q["cursor"] = cursor
            try:
                data = kalshi_data._get(path, params=q)
            except RuntimeError as e:
                if "404" not in str(e):
                    raise
                break
            batch = data.get("markets") or []
            rows.extend(batch)
            cursor = data.get("cursor")
            pages += 1
            if not cursor or not batch:
                break
    return rows

def eligible(series_ticker, rng):
    picked = []
    seen = set()
    for m in markets_for_series(series_ticker):
        ticker = m.get("ticker")
        if not ticker or ticker in seen:
            continue
        closed = parse_dt(m.get("close_time"))
        if closed is None or closed < WINDOW_START or closed > WINDOW_END:
            continue
        mtype = m.get("market_type")
        if mtype and mtype != "binary":
            continue
        try:
            vol = float(m.get("volume_fp") or 0)
        except (TypeError, ValueError):
            vol = 0.0
        if vol < MIN_VOLUME or vol > MAX_VOLUME:
            continue
        seen.add(ticker)
        picked.append({
            "ticker": ticker,
            "series_ticker": series_ticker,
            "question": m.get("title") or "",
            "yes_sub_title": m.get("yes_sub_title") or "",
            "volume": vol,
            "close_time": m.get("close_time"),
        })
    rng.shuffle(picked)
    return picked[:MARKETS_PER_SERIES]

def search_events(query):
    try:
        resp = requests.get(
            "https://gamma-api.polymarket.com/public-search",
            params={"q": query[:160]},
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return []
    return (resp.json().get("events") or [])[:8]

PARAPHRASE = {"nominee", "primary", "nomination", "democratic", "republican", "democrat"}
MONTH_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

SYNONYM = {
    "basketball": "nba",
    "football": "nfl",
    "baseball": "mlb",
    "hockey": "nhl",
}

def _long_tokens_covered(kalshi_q, poly_q):

    soft = PARAPHRASE | {"winner", "attend", "finish", "picked", "drafted", "governor", "senate"}
    needed = {t for t in tokens(kalshi_q) if len(t) >= 4 and t not in soft}
    poly = tokens(poly_q)
    raw = poly_q.lower()
    for word in needed:
        if word in poly:
            continue
        alt = SYNONYM.get(word)
        if alt and alt in raw:
            continue
        return False
    return True

def _same_round(a, b):
    ra = re.findall(r"round\s*(\d+)", a.lower())
    rb = re.findall(r"round\s*(\d+)", b.lower())
    if not ra and not rb:
        return True
    return ra == rb

def _month_days(text):
    found = set()
    low = text.lower()
    for mon, day in re.findall(r"\d{2}(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)(\d{2})", low):
        found.add((MONTH_NUM[mon], int(day)))
    for mon, num in MONTH_NUM.items():
        for day in re.findall(rf"{mon}[a-z]*\s+(\d{ 1,2} )", low):
            d = int(day)
            if 1 <= d <= 31:
                found.add((num, d))
    for mon, day in re.findall(r"20\d\d-(\d{2})-(\d{2})", low):
        found.add((int(mon), int(day)))
    return found

RELEASE_WORDS = ("unemployment", "payroll", "jobless", "inflation", "nonfarm", "cpi")
FULL_MONTH = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

def _months_named(text):
    low = text.lower()
    found = {num for name, num in FULL_MONTH.items() if name in low}
    for ab, num in MONTH_NUM.items():
        if re.search(rf"\b{ab}\b", low) or re.search(rf"\d{ 2} {ab}", low):
            found.add(num)
    return found

def _core_conflict(a, b):
    return ("core" in a.lower()) != ("core" in b.lower())

def _measure_conflict(a, b):

    def mom(text):
        low = text.lower()
        return "month-over-month" in low or "month over month" in low or re.search(r"\bmom\b", low) is not None
    def yoy(text):
        low = text.lower()
        return "year-over-year" in low or "year over year" in low or "yoy" in low
    def ou(text):
        return re.search(r"\bo/u\b|\bover/under\b|\bover under\b", text.lower()) is not None
    def winner(text):
        return re.search(r"\bwinner\b", text.lower()) is not None
    return (mom(a) and yoy(b)) or (yoy(a) and mom(b)) or (winner(a) and ou(b)) or (ou(a) and winner(b))

def _month_conflict(a, b):
    ma, mb = _months_named(a), _months_named(b)
    if not ma or not mb:
        return False
    return ma.isdisjoint(mb)

def _outcome_conflict(a, b):
    def kind(text):
        low = text.lower()
        if re.search(r"\bdraw\b|\btie\b", low):
            return "draw"
        if re.search(r"\bwinner\b|\bwin\b", low):
            return "win"
        return None
    ka, kb = kind(a), kind(b)
    return ka is not None and kb is not None and ka != kb

def _gender_conflict(a, b):
    def flags(text):
        low = text.lower()
        women = re.search(r"\bwomen\b|\bwoman\b", low) is not None
        men = re.search(r"\bmen\b|\bman\b", low) is not None
        return women, men
    aw, am = flags(a)
    bw, bm = flags(b)
    return (aw and bm) or (am and bw)

def _same_release(market, question):

    blob = f"{market['question']} {market['ticker']}"
    low_k, low_p = blob.lower(), question.lower()
    if not any(word in low_k and word in low_p for word in RELEASE_WORDS):
        return False
    mk, mp = _months_named(blob), _months_named(question)
    if not mk or not mp or mk.isdisjoint(mp):
        return False
    yk, yp = years(market["question"]), years(question)
    m = re.search(r"-(\d{2})[A-Z]{3}", market["ticker"])
    if m:
        yk = yk or {2000 + int(m.group(1))}
    if yk and yp and yk.isdisjoint(yp):
        return False
    return True

def _same_month_day(a, b):
    da, db = _month_days(a), _month_days(b)
    if not da or not db:
        return True
    return not da.isdisjoint(db)

def poly_match(market, used_slugs):

    sub = (market.get("yes_sub_title") or "").strip()
    events = []
    seen_slugs = set()
    for event in search_events(market["question"][:140]):
        slug = event.get("slug")
        if not slug or slug in seen_slugs or not event.get("closed"):
            continue
        seen_slugs.add(slug)
        events.append(event)
    kalshi_when = parse_dt(market["close_time"])
    kalshi_text = f"{market['question']} {sub}"
    kalshi_toks = tokens(kalshi_text)
    kalshi_years = years(market["question"])
    best = None
    for event in events:
        event_when = parse_dt(event.get("endDate") or event.get("closedTime"))
        if kalshi_when and event_when and abs(kalshi_when - event_when) > DATE_TOLERANCE:
            continue
        for pm in event.get("markets") or []:
            if not pm.get("closed") or not pm.get("slug") or pm["slug"] in used_slugs:
                continue
            try:
                pvol = float(pm.get("volumeNum") or 0)
            except (TypeError, ValueError):
                pvol = 0.0
            if pvol <= 0 or pvol > MAX_VOLUME:
                continue
            question = pm.get("question") or ""
            if _gender_conflict(market["question"], question) or _outcome_conflict(market["question"], question):
                continue
            if _month_conflict(f"{market['question']} {market['ticker']}", question):
                continue
            if _measure_conflict(market["question"], question) or _core_conflict(market["question"], question):
                continue
            poly_years = years(question)
            if kalshi_years and poly_years and kalshi_years.isdisjoint(poly_years):
                continue
            if _same_release(market, question):
                if best is None or best[0] < 10:
                    best = (10, {
                        "slug": pm["slug"],
                        "question": question,
                        "volume": pvol,
                        "overlap": 10,
                    })
                continue
            poly_toks = tokens(question)
            overlap = kalshi_toks & poly_toks
            union = kalshi_toks | poly_toks
            if len(overlap) < 2 or not union or len(overlap) / len(union) < 0.4:
                continue
            if not _long_tokens_covered(market["question"], question):
                continue
            if not _same_round(market["question"], question):
                continue
            if not _same_month_day(market["ticker"] + " " + market["question"], question):
                continue
            if sub and not _name_in(sub, question) and len(sub) >= 8:
                continue
            if best is None or len(overlap) > best[0]:
                best = (len(overlap), {
                    "slug": pm["slug"],
                    "question": question,
                    "volume": pvol,
                    "overlap": len(overlap),
                })
    return best[1] if best else None

def _name_in(sub, question):
    q = re.sub(r"[^a-z0-9]+", " ", question.lower())
    parts = sub.split()
    if len(parts[-1]) == 1 and len(parts) > 1:
        city = re.sub(r"[^a-z0-9]+", " ", " ".join(parts[:-1]).lower()).strip()
        if city not in q:
            return False
        after = q.split(city, 1)[1].split()
        return bool(after) and after[0].startswith(parts[-1].lower())
    name = re.sub(r"[^a-z0-9]+", " ", sub.lower()).strip()
    return len(name) >= 4 and name in q

def draw():
    series_by_study, rng = all_series()
    frozen_median = {}

    strata = {k: {"high": [], "low": []} for k in ("political", "economic", "sports")}
    chosen = []
    used = set()
    kept = {k: {"high": 0, "low": 0} for k in strata}
    for wave in range(MAX_WAVES):
        for study in ("political", "economic", "sports"):
            if kept[study]["high"] >= 3 and kept[study]["low"] >= 2:
                continue
            start = wave * WAVE_SIZE
            chunk = series_by_study[study][start:start + WAVE_SIZE]
            added = []
            for series in chunk:
                added.extend(eligible(series, rng))
            print(f"wave {wave + 1} {study}: +{len(added)} markets", flush=True)
            if study not in frozen_median and added:
                vols = sorted(m["volume"] for m in added)
                frozen_median[study] = vols[len(vols) // 2]
                print(f"  {study} median frozen at {frozen_median[study]:.0f}", flush=True)
            if study not in frozen_median:
                continue
            med = frozen_median[study]
            high = [m for m in added if m["volume"] > med]
            low = [m for m in added if m["volume"] <= med]
            rng.shuffle(high)
            rng.shuffle(low)
            strata[study]["high"].extend(high)
            strata[study]["low"].extend(low)
            _take(study, strata, kept, chosen, used, med)
        if all(kept[s]["high"] >= 3 and kept[s]["low"] >= 2 for s in kept):
            break
    for study in kept:
        for activity, need in (("high", 3), ("low", 2)):
            if kept[study][activity] < need:
                raise SystemExit(
                    f"{study} {activity}: matched {kept[study][activity]} of {need}"
                )
    return chosen

def _take(study, strata, kept, chosen, used, med):
    for activity, need in (("high", 3), ("low", 2)):
        while kept[study][activity] < need and strata[study][activity]:
            market = strata[study][activity].pop(0)
            match = poly_match(market, used)
            if not match:
                continue
            used.add(match["slug"])
            kept[study][activity] += 1
            chosen.append({
                "sample_id": f"{study[:3].upper()}-{len([c for c in chosen if c['category']==study]) + 1}",
                "category": study,
                "activity": activity,
                "event": market["question"],
                "question_match": "shared_words_same_window",
                "match_note": (
                    f"Seed {SEED}. Series order and market order are shuffles, not recency. "
                    f"{activity} means Kalshi volume {'above' if activity=='high' else 'at or below'} "
                    f"the wave-1 median of {med:.0f} contracts. Shared words: {match['overlap']}."
                ),
                "kalshi_volume": market["volume"],
                "poly_volume": match["volume"],
                "kalshi": {
                    "ticker": market["ticker"],
                    "series_ticker": market["series_ticker"],
                    "question": market["question"],
                },
                "polymarket": {"slug": match["slug"], "question": match["question"]},
            })
            print(f"  kept {activity} {market['ticker']}", flush=True)
            print(f"    K {market['question']}", flush=True)
            print(f"    P {match['question']}", flush=True)

def main():
    chosen = draw()
    doc = {
        "procedure": {
            "seed": SEED,
            "design": "Shuffle every Kalshi series in the category (seed 42). Examine them in that order, 40 per wave, at most 10 waves. Within a series, shuffle markets that closed from 2023-09-01 through 2026-09-29 with 1,000 to 5,000,000 contracts, and keep at most 4. Freeze the median volume on the first wave. High is above that median; low is at or below. Shuffle each stratum and take the first 3 high and 2 low markets that have a closed Polymarket contract. A match requires two shared content words covering the long Kalshi words, the same round when either question names one, no men/women conflict, agreeing years, close dates within 60 days, and Polymarket volume at most 5 million dollars. The same official release (unemployment, payrolls, CPI, inflation, or jobless claims) in the same month and year also counts. Each Polymarket contract is used once.",
            "why": "A series is equally likely to be anywhere in the shuffle, and markets inside a series are shuffled before any are kept. Close date is a window, not a ranking, so newer markets are not preferred.",
            "window": ["2023-09-01", "2026-09-29"],
            "volume_cap_contracts_and_dollars": MAX_VOLUME,
        },
        "polymarket_spread": {
            "method": "effective_spread",
            "formula": "2 * abs(trade_price - outcome_midpoint_in_the_same_hour)",
            "why": "Polymarket does not archive the order book. Yes and No price history sum to 1, so that series is a midpoint. The parity gap is zero and is not the spread.",
        },
        "markets": chosen,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    print(f"wrote {len(chosen)} markets", flush=True)

if __name__ == "__main__":
    main()
