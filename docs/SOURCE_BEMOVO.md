# Bemovo source pilot

This adapter joins published developer asking prices with apartment features from a public website. Its scope is **Bemovo PH1 in Warsaw's Bemowo district**, developer NIP `5252801624`. It is a pilot for one investment and supplies no evidence of prediction quality across Warsaw.

## Sources and verified snapshot

- Government dataset: [dane.gov.pl dataset 39940](https://dane.gov.pl/pl/dataset/39940), with published **CC0 1.0** terms.
- Government resource audited: `2743163`, snapshot date **2026-10-05**.
- Supplementary features: [Bemovo's public homepage](https://bemovo.pl/pl/). The government dataset's licence describes the government price source.

The source audit for that date found:

| Check | Result |
| --- | ---: |
| Government CSV rows | 263 |
| Apartment price rows | 120 |
| Extra-item rows excluded from apartment prices | 143 |
| Website units | 125 |
| Website available apartments | 120 |
| Website sold units | 5 |
| Commercial units among the sold units | 2 |
| Residential apartments in the complete inventory | 123 (120 available, 3 sold) |
| Exact joins for available apartments | 120 |
| Government/website price mismatches | 0 |

These counts describe that snapshot, rather than a permanent inventory. Prices are gross apartment asking prices; parking and other extras are excluded from the model target.

## Join and time rules

The adapter matches the **exact apartment number** between the two sources. A saved identity has the form `5252801624:Bemovo PH1:A0/01`, with source `dane.gov.pl:39940`.

In default price mode, every available residential apartment must have a government price and website features. Government apartment rows with no matching features, a commercial match, duplicate identities or a price difference above PLN 0.01 stop collection. Sold and reserved apartments are excluded from the available-price CSV but retained in the residential status inventory. Commercial units are excluded from that inventory. The current parser also restricts the developer, project, city and address.

Area, room count and floor come from website features. Area is **never reconstructed by dividing total price by price per square metre**. Build year, coordinates and distance remain missing until a reliable source supplies them; district is `Bemowo`.

`observed_at` records capture time as an aware UTC timestamp. In price mode, the government snapshot date must equal the observation's calendar day in `Europe/Warsaw`, and each price's validity interval must contain capture time. Publication dates are not substituted for observation time. Availability-only captures record the actual HTML collection time independently of government price dates.

## Collection and outputs

A normal collection reads four public resources: dataset metadata, latest-resource metadata, its CSV, and the homepage HTML. Requests have bounded retries, redirects, timeouts and response size; HTTP 403/429 stops collection. The adapter calls no private API and executes no website JavaScript.

```powershell
.\.venv\Scripts\python.exe -m src.fetch_bemovo
```

The default command saves `data/bemovo.csv` and `data/bemovo_audit.json` locally. The CSV contains available apartment prices; the audit's `inventory.apartments` contains the complete residential inventory with published `available`, `reserved` and `sold` statuses. PostgreSQL writes require the explicit `--save-db` flag:

```powershell
.\.venv\Scripts\python.exe -m src.fetch_bemovo --save-db
```

This saves available prices and residential statuses atomically and adds `database_availability_counts` to the report. The 120 available / three sold residential apartments above describe the 2026-10-05 audit. A fresh capture is needed to confirm today's inventory. These date-specific counts are separate from any later verification of the cloud availability tables.

### Availability-only capture

For a 2026-10-06 capture, a latest government resource dated 2026-10-05 fails the default price-date rule without changing saved records. The independent availability mode can still capture current published statuses:

```powershell
.\.venv\Scripts\python.exe -m src.fetch_bemovo --availability-only
.\.venv\Scripts\python.exe -m src.fetch_bemovo --availability-only --save-db
```

It reads one public homepage HTML page, parses and validates the full residential catalogue, and excludes commercial units. No government metadata/CSV is requested and no government/website price comparison is performed. Outputs are `data/bemovo_availability.csv` and `data/bemovo_availability_audit.json`; the earlier price CSV and audit remain intact.

The optional database save writes statuses and the capture header atomically using `prices_complete=False` and no price rows. It never appends price observations or refreshes old asking prices. The catalogue still has to be complete, and the same scope, UTC time, replay and disappearance rules apply. Counts from this mode belong to its own capture time, independently of the 2026-10-05 price audit.

The GitHub workflow is still configured to call the generic `src.fetch_data` JSON-LD adapter. It is **not connected to the Bemovo pilot**, and its production pipeline remains **OFF**. Run this pilot manually.

The price report records source/resource identifiers, snapshot and capture times, inventory counts, join checks, source-content digests and feature provenance. The availability report records the website capture and status inventory separately. Digests use decoded UTF-8 text with any leading BOM removed. Collected CSV files, downloaded source data and generated reports are kept outside version control. Unit tests use synthetic dictionaries and fake HTTP responses.

The first live collection at `2026-10-05T10:59:53.553837+00:00` stored 120 current apartments and 120 history observations in Supabase. A read-back check confirmed that IDs, prices, area, rooms and floor match the local snapshot; preprocessing retained all 120 rows. No real-data model was trained from this snapshot.

The live availability-only capture at `2026-10-06T10:00:11.228117+00:00` stored 120 available and three sold residential units, with no reserved or missing units. Two commercial units were excluded. Supabase read-back confirmed all 123 status observations and the capture header; all 120 earlier price observations and their timestamps were unchanged. Replaying the same capture added nothing. All five public storage tables had RLS enabled. The price feed remained dated 2026-10-05, so no fresh price observations were claimed.

## Evaluation limits

The first snapshot has one observation day and covers one investment. It is insufficient for the default temporal train/validation/test split. An explicitly exploratory group split can exercise the training path, but cannot establish future-period accuracy or Warsaw-wide market performance. Asking prices also differ from completed transaction prices.

## Availability contract and limits

The pilot uses scope `Bemovo PH1` and listing prefix `5252801624:Bemovo PH1:`. A successful complete capture is recorded in `inventory_snapshots`; its Boolean `prices_complete` field distinguishes full price/status captures from status-only captures. Current statuses live in `listing_availability`, with history in `listing_availability_observations`. These tables are added through the shared database metadata. Existing price tables are neither altered nor deleted.

`save_inventory_snapshot` requires `complete=True` and a validated nonempty full residential catalogue. It commits supplied price observations, all reported statuses and the capture header in one transaction. With the default `prices_complete=True`, available IDs must exactly match the verified price IDs. With `prices_complete=False`, prices may cover a subset of available IDs or none; the availability-only CLI supplies none. A complete catalogue with zero available apartments is valid when its apartments are all sold or reserved. First-seen unavailable apartments receive status records without invented price observations.

Previously seen apartments absent from a complete capture of their own source/scope/prefix become `missing`. This means absent from the catalogue, **never confirmed sold**. Other investments are unaffected, and reappearance as available restores `available`. An incomplete/invalid capture or storage error leaves existing price and status records unchanged. Identical replays are no-ops; different contents at the same UTC timestamp and new stale captures are rejected.

`read_current_listings(..., available_only=True)`, latest-row preprocessing and candidate scoring exclude tracked sold, reserved and missing apartments. Historical asking prices remain available for training after an apartment becomes unavailable; they are not transaction prices. Availability `last_seen_at` records last presence in the catalogue, including sold/reserved units, while price `last_seen_at` remains the last price observation.

Statuses describe the last successful complete capture; they do not guarantee availability after that time. The generic JSON-LD adapter lacks this full-inventory contract, so untracked listings retain the previous price-only behaviour and disappearance does not retire them. Further work is more developments, genuine multi-day history and wiring this pilot's price/availability ingestion into the currently disabled daily workflow.
