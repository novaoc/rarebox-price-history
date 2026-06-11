#!/usr/bin/env python3
"""Build the join tables between TCGplayer (tcgcsv) and the card APIs the
Rarebox app uses. Two outputs:

  maps/groups.json
      { "<categoryId>": { "<groupId>": "<setKey>" } }
  maps/products/{game}/{setKey}.json
      { "<productId>": ["<cardKey>", isVariant ? 1 : 0] }

setKey per game matches what the app can derive:
  pokemon     pokemontcg.io set id (sv8, base1) — joined by set NAME
  pokemon-ja  tcgdex set id — tcgplayer group abbreviation IS the tcgdex id
  mtg         scryfall set code — group abbreviation, validated vs scryfall
  lorcana     lorcast set code — joined by set name
  one-piece   optcgapi set_id — abbreviation "OP01"→"OP-01", name fallback
  yugioh      slug(group name) — the app slugs ygoprodeck set_name the same way
  riftbound   riftcodex set_id — group abbreviation (OGN/UNL/...), name fallback

Run with --new-only to map only groups absent from maps/groups.json (daily CI).
"""

import re
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from common import (  # noqa: E402
    CATEGORIES, MAPS, TCGCSV, err, fetch_json, is_variant_name, load_json, log,
    norm_name, norm_number, product_number, save_json, slug,
)

NEW_ONLY = "--new-only" in sys.argv

# Hand-checked exceptions where name joining fails (extend as logged)
POKEMON_NAME_FIXES = {
    "scarlet and violet base set": "scarlet and violet",
    "sword and shield base set": "sword and shield",
    "sun and moon base set": "sun and moon",
    "xy base set": "xy",
    "black and white base set": "black and white",
}


def pokemon_set_index():
    sets = fetch_json("https://api.pokemontcg.io/v2/sets?select=id,name")["data"]
    return {norm_name(s["name"]): s["id"] for s in sets}


def lorcana_set_index():
    d = fetch_json("https://api.lorcast.com/v0/sets")
    arr = d.get("results", d) or []
    return {norm_name(s["name"]): s["code"] for s in arr}


def onepiece_set_index():
    arr = fetch_json("https://optcgapi.com/api/allSets/")
    return {norm_name(s["set_name"]): s["set_id"] for s in arr}, {s["set_id"] for s in arr}


def riftbound_set_index():
    d = fetch_json("https://api.riftcodex.com/sets")
    return {norm_name(s["name"]): s["set_id"] for s in d.get("items", [])}, \
           {s["set_id"] for s in d.get("items", [])}


def scryfall_codes():
    d = fetch_json("https://api.scryfall.com/sets")
    return {s["code"] for s in d.get("data", [])}


def map_group(game, g, ctx):
    """Return setKey for a tcgplayer group, or None."""
    abbr = (g.get("abbreviation") or "").strip()
    name = norm_name(g.get("name") or "")
    if game == "pokemon-ja":
        return abbr.lower() if abbr else None
    if game == "mtg":
        code = abbr.lower()
        return code if code in ctx["scry"] else None
    if game == "yugioh":
        return slug(g.get("name") or "")
    if game == "pokemon":
        name = POKEMON_NAME_FIXES.get(name, name)
        return ctx["pokemon"].get(name)
    if game == "lorcana":
        return ctx["lorcana"].get(name)
    if game == "one-piece":
        m = re.match(r"^(OP|EB|PRB)-?(\d+)$", abbr, re.I)
        if m:
            sid = f"{m.group(1).upper()}-{m.group(2).zfill(2)}"
            if sid in ctx["op_ids"]:
                return sid
        return ctx["one-piece"].get(name)
    if game == "riftbound":
        if abbr and abbr.upper() in ctx["rift_ids"]:
            return abbr.upper()
        return ctx["riftbound"].get(name)
    return None


def card_key(game, p):
    """Per-game card key from a tcgplayer product. None = not a single card."""
    num = product_number(p)
    if game == "riftbound":
        # riftcodex carries tcgplayer_id per printing — key by productId
        return str(p["productId"]) if num else None
    if not num:
        return None
    if game == "one-piece":
        return num.split("/")[0].strip().upper()
    if game == "yugioh":
        return num.split("/")[0].strip().upper()
    if game == "mtg":
        return num.split("/")[0].strip().lower()
    # pokemon / pokemon-ja / lorcana
    return norm_number(num)


def main() -> int:
    groups_map = load_json(MAPS / "groups.json", {})
    ctx = {}
    log("loading set indexes…")
    ctx["pokemon"] = pokemon_set_index()
    ctx["lorcana"] = lorcana_set_index()
    ctx["one-piece"], ctx["op_ids"] = onepiece_set_index()
    ctx["riftbound"], ctx["rift_ids"] = riftbound_set_index()
    ctx["scry"] = scryfall_codes()

    unmatched = []
    for cat, game in CATEGORIES.items():
        cat_map = groups_map.setdefault(str(cat), {})
        groups = fetch_json(f"{TCGCSV}/{cat}/groups")["results"]
        todo = [g for g in groups if not (NEW_ONLY and str(g["groupId"]) in cat_map)]
        log(f"[{game}] {len(groups)} groups, mapping {len(todo)}")
        for g in todo:
            gid = str(g["groupId"])
            set_key = map_group(game, g, ctx)
            if not set_key:
                unmatched.append(f"{game}: {g.get('abbreviation')!r} {g.get('name')!r}")
                cat_map[gid] = None  # remembered so --new-only skips it
                continue
            cat_map[gid] = set_key
            # product map for this set
            try:
                prods = fetch_json(f"{TCGCSV}/{cat}/{g['groupId']}/products")["results"]
            except Exception as e:  # noqa: BLE001
                err(f"  {game}/{set_key}: products fetch failed ({e})")
                cat_map[gid] = None
                continue
            pmap = {}
            for p in prods:
                key = card_key(game, p)
                if not key:
                    continue
                variant = 1 if (game != "riftbound" and is_variant_name(p.get("name", ""))) else 0
                pmap[str(p["productId"])] = [key, variant]
            if pmap:
                save_json(MAPS / "products" / game / f"{set_key}.json", pmap)
            time.sleep(0.11)
        save_json(MAPS / "groups.json", groups_map)

    if unmatched:
        log(f"\n{len(unmatched)} unmatched groups (no history for these):")
        for u in unmatched[:60]:
            log(f"  - {u}")
    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
