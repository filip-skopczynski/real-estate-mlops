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
| Exact joins for available apartments | 120 |
| Government/website price mismatches | 0 |

These counts describe that snapshot, rather than a permanent inventory. Prices are gross apartment asking prices; parking and other extras are excluded from the model target.

## Join and time rules

The adapter matches the **exact apartment number** between the two sources. A saved identity has the form `5252801624:Bemovo PH1:A0/01`, with source `dane.gov.pl:39940`.

The complete available residential inventory must have a government price and website features. Government apartment rows with no matching features, a commercial match, duplicate identities or a price difference above PLN 0.01 stop collection. Sold, reserved and unpriced commercial units are excluded. The current parser also restricts the developer, project, city and address.

Area, room count and floor come from website features. Area is **never reconstructed by dividing total price by price per square metre**. Build year, coordinates and distance remain missing until a reliable source supplies them; district is `Bemowo`.

`observed_at` records capture time as an aware UTC timestamp. The government snapshot date must equal the observation's calendar day in `Europe/Warsaw`, and each price's validity interval must contain capture time. Publication dates are not substituted for observation time.

## Collection and outputs

A normal collection reads four public resources: dataset metadata, latest-resource metadata, its CSV, and the homepage HTML. Requests have bounded retries, redirects, timeouts and response size; HTTP 403/429 stops collection. The adapter calls no private API and executes no website JavaScript.

```powershell
.\.venv\Scripts\python.exe -m src.fetch_bemovo
```

The default command saves `data/bemovo.csv` and `data/bemovo_audit.json` locally. PostgreSQL writes require the explicit `--save-db` flag.

The GitHub workflow is still configured to call the generic `src.fetch_data` JSON-LD adapter. It is **not connected to the Bemovo pilot**, and its production pipeline remains **OFF**. Run this pilot manually.

The report records source/resource identifiers, snapshot and capture times, inventory counts, join checks, source-content digests and feature provenance. Digests use decoded UTF-8 text with any leading BOM removed. Collected CSV files, downloaded source data and generated reports are kept outside version control. Unit tests use synthetic dictionaries and fake HTTP responses.

The first live collection at `2026-10-05T10:59:53.553837+00:00` stored 120 current apartments and 120 history observations in Supabase. A read-back check confirmed that IDs, prices, area, rooms and floor match the local snapshot; preprocessing retained all 120 rows. No real-data model was trained from this snapshot.

## Evaluation limits

The first snapshot has one observation day and covers one investment. It is insufficient for the default temporal train/validation/test split. An explicitly exploratory group split can exercise the training path, but cannot establish future-period accuracy or Warsaw-wide market performance. Asking prices also differ from completed transaction prices.

## Availability limits

Database upserts retain previously seen apartments. When a later snapshot excludes an apartment because it was sold, reserved or disappeared, the existing database row is not marked inactive or removed. `last_seen_at` records the last saved observation and does not establish that the apartment is still available.

Use the fresh pilot CSV and its capture time to inspect the available inventory. Current-listing queries from the database can include older apartments until an explicit availability or expiry policy is implemented.
