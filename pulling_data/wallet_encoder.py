import csv
import json
import math
import os
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone

import polymarket_data

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_PATH = os.path.join(HERE, "sample_15.json")
MARKETS_PATH = os.path.join(polymarket_data.OUT_DIR, "markets.csv")
TRADES_DIR = polymarket_data.TRADES_DIR
HIST_DIR = os.path.join(polymarket_data.OUT_DIR, "wallet_histories")
LIST_PATH = os.path.join(polymarket_data.OUT_DIR, "wallet_train_list.csv")
QUEUE_PATH = os.path.join(polymarket_data.OUT_DIR, "label_queue.csv")
LABELS_PATH = os.path.join(polymarket_data.OUT_DIR, "wallet_labels.csv")
MODEL_PATH = os.path.join(HERE, "wallet_encoder.pt")
EMBED_PATH = os.path.join(polymarket_data.OUT_DIR, "wallet_embeddings.csv")

SEED = 42
MIN_SAMPLE_TRADES = 5
MIN_HISTORY_TRADES = 20
MAX_HISTORY_TRADES = 2000
CROP = 50
WINDOW_START = int(datetime(2023, 9, 1, tzinfo=timezone.utc).timestamp())
WINDOW_END = int(datetime(2026, 9, 30, tzinfo=timezone.utc).timestamp()) - 1
N_LABELS = 80
HOLDOUT = 0.2
PAGE = 1000
MAX_PAGES = 8
N_FEATURES = 8
HIDDEN = 64
EMBED = 32
EPOCHS = 8
BATCH = 64
TEMPERATURE = 0.2


