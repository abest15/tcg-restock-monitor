# TCG Restock Monitor: Pokémon + One Piece

Runs on your PC and pushes alerts to your phone (ntfy app) when sealed Pokémon or One Piece
product restocks online **or at stores near your ZIP**. It also learns *when* each nearby store
tends to restock.

## What you get on your phone

| Alert | Example | Priority |
|---|---|---|
| 🚨 Online restock / pre-order open | "RESTOCK at Target: Delta Reign Elite Trainer Box · ✅ RETAIL PRICE $49.99 · market value $95" + **Buy now** button | urgent for `max` items |
| 🏬 In-store restock near you | "IN-STORE: 30th Celebration ETB at Target Midtown +2 more · 📍 Midtown (2.4 mi) 6 in stock · pickup ✅" + **Order pickup** and **Directions** buttons | high |
| 👀 New listing | A new product page appeared at Target/Best Buy/Walmart (an upcoming drop) | normal |
| 💲 Below MSRP | A watched item listed ≥5% under MSRP | high |
| 🚨 Pokémon Center queue is UP | The virtual queue appeared, so a drop is live | urgent |
| 📍 Local sighting | A Reddit post mentions a watched product + your town | high |
| 📦 Releasing today | 7am on release days (Delta Reign Nov 6, OP-18 Nov 20, …) | high |
| ☀️ Daily brief (8am) | Upcoming releases, last 24h restocks, stores likely to restock today | low |
| 🔮 Tomorrow's forecast (8pm) | "Target Midtown restocked 3 of the last 5 Tuesdays, usually detected morning" | normal |
| 📊 Weekly report (Sun 6pm) | Store restock patterns, market value vs retail, your portfolio value | low |

## Where it looks

