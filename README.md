# Warsaw Real-Estate Deal Hunter

A Python pipeline that collects apartment asking prices, preserves price and availability history in PostgreSQL, and trains an XGBoost regression model to rank available listings below its predicted asking price.

**Current status:** the first real-source price pilot was verified on 2026-10-05: 120 available Bemovo apartments in Warsaw, stored in Supabase and checked by reading them back. On 2026-10-06, a separate live availability capture stored 120 available and three sold residential units, excluding two commercial units. No prices or price timestamps were changed; the government price feed was still dated 2026-10-05. The pilot supports atomic price/availability snapshots and excludes unavailable tracked apartments from candidate scoring. OLX and Otodom have separate public-HTML adapters and a bounded observations runner with opt-in database writes. A dedicated daily portal workflow is prepared but remains disabled until configured. Source parsers and ingestion checks have offline tests; GitHub CI also tests PostgreSQL storage. A fresh checkout needs its own ignored `.env`. One development and one day of price history do not establish Warsaw-wide model quality. The older generic production job remains disabled and is not connected to the pilot adapter.

## Architecture

```mermaid
flowchart LR
    A[Government CSV + developer features] --> B[Source-specific validation + join]
    K[Generic JSON-LD / saved HTML] --> B
    B --> C[(PostgreSQL: prices + availability)]
    C --> D[Validation + deduplication]
    D --> E[First observations: training]
    D --> F[Available latest observations: candidates]
    E --> G[Train / validation / test]
    G --> H[XGBoost + persisted preprocessor]
    H --> I[Price predictions + ranked candidates]
    F --> I
    J[GitHub Actions: tests; production gated] -.-> B
    L[OLX: one public search page] --> M[Local preview CSV + audit]
    N[Otodom: one public search page] --> M
    O[Bounded OLX + Otodom page collection] --> P[Price CSV + coverage audit]
    O -.->|Explicit database write| C
```