def _parse_time(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        return int(value)
    text = str(value).strip().replace("Z", "+00:00")
    if text.endswith("+00"):
        text += ":00"
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _sample_slugs():
    with open(SAMPLE_PATH, encoding="utf-8") as f:
        markets = json.load(f)["markets"]
    return [m["polymarket"]["slug"] for m in markets]


def _closes():
    closes = {}
    with open(MARKETS_PATH, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            closes[row["slug"]] = _parse_time(row.get("closed_time"))
    return closes


def _read_trades(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def select_wallets():
    closes = _closes()
    stats = defaultdict(lambda: {"n": 0, "dollars": 0.0, "cutoff": 0, "markets": set()})
    total_dollars = 0.0
    total_trades = 0
    for slug in _sample_slugs():
        path = os.path.join(TRADES_DIR, f"{slug}.csv")
        close = closes.get(slug) or 0
        for row in _read_trades(path):
            wallet = (row.get("wallet") or "").lower()
            if not wallet:
                continue
            try:
                dollars = float(row.get("usdc_size") or 0)
            except ValueError:
                dollars = 0.0
            item = stats[wallet]
            item["n"] += 1
            item["dollars"] += dollars
            item["markets"].add(slug)
            if close > item["cutoff"]:
                item["cutoff"] = close
            total_dollars += dollars
            total_trades += 1
    chosen = []
    for wallet, item in stats.items():
        chosen.append({
            "wallet": wallet,
            "sample_trades": item["n"],
            "sample_dollars": round(item["dollars"], 2),
            "sample_markets": len(item["markets"]),
            "cutoff_ts": item["cutoff"],
        })
    chosen.sort(key=lambda r: (-r["sample_trades"], r["wallet"]))
    os.makedirs(polymarket_data.OUT_DIR, exist_ok=True)
    with open(LIST_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["wallet", "sample_trades", "sample_dollars", "sample_markets", "cutoff_ts"])
        writer.writeheader()
        writer.writerows(chosen)
    active = [r for r in chosen if r["sample_trades"] >= MIN_SAMPLE_TRADES]
    _write_label_queue(active)
    print(f"wallets in the 15 markets: {len(stats)}")
    print(f"trades: {total_trades}  dollars: {total_dollars:.0f}")
    print(f"pull list: {len(chosen)}")
    print(f"label queue drawn from {len(active)} wallets with {MIN_SAMPLE_TRADES}+ trades in the 15")
    print(f"wrote {LIST_PATH}")
    print(f"wrote {QUEUE_PATH}")
    return chosen


def _write_label_queue(chosen):
    rng = random.Random(SEED)
    ordered = sorted(chosen, key=lambda r: r["sample_trades"])
    n = len(ordered)
    bins = [ordered[i * n // 4:(i + 1) * n // 4] for i in range(4)]
    per_bin = N_LABELS // 4
    picked = []
    for i, group in enumerate(bins):
        pool = list(group)
        if i == len(bins) - 1:
            pool.sort(key=lambda r: (-r["sample_trades"], r["wallet"]))
            forced = pool[:5]
            rest = pool[5:]
            rng.shuffle(rest)
            picked.extend(forced + rest[:per_bin - len(forced)])
            continue
        rng.shuffle(pool)
        picked.extend(pool[:per_bin])
    with open(QUEUE_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["wallet", "sample_trades", "sample_dollars", "label"])
        writer.writeheader()
        for row in picked:
            writer.writerow({
                "wallet": row["wallet"],
                "sample_trades": row["sample_trades"],
                "sample_dollars": row["sample_dollars"],
                "label": "",
            })


def _dedup_key(raw):
    return (
        raw.get("transactionHash"),
        (raw.get("proxyWallet") or "").lower(),
        raw.get("asset"),
        raw.get("side"),
        raw.get("price"),
        raw.get("size"),
    )


def pull_histories():
    if not os.path.exists(LIST_PATH):
        select_wallets()
    with open(LIST_PATH, encoding="utf-8") as f:
        wallets = list(csv.DictReader(f))
    os.makedirs(HIST_DIR, exist_ok=True)
    fields = ["transaction_hash", "wallet", "side", "outcome", "asset_token_id",
              "condition_id", "price", "size", "usdc_size", "timestamp", "title"]
    for i, row in enumerate(wallets, 1):
        wallet = row["wallet"]
        path = os.path.join(HIST_DIR, f"{wallet}.csv")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            continue
        kept = []
        seen = set()
        end_cursor = None
        for _ in range(MAX_PAGES):
            params = {"user": wallet, "limit": PAGE, "offset": 0, "takerOnly": "false"}
            if end_cursor is not None:
                params["end"] = end_cursor
            batch = polymarket_data._get(polymarket_data.DATA_API_BASE, "/trades", params=params)
            if not batch:
                break
            min_ts = None
            passed_start = False
            for raw in batch:
                ts = raw.get("timestamp")
                if ts is None:
                    continue
                ts = int(ts)
                if min_ts is None or ts < min_ts:
                    min_ts = ts
                if ts > WINDOW_END:
                    continue
                if ts < WINDOW_START:
                    passed_start = True
                    continue
                key = _dedup_key(raw)
                if key in seen:
                    continue
                seen.add(key)
                kept.append(polymarket_data.normalize_trade(raw))
            if min_ts is None or len(batch) < PAGE or passed_start or len(kept) >= MAX_HISTORY_TRADES:
                break
            nxt = min_ts - 1
            if end_cursor is not None and nxt >= end_cursor:
                break
            end_cursor = nxt
        kept.sort(key=lambda r: int(r["timestamp"] or 0))
        kept = kept[-MAX_HISTORY_TRADES:]
        tmp = path + ".part"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(kept)
        os.replace(tmp, path)
        if i <= 5 or i % 50 == 0 or i == len(wallets):
            print(f"[{i}/{len(wallets)}] {wallet} {len(kept)} trades", flush=True)


def _hour_cycle(ts):
    hour = datetime.fromtimestamp(int(ts), timezone.utc).hour
    angle = 2 * math.pi * hour / 24
    return math.sin(angle), math.cos(angle)


def trades_to_rows(rows):
    parsed = []
    for row in rows:
        try:
            ts = int(float(row["timestamp"]))
            price = float(row["price"])
            size = float(row["size"] or 0)
            usdc = float(row["usdc_size"] or 0)
        except (TypeError, ValueError):
            continue
        parsed.append((ts, price, size, usdc, row.get("side") or "", row.get("outcome") or ""))
    parsed.sort()
    out = []
    prev = None
    for ts, price, size, usdc, side, outcome in parsed:
        gap = 0 if prev is None else max(0, ts - prev)
        prev = ts
        sin_h, cos_h = _hour_cycle(ts)
        out.append([
            math.log1p(max(usdc, 0)),
            price,
            1.0 if side.upper() == "BUY" else 0.0,
            1.0 if outcome.lower() == "yes" else 0.0,
            math.log1p(gap),
            sin_h,
            cos_h,
            math.log1p(max(size, 0)),
        ])
    return out


def load_histories():
    sequences = {}
    if not os.path.isdir(HIST_DIR):
        return sequences
    for name in os.listdir(HIST_DIR):
        if not name.endswith(".csv"):
            continue
        rows = trades_to_rows(_read_trades(os.path.join(HIST_DIR, name)))
        if len(rows) >= MIN_HISTORY_TRADES:
            sequences[name[:-4].lower()] = rows
    return sequences


def _standardize(sequences):
    count = 0
    total = [0.0] * N_FEATURES
    for rows in sequences.values():
        for row in rows:
            count += 1
            for i, value in enumerate(row):
                total[i] += value
    mean = [v / count for v in total]
    var = [0.0] * N_FEATURES
    for rows in sequences.values():
        for row in rows:
            for i, value in enumerate(row):
                var[i] += (value - mean[i]) ** 2
    std = [math.sqrt(v / count) or 1.0 for v in var]
    scaled = {}
    for wallet, rows in sequences.items():
        scaled[wallet] = [[(value - mean[i]) / std[i] for i, value in enumerate(row)] for row in rows]
    return scaled, mean, std


def _crop(rows, start, length):
    piece = rows[start:start + length]
    if len(piece) < length:
        piece = piece + [[0.0] * N_FEATURES] * (length - len(piece))
    return piece


def build_model():
    import torch
    from torch import nn

    class Encoder(nn.Module):
        def __init__(self):
            super().__init__()
            self.gru = nn.GRU(N_FEATURES, HIDDEN, batch_first=True)
            self.proj = nn.Sequential(
                nn.Linear(HIDDEN, HIDDEN),
                nn.ReLU(),
                nn.Linear(HIDDEN, EMBED),
            )

        def forward(self, x, lengths):
            packed = nn.utils.rnn.pack_padded_sequence(
                x, lengths.cpu(), batch_first=True, enforce_sorted=False
            )
            _, hidden = self.gru(packed)
            return nn.functional.normalize(self.proj(hidden[-1]), dim=-1)

    class Head(nn.Module):
        def __init__(self):
            super().__init__()
            self.fc = nn.Linear(EMBED, 1)

        def forward(self, z):
            return self.fc(z).squeeze(-1)

    return Encoder(), Head()


def _nt_xent(z1, z2):
    import torch
    from torch.nn import functional as F

    z = torch.cat([z1, z2], dim=0)
    sim = z @ z.T / TEMPERATURE
    n = z1.size(0)
    labels = torch.cat([
        torch.arange(n, device=z.device) + n,
        torch.arange(n, device=z.device),
    ])
    eye = torch.eye(sim.size(0), device=sim.device, dtype=torch.bool)
    sim = sim.masked_fill(eye, -1e9)
    return F.cross_entropy(sim, labels)


def _batches(sequences, wallets, rng):
    order = list(wallets)
    rng.shuffle(order)
    for start in range(0, len(order), BATCH):
        chunk = order[start:start + BATCH]
        left, right, lengths = [], [], []
        for wallet in chunk:
            rows = sequences[wallet]
            span = len(rows) - CROP
            a = 0 if span <= 0 else rng.randrange(span + 1)
            b = 0 if span <= 0 else rng.randrange(span + 1)
            left.append(_crop(rows, a, CROP))
            right.append(_crop(rows, b, CROP))
            lengths.append(min(len(rows), CROP))
        yield left, right, lengths


def train():
    import torch

    print("loading histories", flush=True)
    raw = load_histories()
    print(f"loaded {len(raw)} wallets", flush=True)
    if len(raw) < 32:
        raise SystemExit(
            f"Need at least 32 wallets with {MIN_HISTORY_TRADES}+ trades in {HIST_DIR}. Found {len(raw)}. Run pull first."
        )
    wallets = sorted(raw)
    rng = random.Random(SEED)
    rng.shuffle(wallets)
    cut = int(len(wallets) * (1 - HOLDOUT))
    train_w = wallets[:cut]
    hold_w = wallets[cut:]
    print(f"standardizing {len(train_w)} train wallets", flush=True)
    sequences, mean, std = _standardize({w: raw[w] for w in train_w})
    for wallet in hold_w:
        sequences[wallet] = [
            [(value - mean[i]) / std[i] for i, value in enumerate(row)]
            for row in raw[wallet]
        ]
    print(f"training {len(train_w)} wallets, holdout {len(hold_w)}", flush=True)
    torch.manual_seed(SEED)
    encoder, head = build_model()
    opt = torch.optim.Adam(encoder.parameters(), lr=1e-3)
    for epoch in range(EPOCHS):
        encoder.train()
        losses = []
        for left, right, lengths in _batches(sequences, train_w, random.Random(SEED + epoch)):
            x1 = torch.tensor(left, dtype=torch.float32)
            x2 = torch.tensor(right, dtype=torch.float32)
            leng = torch.tensor(lengths)
            loss = _nt_xent(encoder(x1, leng), encoder(x2, leng))
            opt.zero_grad()
            loss.backward()
            opt.step()
            losses.append(loss.item())
        encoder.eval()
        hold_losses = []
        with torch.no_grad():
            for left, right, lengths in _batches(sequences, hold_w, random.Random(SEED)):
                x1 = torch.tensor(left, dtype=torch.float32)
                x2 = torch.tensor(right, dtype=torch.float32)
                leng = torch.tensor(lengths)
                hold_losses.append(_nt_xent(encoder(x1, leng), encoder(x2, leng)).item())
        print(f"epoch {epoch + 1} train {sum(losses) / len(losses):.3f} holdout {sum(hold_losses) / max(len(hold_losses), 1):.3f}")
    labeled = _load_labels()
    head_state = None
    if labeled:
        head_state = _fit_head(encoder, sequences, labeled, head)
    torch.save({
        "encoder": encoder.state_dict(),
        "head": head_state,
        "mean": mean,
        "std": std,
    }, MODEL_PATH)
    print(f"wrote {MODEL_PATH}")
    print(f"encoder wallets: {len(train_w)} train, {len(hold_w)} holdout")


def _load_labels():
    if not os.path.exists(LABELS_PATH):
        return {}
    labels = {}
    with open(LABELS_PATH, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            text = (row.get("label") or "").strip().lower()
            if text in {"1", "institutional", "informed"}:
                labels[row["wallet"].lower()] = 1
            elif text in {"0", "retail"}:
                labels[row["wallet"].lower()] = 0
    return labels


def _fit_head(encoder, sequences, labels, head):
    import torch

    wallets = [w for w in labels if w in sequences]
    if len(wallets) < 20 or len(set(labels[w] for w in wallets)) < 2:
        print(f"skipping the label head: {len(wallets)} labeled wallets with long enough histories")
        return None
    encoder.eval()
    xs, ys = [], []
    with torch.no_grad():
        for wallet in wallets:
            rows = sequences[wallet][-CROP:]
            x = torch.tensor([_crop(rows, 0, CROP)], dtype=torch.float32)
            leng = torch.tensor([len(rows)])
            xs.append(encoder(x, leng))
            ys.append(labels[wallet])
    x = torch.cat(xs, dim=0)
    y = torch.tensor(ys, dtype=torch.float32)
    opt = torch.optim.Adam(head.parameters(), lr=1e-2)
    loss_fn = torch.nn.BCEWithLogitsLoss()
    for _ in range(200):
        opt.zero_grad()
        loss = loss_fn(head(x), y)
        loss.backward()
        opt.step()
    print(f"label head fit on {len(wallets)} wallets, loss {loss.item():.3f}")
    return head.state_dict()


def apply_embeddings():
    import torch

    if not os.path.exists(MODEL_PATH):
        raise SystemExit(f"Missing {MODEL_PATH}. Run train first.")
    blob = torch.load(MODEL_PATH, map_location="cpu", weights_only=False)
    encoder, head = build_model()
    encoder.load_state_dict(blob["encoder"])
    encoder.eval()
    use_head = blob.get("head") is not None
    if use_head:
        head.load_state_dict(blob["head"])
        head.eval()
    mean, std = blob["mean"], blob["std"]
    sequences = load_histories()
    fields = ["wallet", "n_trades"] + [f"z{i}" for i in range(EMBED)] + ["institutional_score"]
    with open(EMBED_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for wallet, rows in sequences.items():
            scaled = [[(value - mean[i]) / std[i] for i, value in enumerate(row)] for row in rows[-CROP:]]
            x = torch.tensor([_crop(scaled, 0, CROP)], dtype=torch.float32)
            leng = torch.tensor([len(scaled)])
            with torch.no_grad():
                z = encoder(x, leng)[0]
                score = ""
                if use_head:
                    score = round(float(torch.sigmoid(head(z.unsqueeze(0)))[0]), 4)
            rec = {"wallet": wallet, "n_trades": len(rows), "institutional_score": score}
            for i, value in enumerate(z.tolist()):
                rec[f"z{i}"] = round(value, 6)
            writer.writerow(rec)
    covered = 0.0
    listed = 0.0
    if os.path.exists(LIST_PATH):
        with open(LIST_PATH, encoding="utf-8") as src:
            for row in csv.DictReader(src):
                dollars = float(row["sample_dollars"])
                listed += dollars
                if row["wallet"] in sequences:
                    covered += dollars
    print(f"wrote {len(sequences)} embeddings -> {EMBED_PATH}")
    if listed:
        print(f"embeddings cover {covered / listed:.1%} of training-list dollars in the 15 markets")
    label_groups()


def label_groups():
    import torch

    if not os.path.exists(EMBED_PATH):
        raise SystemExit(f"Missing {EMBED_PATH}. Run apply first.")
    with open(EMBED_PATH, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    z = torch.tensor([[float(row[f"z{i}"]) for i in range(EMBED)] for row in rows])
    n = torch.tensor([int(row["n_trades"]) for row in rows])
    g = torch.Generator().manual_seed(SEED)
    cent = z[torch.randperm(z.shape[0], generator=g)[:2]].clone()
    labels = None
    for _ in range(40):
        labels = torch.cdist(z, cent).argmin(dim=1)
        for j in range(2):
            mask = labels == j
            if int(mask.sum()) > 0:
                cent[j] = z[mask].mean(dim=0)
        cent = torch.nn.functional.normalize(cent, dim=1)
    mm = 0 if n[labels == 0].median() >= n[labels == 1].median() else 1
    names = {mm: "Market Maker", 1 - mm: "Non Market Maker"}
    fields = list(rows[0].keys())
    if "group" not in fields:
        fields.append("group")
    for i, row in enumerate(rows):
        row["group"] = names[int(labels[i])]
    tmp = EMBED_PATH + ".part"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, EMBED_PATH)
    counts = {}
    for row in rows:
        counts[row["group"]] = counts.get(row["group"], 0) + 1
    for name, count in counts.items():
        print(f"{name}: {count}")


def demo():
    import torch

    rng = random.Random(SEED)
    wallets = [f"w{i}" for i in range(64)]
    sequences = {}
    for wallet in wallets:
        length = rng.randint(MIN_HISTORY_TRADES, 48)
        sequences[wallet] = [[rng.uniform(-1, 1) for _ in range(N_FEATURES)] for _ in range(length)]
    torch.manual_seed(SEED)
    encoder, _ = build_model()
    opt = torch.optim.Adam(encoder.parameters(), lr=1e-3)
    losses = []
    while len(losses) < 3:
        for left, right, lengths in _batches(sequences, wallets, rng):
            x1 = torch.tensor(left, dtype=torch.float32)
            x2 = torch.tensor(right, dtype=torch.float32)
            leng = torch.tensor(lengths)
            loss = _nt_xent(encoder(x1, leng), encoder(x2, leng))
            opt.zero_grad()
            loss.backward()
            opt.step()
            losses.append(loss.item())
            if len(losses) == 3:
                break
    print(f"demo steps {len(losses)} last loss {losses[-1]:.3f}")


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "select"
    if command == "select":
        select_wallets()
    elif command == "pull":
        pull_histories()
    elif command == "train":
        train()
    elif command == "apply":
        apply_embeddings()
    elif command == "label":
        label_groups()
    elif command == "demo":
        demo()
    else:
        raise SystemExit("Use select, pull, train, apply, label, or demo.")


if __name__ == "__main__":
    main()