| Source | Online | In-store near you | Notes |
|---|---|---|---|
| **Target** | ✅ | ✅ per store, with quantities | The best in-store signal. Store quantity often rises when a delivery is received, before it's on the shelf. |
| **Best Buy** | ✅ | ✅ stores with pickup stock | Needs a free API key from developer.bestbuy.com. Official and very reliable. |
| **Walmart** | ✅ | ✗ | Marketplace scalper listings are ignored. Walmart sometimes blocks bots; the monitor backs off automatically. |
| **Pokémon Center** | ✅ queue + product pages you list | n/a | Exclusive ETBs live here. |
| **Local card shops (Shopify)** | ✅ | ✅ (it's your local shop) | Add your local game stores' websites; most run Shopify. |
| **Costco, Sam's Club, Dollar General, Dollar Tree, Family Dollar, Five Below, Ace Hardware** | ✅ website search (experimental) | via Reddit posts naming the store | Their card stock is mostly in-store. Run `doctor` to see which sites can be read from your connection. |
| **Any other page** | ✅ | – | GameStop, Costco, Sam's, Barnes & Noble… via `pagewatch` text matching. |
| **Reddit** | community | ✅ local sightings | r/PKMNTCGDeals, r/OnePieceTCG + your town names. |
| **Market value** | – | – | What items actually sell for (recent sales, like Collectr shows). Used only so you never get an alert above an item's real value. |

Anything Pokémon or One Piece TCG is caught even if it isn't in `watchlist.yaml`. The watchlist sets
**priority** and **MSRP**. Listings priced above MSRP × 1.15 are treated as scalpers and ignored.

## Setup (about 10 minutes)

1. **Install Python 3.9+**: https://www.python.org/downloads/ (Windows: tick **"Add python.exe to PATH"**).
2. **Install the ntfy app** on your phone (App Store / Google Play).
3. Unzip this folder somewhere permanent (e.g. `Documents\restock-monitor`).
4. Run setup:
   - **Windows:** double-click `setup.bat`
   - **Mac / Linux / Raspberry Pi:** `./setup.sh`

   It asks for your ZIP, drive radius, nearby town names, and optional Best Buy key and local shop websites.
   It then prints your private ntfy topic. Subscribe to it in the app, and you'll get a test alert.
   Finally it runs `doctor`, which tests each retailer from your internet connection.
5. Start it:
   - **Windows:** double-click `start.bat`, and run `install-autostart.bat` once so it starts at login
   - **Mac:** `./start.sh`, or `./install-mac-autostart.sh` for always-on
   - **Linux / Pi:** `./install-service.sh` (runs on boot, restarts itself)

   For the first few minutes it records what's in stock (no spam), then sends "✅ monitor is live".
   After that you only hear about **changes**.

> **Keep the PC awake.** A sleeping computer can't see a 3am drop. Set sleep to "Never" when plugged in,
> or run it on a Raspberry Pi (~$50, uses about as much power as a phone charger).

### Phone tips
- **Android:** in ntfy, open the topic → *Notification settings*. Let **max priority** override Do Not Disturb, and pick a loud sound.
- **iPhone:** allow ntfy notifications and Time-Sensitive alerts. Urgent alerts come through as Time-Sensitive.
- Tapping an alert opens the product page. The buttons open the product or Google Maps directions to the store.

## Commands

```
python -m restockbot run          # monitor (start.bat / start.sh do this)
python -m restockbot doctor       # test every source from your network
python -m restockbot once         # one check of everything, printed (no alerts)
python -m restockbot status       # what's buyable now, in stock near you, recent restocks
python -m restockbot stores       # Targets the bot is watching near you
python -m restockbot patterns     # learned in-store restock days/times per store
python -m restockbot calendar     # upcoming releases
python -m restockbot test-alert   # send a test push
python -m restockbot setup        # re-run the wizard (change ZIP, radius, ...)
```
On Windows, use `.venv\Scripts\python` instead of `python` (or `.venv/bin/python` on Mac/Linux).

## Price rules: retail only, never scalper prices

You only get a buy alert when the price is at retail, or a little above it:

| Retail price | Allowed above retail |
|---|---|
| up to $15 (blisters, mini tins) | $3 |
| up to $35 (bundles, tins, collections) | $5 |
| up to $75 (ETBs, premium collections) | $10 |
| up to $150 | $15 |
| over $150 (booster boxes, UPCs) | 10% |

The price must also be at or below the item's **market value**, meaning what it actually sells for
(recent sales, the same kind of number Collectr shows). That way you never get an alert for something
worth less than you'd pay. Marketplace and scalper listings never pass, and neither does anything without
a known retail price. Costco and Sam's Club bundles are judged per item. Change the allowances under
`price_rules` in config.yaml, or set `retail_only: true` for retail price or less only.

## Drop mode: faster when it matters

Alerts go out the moment the bot sees a change. To see changes sooner without getting blocked, the
bot checks 2-2.5x faster at the times drops actually happen:
- release-day mornings (midnight to 1pm)
- for 30 minutes after any watched product restocks anywhere (drops usually hit several stores at once)
- when the Pokémon Center queue goes up, a local sighting is posted, or a new listing or new set appears

## More in-store stock with free keys

Run `.venv\Scripts\python -m restockbot keys` to add:
- **Best Buy** (developer.bestbuy.com): pickup stock at nearby Best Buys
- **Kroger** (developer.kroger.com, register an app with scope `product.compact`): stock level and aisle
  at nearby King Soopers / Kroger-family stores

## Pokémon vending machines

Machines (in Colorado mostly inside King Soopers and Safeway) don't publish stock or restock times
anywhere. The bot turns collector posts about machines into 🎰 alerts (📍 when they mention your towns)
and learns which machines tend to restock on which days.

## New sets are picked up automatically

You don't need to edit anything when a new set is announced. Twice a day the bot reads the public
Pokémon set list (pokemontcg.io) and the official One Piece English product list. It also learns set
names from retailer listings (pre-orders usually name a new set first). For each new set it:
- pushes "🆕 New set detected"
- adds searches for it at every retailer
- watches it at high priority
- adds its release date to your calendar and morning brief

Retailer searches also run newest-first, so new product pages and pre-orders show up quickly.
Restocks of older sets are still caught by the general "any Pokémon/One Piece card product" rule.
`python -m restockbot calendar` lists upcoming releases and every set the bot has discovered.

## Customizing

- **`watchlist.yaml`**: products, MSRPs, priority (`max` = urgent alarm), release calendar and search terms.
  Already loaded: Delta Reign, all 30th Celebration products, Deck Crafter's Collection, Prismatic
  Evolutions, Ascended Heroes, any Pokémon Center ETB / UPC / booster box, and One Piece OP-15 to OP-18,
  EB-05 and PRBs. Add new sets as they're announced; copy an existing block.
- **`config.yaml`**: radius, check speed, quiet hours, digest times, and turning sources on/off.
  - Pokémon Center exclusives: paste product URLs under `pokemoncenter.product_urls`.
  - GameStop / Costco / anything else: add pages under `pagewatch.pages`.
  - Local card shops: add their domains under `shopify.stores`. Check it's Shopify by visiting
    `https://theirshop.com/products.json`; if you see text data, it works.
- **`target_items.txt`**: paste Target product links (one per line) to always monitor them online and at
  every nearby store. Target limits automated searching, so this is the surest way to cover a product.
  Target links posted on r/PKMNTCGDeals are added automatically.
- **`portfolio.yaml`**: list what you own and what you paid; the weekly report tracks its value.
- `tcgplayer_id` on a watchlist item pins which product the market value is read from. It's the
  number in the product's tcgplayer.com URL.

## How the in-store detection works (and its limits)

- **Target** publishes per-store availability and quantity for each product. The bot checks every
  Target in your radius every ~2 minutes for watched products. A store going from 0 → N, or jumping by 3+,
  is a restock. Target's numbers update when a delivery is **received**, so stock may be in the back room.
  Ask Guest Services, or order for pickup to lock it in.
- **Best Buy** reports which stores have the item for pickup.
- **Patterns**: every in-store restock is saved with its time. After 2–4 weeks the bot knows things like
  "Target Midtown restocks Tue/Thu mornings" and pushes a forecast the evening before. Most stores
  restock through vendors on regular weekdays, so patterns usually hold.
- **Walmart and GameStop** don't expose store-level stock to scripts, so for those the bot relies on
  online checks and local Reddit sightings.
- Retailers change their sites. If a source starts failing, the bot pushes "⚠️ … checks are failing" after
  about an hour and keeps retrying. Run `doctor` to see what's wrong.

## Updates are automatic

Every 6 hours the bot checks https://github.com/abest15/tcg-restock-monitor for a newer version.
When it finds one, it installs it and restarts itself, and your phone gets "🔄 Restock bot updated".
These are never touched: `config.yaml`, `data/`, `portfolio.yaml` and `target_items.txt`.
If you've edited `watchlist.yaml`, your copy is kept and the new one is saved next to it as `watchlist.new.yaml`.
If a new version fails to start, the bot rolls back to the previous one.
To update right away: `.venv\Scripts\python -m restockbot update`. To turn updates off, set
`updates: {enabled: false}` in config.yaml.

## Notes

- Alerts only. It never buys anything or logs into your accounts. Checkout is up to you.
- Checks are paced like a person refreshing pages. Lowering intervals much below the defaults
  gets you blocked faster.
- Data is stored in `data/` (SQLite + log). Delete that folder to reset history.
