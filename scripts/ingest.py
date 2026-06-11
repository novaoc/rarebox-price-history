#!/usr/bin/env python3
"""Ingest tcgcsv daily price archives into per-set history files.

    python3 scripts/ingest.py --dates 2026-06-10
    python3 scripts/ingest.py --since 2024-02-08 --until 2026-03-01 --step 7
    python3 scripts/ingest.py --today          # daily CI: today, else yesterday

Appends [epochDay, price] points per card/subType. A point is recorded when
the price changed vs the last recorded point, or the last point is >7 days
old (heartbeat, so charts don't look dead). Points older than 90 days are
compacted to one per ISO week at write time.
"""

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    ARCHIVE, CATEGORIES, DATA, HEADERS, MAPS, err, load_json, log, save_json,
)

HEARTBEAT_DAYS = 7
DAILY_WINDOW = 90  # newer than this: keep daily; older: weekly


def day_num(d: dt.date) -> int:
    return (d - dt.date(1970, 1, 1)).days


def download(url: str, dest: Path) -> bool:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)
        return True
    except Exception as e:  # noqa: BLE001
        err(f"  download failed {url}: {e}")
        return False


def sevenzip() -> str:
    for c in ("7zz", "7z"):
        if shutil.which(c):
            return c
    raise SystemExit("7z not found (apt install 7zip / brew install sevenzip)")


def load_product_maps():
    """{(categoryId, groupId): (game, setKey, {productId: (cardKey, isVariant)})}"""
    groups = load_json(MAPS / "groups.json", {})
    out = {}
    pcache = {}
    for cat_s, gmap in groups.items():
        cat = int(cat_s)
        game = CATEGORIES.get(cat)
        if not game:
            continue
        for gid, set_key in gmap.items():
            if not set_key:
                continue
            if (game, set_key) not in pcache:
                pm = load_json(MAPS / "products" / game / f"{set_key}.json", {})
                pcache[(game, set_key)] = {pid: (v[0], bool(v[1])) for pid, v in pm.items()}
            out[(cat, gid)] = (game, set_key, pcache[(game, set_key)])
    return out


def pick_price(row: dict):
    p = row.get("marketPrice") or row.get("midPrice")
    return round(p, 2) if p and p > 0 else None


def ingest_date(date: dt.date, pmaps, store, last_seen, zbin, tmp: Path) -> bool:
    ds = date.isoformat()
    arc = tmp / f"{ds}.7z"
    if not download(f"{ARCHIVE}/prices-{ds}.ppmd.7z", arc):
        return False
    out = tmp / ds
    out.mkdir(exist_ok=True)
    r = subprocess.run([zbin, "x", "-y", f"-o{out}", str(arc)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        err(f"  extract failed {ds}: {r.stderr[:200]}")
        return False
    day = day_num(date)

    root = out / ds
    if not root.exists():  # some archives nest differently — find the date dir
        cands = list(out.glob("*/"))
        root = cands[0] if cands else out

    n_points = 0
    for cat in CATEGORIES:
        cat_dir = root / str(cat)
        if not cat_dir.exists():
            continue
        for gdir in cat_dir.iterdir():
            key = (cat, gdir.name)
            if key not in pmaps:
                continue
            game, set_key, prods = pmaps[key]
            try:
                rows = json.loads((gdir / "prices").read_text())["results"]
            except (OSError, ValueError, KeyError):
                continue
            for row in rows:
                pid = str(row.get("productId"))
                hit = prods.get(pid)
                if not hit:
                    continue
                card_key, is_variant = hit
                if is_variant:
                    continue
                price = pick_price(row)
                if price is None:
                    continue
                sub = (row.get("subTypeName") or "Normal").lower()
                skey = (game, set_key, card_key, sub)
                last = last_seen.get(skey)
                if last is not None and last[1] == price and day - last[0] < HEARTBEAT_DAYS:
                    continue
                store[(game, set_key)][card_key].setdefault(sub, []).append([day, price])
                last_seen[skey] = (day, price)
                n_points += 1

    shutil.rmtree(out, ignore_errors=True)
    arc.unlink(missing_ok=True)
    log(f"  {ds}: +{n_points} points")
    return True


def compact(points, today_day):
    """Daily within DAILY_WINDOW, last-per-ISO-week beyond."""
    cutoff = today_day - DAILY_WINDOW
    weekly = {}
    recent = []
    for d, p in points:
        if d >= cutoff:
            recent.append([d, p])
        else:
            weekly[d // 7] = [d, p]  # last point of each week wins
    old = [weekly[w] for w in sorted(weekly)]
    merged = old + sorted(recent)
    # drop consecutive duplicates, keep first + last of any flat run
    out = []
    for pt in merged:
        if len(out) >= 2 and out[-1][1] == pt[1] and out[-2][1] == pt[1]:
            out[-1] = pt
        else:
            out.append(pt)
    return out


def flush(store, today: dt.date):
    today_day = day_num(today)
    ds = today.isoformat()
    n_files = 0
    for (game, set_key), cards in store.items():
        path = DATA / game / f"{set_key}.json"
        existing = load_json(path, {"schema": 1, "cards": {}})
        ex_cards = existing.get("cards", {})
        for card_key, subs in cards.items():
            ec = ex_cards.setdefault(card_key, {})
            for sub, pts in subs.items():
                cur = ec.get(sub, [])
                last_day = cur[-1][0] if cur else -1
                add = [p for p in sorted(pts) if p[0] > last_day]
                if add:
                    ec[sub] = compact(cur + add, today_day)
        existing["cards"] = ex_cards
        existing["updated"] = ds
        save_json(path, existing)
        n_files += 1
    log(f"wrote {n_files} set files")


def seed_last_seen(pmaps, last_seen):
    """For incremental runs: prime change-detection from data already on disk."""
    seen_sets = set()
    for game, set_key, _ in pmaps.values():
        if (game, set_key) in seen_sets:
            continue
        seen_sets.add((game, set_key))
        d = load_json(DATA / game / f"{set_key}.json", None)
        if not d:
            continue
        for card_key, subs in d.get("cards", {}).items():
            for sub, pts in subs.items():
                if pts:
                    last_seen[(game, set_key, card_key, sub)] = (pts[-1][0], pts[-1][1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", help="comma-separated YYYY-MM-DD list")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--step", type=int, default=1)
    ap.add_argument("--today", action="store_true")
    args = ap.parse_args()

    dates = []
    if args.dates:
        dates = [dt.date.fromisoformat(x) for x in args.dates.split(",")]
    elif args.since:
        d = dt.date.fromisoformat(args.since)
        until = dt.date.fromisoformat(args.until) if args.until else dt.date.today()
        while d <= until:
            dates.append(d)
            d += dt.timedelta(days=args.step)
    elif args.today:
        dates = [dt.date.today()]
    else:
        ap.error("need --dates, --since or --today")

    zbin = sevenzip()
    log("loading product maps…")
    pmaps = load_product_maps()
    log(f"{len(pmaps)} mapped groups")

    store = defaultdict(lambda: defaultdict(dict))
    last_seen = {}
    seed_last_seen(pmaps, last_seen)

    done_any = False
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        for d in dates:
            okd = ingest_date(d, pmaps, store, last_seen, zbin, tmp)
            if not okd and args.today and d == dt.date.today():
                # today's archive not published yet — fall back to yesterday
                okd = ingest_date(d - dt.timedelta(days=1), pmaps, store, last_seen, zbin, tmp)
            done_any = done_any or okd

    if not done_any:
        err("no archives ingested")
        return 1
    flush(store, dates[-1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
