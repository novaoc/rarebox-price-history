"""Shared plumbing for the Rarebox price-history builder.

Data model (what the app fetches, one file per set, CORS via raw.githubusercontent):

    data/{game}/{setKey}.json
    {
      "schema": 1,
      "updated": "2026-06-11",
      "cards": {
        "<cardKey>": { "<subType>": [[epochDay, price], ...] }
      }
    }

cardKey per game (chosen to be derivable from what the app already has):
  pokemon     collector number, lowercase, no leading zeros ("1", "tg15")
  pokemon-ja  tcgdex localId, same normalization (matches jp-prices.json keys)
  mtg         collector number, lowercase
  lorcana     collector number, no leading zeros
  one-piece   full card id ("OP01-001")
  yugioh      set code ("MRD-060"), uppercase
  riftbound   tcgplayer productId as string (riftcodex exposes tcgplayer_id per card)

Points are appended only when the price changed or the last point is >7 days
old (heartbeat), and compacted to weekly granularity beyond 90 days.
"""

import json
import re
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MAPS = ROOT / "maps"

TCGCSV = "https://tcgcsv.com/tcgplayer"
ARCHIVE = "https://tcgcsv.com/archive/tcgplayer"
HEADERS = {
    "User-Agent": "Rarebox/1.4 (+https://rarebox.io)",
    "Accept": "application/json",
}

# categoryId → app game key
CATEGORIES = {
    1: "mtg",
    2: "yugioh",
    3: "pokemon",
    68: "one-piece",
    71: "lorcana",
    85: "pokemon-ja",
    89: "riftbound",
}

EPOCH_DAY = 86400


def fetch(url: str, retries: int = 3) -> str:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def fetch_json(url: str):
    return json.loads(fetch(url))


def slug(s: str) -> str:
    """Filesystem/URL-safe set key for name-keyed games (yugioh)."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "unknown"


def norm_name(s: str) -> str:
    """Loose name equality for joining tcgplayer groups to game APIs."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\[[^\]]*\]", " ", s)          # "[OP-01]" tags
    s = re.sub(r"^[a-z0-9.]+:\s*", "", s)       # "SV08: " prefixes
    s = s.replace("&", "and")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def norm_number(num: str) -> str:
    """'001/187' → '1'; 'TG15/TG30' → 'tg15'."""
    n = (num or "").split("/")[0].strip().lower()
    n = re.sub(r"^0+(?=[a-z0-9])", "", n)
    m = re.match(r"^([a-z]+)0*(\d.*)$", n)
    if m:
        n = m.group(1) + m.group(2)
    return n


def product_number(p: dict) -> str:
    for e in p.get("extendedData", []):
        if e.get("name") == "Number":
            return e.get("value") or ""
    return ""


def is_variant_name(name: str) -> bool:
    """'Budew (Mirror Foil)' — variant printings share the base Number.
    Purely numeric parens are NOT variants: One Piece base prints are
    named like 'Roronoa Zoro (001)' and skipping them dropped every
    leader card from the history."""
    m = re.search(r"\(((?!.*/)[^)]+)\)\s*$", name or "")
    return bool(m and re.search(r"[a-zA-Z]", m.group(1)))


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path: Path, obj, compact=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        path.write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
    else:
        path.write_text(json.dumps(obj, indent=1, ensure_ascii=False))


def log(msg: str):
    print(msg, flush=True)


def err(msg: str):
    print(msg, file=sys.stderr, flush=True)
