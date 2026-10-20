# Changes

Newest first. The top section is sent to your phone when the bot updates itself.

## 2.1.1
- Update notifications now have buttons to the full change list and the how-to guide

## 2.1.0
- Drop mode: checks 2-2.5x faster on release-day mornings and right after any watched restock, Pokémon Center queue or new listing
- King Soopers / Kroger stores: per-store stock levels with aisle, via Kroger's free API (run `python -m restockbot keys` to add a key)
- Pokémon vending machine reports: collector posts about machines become alerts (📍 when near you), and machine restock days are learned
- Added GameStop and Barnes & Noble website watching
- If TCGplayer is unreachable, alerts fall back to an MSRP check instead of going silent
- Weekly report adds online drop timing per retailer and flags any store that stopped working
- More searches: Pokémon Center ETBs, mini tins, booster packs, One Piece extra and premium boosters

## 2.0.0
- The bot now updates itself from GitHub every 6 hours, so no more copying zip files
- Deal filter: alerts only when the store price beats TCGplayer (market and lowest listing, no shipping); retail-price deals are marked and sent as urgent
- Finds new Pokémon and One Piece sets automatically and searches for them
- Added Foil Pack Fanatic, Costco, Sam's Club, Dollar General, Dollar Tree, Family Dollar, Five Below and Ace Hardware
- Connects like a Chrome browser, which gets blocked far less often