The browser impersonation profile is `chrome120`. This changes the HTTP/TLS client fingerprint; it does not execute JavaScript or guarantee that a website accepts requests. Responses such as 403/429 stop the fetch. The collector uses bounded pages/listings, timeouts, retries for transient failures and a delay between requests. [curl_cffi documentation](https://curl-cffi.readthedocs.io/en/v0.11.2/impersonate.html)

## Quick start on Windows

Use Python **3.11 or 3.12**; 3.12 is the local validation environment. Run commands from this repository's root, in the VS Code terminal.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
```

Activation is optional: calling the environment's Python directly also avoids PowerShell execution-policy problems. On Linux/macOS use `python3.12 -m venv .venv` and `.venv/bin/python`.

### 1. Run the fictional demonstration

```powershell
.\.venv\Scripts\python.exe -m tests.demo_data
.\.venv\Scripts\python.exe -m src.preprocess --input data/demo_observations.csv
.\.venv\Scripts\python.exe -m src.train
```

This creates 200 fictional observations of 180 apartments over multiple days. It deliberately includes later price changes. Outputs:

| File | Meaning |
| --- | --- |
| `data/training.csv` | Earliest valid observation for each listing |
| `data/latest.csv` | Latest valid observation, excluding unavailable apartments where statuses are tracked |
| `models/model.joblib` | Fitted preprocessor, model and metadata |
| `models/metrics.json` | Validation/test metrics, baseline and split provenance |
| `data/deals.csv` | Ranked candidates above the discrepancy threshold |

The demonstration proves the software path works. Its scores are **not evidence of real Warsaw market performance**. Generated data/models are ignored by Git.

For a local database demonstration, run `python -m tests.demo_data --sqlite data/demo.db`, set `DATABASE_URL=sqlite:///data/demo.db` in a local `.env`, then run `python -m src.preprocess` without `--input`. SQLite is a test/demo backend; production uses PostgreSQL.

### 2. Connect Supabase PostgreSQL

Open your Supabase project → **Connect** → **Direct** section → **Session pooler** → **URI**. Copy that URI locally, retaining the supplied host and port `5432`; use `sslmode=require`. The session pooler supports IPv4. Standard `postgresql://` / `postgres://` URLs are normalized to the psycopg2 SQLAlchemy driver. [Supabase PostgreSQL connections](https://supabase.com/docs/guides/database/connecting-to-postgres)

Set `DATABASE_URL` in your local `.env`, replacing the password placeholder locally. URL-encode reserved characters in the password. The example contains neutral placeholders:

```dotenv
DATABASE_URL=postgresql+psycopg2://postgres.PROJECT_REF:YOUR_PASSWORD@POOLER_HOST:5432/postgres?sslmode=require
```

The selected setup uses **Data API disabled**, **Automatically expose new tables disabled**, and **Automatic RLS checked**. Confirm these settings in the Supabase dashboard; their backend state has not been verified by this project. The pipeline uses PostgreSQL directly and requires no Supabase API keys. [Supabase API security](https://supabase.com/docs/guides/api/securing-your-api)

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
# Edit .env locally before running these commands.
.\.venv\Scripts\python.exe -m src.database init
.\.venv\Scripts\python.exe -m src.database check
```

Edit an existing `.env` instead of overwriting it. The runtime reads `DATABASE_URL`; an optional local helper value `SUPABASE_DB_PASSWORD` does not configure the connection automatically. Do not put credentials in source code, screenshots, issue descriptions or Git. `.env` is ignored; `.env.example` contains placeholders only.

### 3. Collect the verified Bemovo pilot

The pilot uses the [GH Development 7 dataset on dane.gov.pl](https://dane.gov.pl/pl/dataset/39940) for gross apartment asking prices and the [public Bemovo website](https://bemovo.pl/pl/) for area, room count, floor and availability. It joins exact apartment numbers, checks the developer/address and price-validity interval, and compares prices between both sources. It excludes parking, extras and commercial units. Area comes from published apartment features; it is never reconstructed by dividing price by price per square metre.

Run the local snapshot first; this command does not connect to PostgreSQL:

```powershell
.\.venv\Scripts\python.exe -m src.fetch_bemovo
```

It writes available apartment prices to `data/bemovo.csv` and the audit to `data/bemovo_audit.json`. The audit records source URLs, resource ID, source date, collection time, counts, content hashes and the full residential inventory with `available`, `reserved` or `sold` statuses. Commercial units are excluded from this inventory. Stale resources, missing apartment matches, invalid data and price differences above one grosz stop collection before any price or availability records are changed.

After configuring your own database, store a verified snapshot as well:

```powershell
.\.venv\Scripts\python.exe -m src.fetch_bemovo --save-db
```

With `--save-db`, one transaction saves the available prices and the complete residential inventory. The audit then includes `database_availability_counts`. A first-seen sold or reserved apartment gets a status record without an invented price. A complete inventory containing only sold/reserved apartments is valid and produces no available-price rows; an empty or incomplete catalogue is rejected.

Each successful capture records its actual observation time. In price mode, a later fetch is a new price observation, even if the price is unchanged. No historical observations are fabricated from today's website. Build year and coordinates are unknown for this pilot and remain missing. Running `src.database init` adds any missing availability tables without altering or deleting existing price tables.

#### Refresh availability separately

The default price collection requires the government resource to match the current Warsaw date. For a 2026-10-06 capture, a latest resource dated 2026-10-05 fails that rule and leaves saved records unchanged. Availability can be refreshed independently from the verified public residential catalogue:

```powershell
.\.venv\Scripts\python.exe -m src.fetch_bemovo --availability-only
# Also save the verified status snapshot to your configured database:
.\.venv\Scripts\python.exe -m src.fetch_bemovo --availability-only --save-db
```

This mode reads one public homepage HTML page and validates the full residential catalogue. It writes `data/bemovo_availability.csv` and `data/bemovo_availability_audit.json`. It preserves `data/bemovo.csv` and its price audit, reads no government CSV and performs no price comparison. With `--save-db`, it atomically updates availability and the capture header without adding price observations. Stored asking prices and their observation times remain unchanged.

See [docs/SOURCE_BEMOVO.md](docs/SOURCE_BEMOVO.md) for the source contract, reuse terms and limitations. The government dataset's CC0 label does not apply to the developer website. Raw website HTML and collected data are not committed to this repository.

This first snapshot is useful for checking ingestion and EDA. Do not report a random apartment split from one development as Warsaw market performance. Next steps are more developments, genuine collection history and evaluation that holds out developments and time, followed by connecting the pilot to daily ingestion.

#### Generic JSON-LD adapter

Set `LISTINGS_URL` to a supported Warsaw **sale** listing page. The generic adapter expects structured apartment data containing `floorSize`, `numberOfRooms`, a Warsaw postal address and an `Offer` with price/currency PLN. It understands nested JSON-LD and linked graph objects, and follows bounded same-origin item/detail and pagination links. It does not guess CSS selectors for an unknown website.

First test parsing without touching the cloud database:

```powershell
.\.venv\Scripts\python.exe -m src.fetch_data --html tests/fixtures/listings.html --url https://example.test/warsaw --no-db --output data/parser_demo.csv
# After choosing and verifying a real source:
.\.venv\Scripts\python.exe -m src.fetch_data --no-db --output data/source_check.csv
```

Inspect the rows: sale rather than rent, PLN rather than price per square metre, total area, room count, stable IDs and canonical URLs. The generic parser rejects explicit rental offers; absent sale/rental metadata must be resolved by the source adapter and selected sale-search URL. A zero-record result fails clearly instead of silently updating the model with no data.

The generic adapter does not supply a complete inventory or availability statuses. Its price-only ingestion keeps the previous behaviour: disappearance does not retire a listing. Such untracked listings remain eligible for scoring; availability filtering applies only where a source supplies verified statuses.

For a separately verified JSON-LD source, run:

```powershell
.\.venv\Scripts\python.exe -m src.fetch_data
.\.venv\Scripts\python.exe -m src.preprocess
.\.venv\Scripts\python.exe -m src.train
```

The first snapshot will usually be insufficient for temporal evaluation. Collect history before using the default `temporal` split. For a clearly labelled exploratory check with at least 30 unique listings, use `python -m src.train --split group`. This does not measure performance on future listings.

#### OLX local preview

The initial 2026-10-07 compatibility probe returned HTTP 200 for OLX and Otodom (after one same-host redirect for Otodom), but the generic JSON-LD adapter extracted zero complete apartment records. OLX now has a separate adapter for a small local preview of one public Warsaw apartment-sale search page:

```powershell
.\.venv\Scripts\python.exe -m src.fetch_olx --max-listings 20 --delay 2
# Offline example with fictional listings:
.\.venv\Scripts\python.exe -m src.fetch_olx --html tests/fixtures/olx_search.html --output data/olx_offline_preview.csv --audit data/olx_offline_audit.json
```

It writes `data/olx_preview.csv` and `data/olx_preview_audit.json`. It needs no database connection. The adapter parses apartment records from the embedded `__PRERENDERED_STATE__` data in public HTML, checking the sale category, Warsaw location, total PLN price, area and room count. Malformed individual records are skipped; no valid records means a failed preview. OLX's `four` room value means **four or more**, so the current exact-count parser exports only one-, two- and three-room apartments. It skips the four-or-more group, biasing this sample toward smaller room counts.

The first manual live preview on 2026-10-07 exported 20 records. A separate local replay checked 43 extracted prices and areas against the visible cards in its saved HTML; all matched. These checks validate extraction, not market coverage or model performance.

The adapter reads public HTML without logging in, calling an API or using proxies. Online collection checks robots.txt, uses bounded responses and request delays, and stops on HTTP 403/429. It does not follow pagination, detail pages or Otodom links. `--html` supports parsing a saved page offline; `--output` and `--audit` change the local output paths. The default preview limit is 20 records and the maximum is 100; the minimum request delay is two seconds.

This sample does not establish complete market coverage or confirm a sale when an advertisement disappears. It is separate from PostgreSQL ingestion, model training and the scheduled job. Portal reuse terms and permission to use these records for model training remain unresolved. See [docs/SOURCE_OLX.md](docs/SOURCE_OLX.md) for the preview contract and [docs/OLX_OTODOM.md](docs/OLX_OTODOM.md) for the access findings. Setting `LISTINGS_URL` alone does not configure these portal-specific adapters.

#### Otodom local preview

Otodom has a separate manual preview for one public Warsaw apartment-sale search page:

```powershell
.\.venv\Scripts\python.exe -m src.fetch_otodom --max-listings 20 --delay 2
# Offline example with fictional listings:
.\.venv\Scripts\python.exe -m src.fetch_otodom --html tests/fixtures/otodom_search.html --output data/otodom_offline_preview.csv --audit data/otodom_offline_audit.json
```

The default outputs are `data/otodom_preview.csv` and `data/otodom_preview_audit.json`. The adapter reads the public HTML's `__NEXT_DATA__` JSON without executing JavaScript or calling an API. It verifies the Warsaw apartment-sale context and requires each exported apartment to have a numeric total asking price in PLN, numeric published area and an exact supported room count. The verified room mappings cover one to four rooms; Otodom's `FOUR` was checked against a card showing exactly four rooms. Unknown room codes, development cards and advertisements with hidden prices are excluded. It retains published district/floor values when supported; coordinates, distance and build year remain missing.

The first manual live preview on 2026-10-07 exported 18 unique offers. A separate replay of an earlier captured page checked 17 prices, areas and room counts against its visible cards; all matched. HPR advertising copies are excluded rather than counted as new apartment identities.

The default limit is 20 records, maximum 100, with at least two seconds between requests. Online collection checks robots.txt and stops on HTTP 403/429. No account, proxy, pagination or detail-page requests are used. The preview requires no `.env` or database configuration and does not feed PostgreSQL, model training or the daily workflow. A first-page sample cannot establish completeness or confirm sale/availability changes. Otodom's robots.txt includes `search=yes, ai-input=no, ai-train=no`; model-training reuse is not established. See [docs/SOURCE_OTODOM.md](docs/SOURCE_OTODOM.md) for the verified source format and limitations.

### 4. GitHub Actions

The project is published at [filip-skopczynski/real-estate-mlops](https://github.com/filip-skopczynski/real-estate-mlops). Source, tests, notebook, requirements and `.env.example` are tracked; credentials, local databases, collected CSV files and model binaries are excluded.

#### Bounded daily OLX and Otodom observations

The dedicated runner can collect consecutive public Warsaw apartment-sale search pages from both portals. Its defaults are five pages per portal, 500 validated listings per portal and at least two seconds between requests. Earlier one-page preview commands remain unchanged.

```powershell
# Inspect local CSV and audit first; no database connection:
.\.venv\Scripts\python.exe -m src.daily_listings
# After configuring and checking your own database:
.\.venv\Scripts\python.exe -m src.daily_listings --save-db
```

Outputs are `data/daily_listings.csv` and `data/daily_collection_audit.json`. The database path atomically saves price observations per source and reports new IDs separately from existing IDs and inserted observations. Here **new means first observed in our database**, not newly published or a newly listed property. Source/listing IDs distinguish listings within a portal; cross-portal apartment deduplication remains unresolved. A failed or interrupted source is reported while validated records from successful pages/sources are retained. Ordinary budget limits are reported as incomplete coverage, not as transport failures. No availability snapshot, disappearance inference or model training is performed.

The separate workflow `.github/workflows/daily_portals.yml`, named **Daily OLX and Otodom observations**, is ready for configuration and has not been enabled. Once published on `main`, test it manually through Actions with `save_db` unchecked. This needs no database secret and uploads the CSV/audit for 14 days. Tests run first with an isolated PostgreSQL 16 service. For database writes, add a `DATABASE_URL` repository secret, then test a manual run with `save_db` checked. Enable subsequent scheduled collection by setting the repository variable `PORTAL_COLLECTION_ENABLED=true`.

| Type | Name | Default / purpose |
| --- | --- | --- |
| Secret | `DATABASE_URL` | Supabase Session pooler URI with SSL, required only for writes |
| Variable | `PORTAL_COLLECTION_ENABLED` | Absent/false keeps daily portal collection disabled |
| Variable | `OLX_MAX_PAGES` | `5` |
| Variable | `OTODOM_MAX_PAGES` | `5` |
| Variable | `PORTAL_MAX_LISTINGS` | `500` per source |
| Variable | `REQUEST_DELAY_SECONDS` | `2`, minimum two seconds |

The new schedule targets **06:15 Warsaw time** using `timezone: Europe/Warsaw`. GitHub schedules use the default branch, can be delayed or dropped, and are disabled in public repositories after 60 days without repository activity. Collection also requires `main`. [GitHub timezone syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule), [scheduled workflow limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

This is bounded sampling, not all listings: a captured OLX search reported 4437 visible results but only 1000 results / 25 pages in its accessible search metadata; Otodom reported 20,252 results / 563 pages. Default ordering is not verified as newest publication ordering. Promoted/refreshed listings and shifting pages may cause omissions, and source-specific parser exclusions still apply. Portal reuse/training terms remain unresolved; Otodom signals `ai-input=no, ai-train=no`. Keep automated training separate. See the Polish [daily collection guide](docs/DAILY_COLLECTION.md) for local checks, database setup, activation and coverage limits.

#### Existing generic pipeline

CI runs on pushes and pull requests. Keep `PIPELINE_ENABLED` disabled: the older production job calls the generic JSON-LD adapter and does not use Bemovo, OLX or Otodom. Availability storage is implemented, but connecting the Bemovo pilot to a daily job remains a separate next step. The configuration below applies to a separately verified JSON-LD source, not the new portal runner.

In repository **Settings → Secrets and variables → Actions**, configure:

| Type | Name | Value |
| --- | --- | --- |
| Secret | `DATABASE_URL` | Your Supabase Session pooler URI with SSL |
| Variable | `LISTINGS_URL` | Verified source URL |
| Variable | `PIPELINE_ENABLED` | `true` only after source verification |
| Variable | `TRAIN_SPLIT` | `temporal` (default); optionally `group` for exploration |
| Variable | `MAX_PAGES` | Optional, default `2` |
| Variable | `MAX_LISTINGS` | Optional, default `100` |
| Variable | `REQUEST_DELAY_SECONDS` | Optional, default `2` |
| Variable | `DEAL_THRESHOLD` | Optional fraction, default `0.15` |

Tests run on pushes and pull requests, including an isolated PostgreSQL service. The production job runs only for a schedule/manual run on `main` and when explicitly enabled. Run it first through **Actions → Warsaw listing pipeline → Run workflow**. It ingests observations even while training is waiting for enough history. Model, report and candidate CSV are retained as Actions artifacts for 14 days; the workflow does not commit them to the repository.

The cron expression runs daily at **05:15 UTC** (06:15/07:15 in Warsaw depending on daylight saving). GitHub schedules run from the default branch and can be delayed; this is daily refresh, not a real-time stream. [GitHub workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)

## Data and model design

`listings` stores the latest known asking price keyed by `(source, listing_id)`. `listing_observations` stores price history keyed by `(source, listing_id, observed_at)`. Older price observations cannot roll the current price backwards. These tables retain historical prices when an apartment becomes unavailable; a sold status never converts its asking price into a transaction price. Observations record **fetch time**, not an unverified publication date.

Three additional tables separate inventory checks from price history:

| Table | Purpose |
| --- | --- |
| `inventory_snapshots` | Successful complete captures, identified by source, scope and UTC capture time; `prices_complete` distinguishes price/status and status-only captures |
| `listing_availability` | Latest `available`, `reserved`, `sold` or `missing` status |
| `listing_availability_observations` | Status history, including catalogue disappearances and reappearances |

`save_inventory_snapshot` commits supplied prices, statuses and the capture header atomically. It requires a validated complete residential catalogue. The default `prices_complete=True` requires available IDs to exactly match price IDs. The availability-only CLI supplies no prices and uses `prices_complete=False`, retaining the same completeness, scope, time and replay checks for statuses. Only previously seen IDs within that capture's source/scope/prefix can become `missing`; this means absent from the catalogue, **not confirmed sold**. Reappearing apartments can become available again. An identical replay is a no-op, changed contents at the same UTC time fail, and a new capture older than the latest capture is rejected. Invalid or incomplete snapshots leave existing price and status records unchanged.

`read_current_listings(..., available_only=True)` excludes tracked sold, reserved and missing apartments. Price `last_seen_at` describes the last price observation; availability `last_seen_at` describes the last presence in the full catalogue, including sold/reserved units. Neither timestamp alone proves present-day availability.

Preprocessing validates Warsaw, positive finite prices/areas, integer room counts and observation dates. Optional missing values are imputed in the model. Distance is straight-line distance from the configured Warsaw reference point, not travel time. Training uses the earliest valid price observation per source/listing ID, including historical asking prices of apartments later sold. Latest-row preprocessing and candidate scoring exclude tracked sold, reserved and missing apartments.

The model uses area, rooms, district, distance, floor, build year and coordinates. `price_per_m2` is available for EDA only and is excluded from predictors because it contains the target. Imputation and categorical encoding are fitted on training data only. Unseen districts are handled by the encoder.

Temporal evaluation keeps whole UTC days separate, approximately 60% training, 20% validation and 20% test. Listing IDs are disjoint. Early stopping uses validation only; the final test is not passed to `fit`. The persisted model is the same model that was evaluated. Compare MAE/RMSE (PLN) and R² against a training-median baseline. [XGBoost sklearn interface](https://xgboost.readthedocs.io/en/stable/python/python_api.html#xgboost.XGBRegressor)

For prediction `P` and asking price `A`, the discrepancy is `(P - A) / P`. A prediction of 1,000,000 PLN and asking price of 850,000 PLN yields 15%. The candidate CSV marks whether a listing was seen during training; discrepancies on training listings can be optimistic.

Limitations: asking prices differ from completed transaction prices; missing property quality/features can explain apparent discounts. Listing-ID deduplication does not match the same property republished under another ID or on another portal. Availability describes the last successful complete capture, not changes since then; generic sources without status tracking cannot retire disappeared apartments. Temporal evaluation needs more than the minimum three days and 30 listings for a credible market conclusion. The verified source covers only one primary-market development. There is no drift monitoring, automatic model promotion or transactional artifact registry yet.

## Exploration and tests

Open `notebooks/01_eda.ipynb` in VS Code and select the project's Python environment. Its price/feature analysis uses the training partition, preserving the holdouts. See [KROK_PO_KROKU.md](KROK_PO_KROKU.md) for a Polish walkthrough.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Offline tests exercise parsing, bounded network behaviour, database price history/idempotence, invalid records, leakage boundaries, unknown categories, model serialization and the complete fixture-to-model path. PostgreSQL integration tests also run when `TEST_DATABASE_URL` points at an **isolated test database**. Do not use a production Supabase database for the test suite.

## Next portfolio milestones

1. Add more verified developments with complete residential inventories and stable scope identifiers.
2. Connect the pilot's price/availability ingestion to the daily job, collect history and report performance on unseen developments and future listings, with error slices by district/size.
3. Improve cross-listing duplicate detection and property features before claiming deal quality; evaluate transaction prices in a separate task.
4. Add model version/promotion rules, data freshness checks and a small prediction API when the data/model are reliable.

The project has CI and a gated scheduling configuration; the verified pilot currently runs manually. The chosen scope uses Python collectors and XGBoost without an LLM agent or OpenAI API integration, aiming to stay within free service allowances.
