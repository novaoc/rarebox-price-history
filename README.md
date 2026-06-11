# rarebox-price-history

Daily TCGplayer market-price history for every game [Rarebox](https://github.com/novaoc/rarebox)
tracks — Pokémon (EN + JP), Magic, Yu-Gi-Oh!, Lorcana, One Piece, Riftbound —
served as static per-set JSON over raw.githubusercontent (CORS `*`).

Source data: [tcgcsv.com](https://tcgcsv.com) daily price archives
(`prices-YYYY-MM-DD.ppmd.7z`, published since 2024-02-08). A GitHub Action
appends one point per card per day; history accumulates from there.

```
data/{game}/{setKey}.json
{
  "schema": 1,
  "updated": "2026-06-11",
  "cards": { "<cardKey>": { "normal": [[epochDay, usd], ...], "holofoil": [...] } }
}
```

- `game`: `pokemon` `pokemon-ja` `mtg` `yugioh` `lorcana` `one-piece` `riftbound`
- `setKey`: the id the app already uses (pokemontcg.io set id, tcgdex set id,
  scryfall code, slug(ygo set name), lorcast code, optcgapi set id, riftcodex id)
- `cardKey`: collector number (normalized) for most games; `OP01-001`-style ids
  for One Piece; YGO set codes; tcgplayer productId for Riftbound
- `epochDay`: days since 1970-01-01 UTC (multiply by 86400000 for JS Date)
- Points are change-only with a 7-day heartbeat; daily granularity for the last
  90 days, weekly beyond.

Rebuild joins: `python3 scripts/build_maps.py` · Backfill:
`python3 scripts/ingest.py --since 2024-02-08 --step 7` · Daily:
`python3 scripts/ingest.py --today`

Prices © TCGplayer, via tcgcsv. For personal collection tracking.
