# Warsaw Real-Estate Deal Hunter

A Python pipeline that collects apartment asking prices, preserves observation history in PostgreSQL, and trains an XGBoost regression model to rank listings below its predicted asking price.

**Current status:** offline tests passed with reproducible fictional data. A live Supabase PostgreSQL connection, table creation and rollback-only storage checks have also passed. The two tables have RLS enabled; test records were rolled back. Credentials are stored only in a local ignored `.env`; a fresh checkout needs its own database configuration. GitHub deployment still requires configuration. The ingestion adapter supports schema.org JSON-LD; no specific property portal has been integrated or verified yet. A real source must be inspected before enabling live collection.

## Architecture

```mermaid
flowchart LR
    A[Public sale listings / saved HTML] --> B[curl_cffi + BeautifulSoup]
    B --> C[(Supabase PostgreSQL)]
    C --> D[Validation + deduplication]
    D --> E[First observations: training]
    D --> F[Latest observations: candidates]
    E --> G[Train / validation / test]
    G --> H[XGBoost + persisted preprocessor]
    H --> I[Price predictions + ranked candidates]
    F --> I
    J[GitHub Actions] --> B
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
| `data/latest.csv` | Latest valid observation for each listing |
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

### 3. Verify an actual listing source

Set `LISTINGS_URL` to a supported Warsaw **sale** listing page. The generic adapter expects structured apartment data containing `floorSize`, `numberOfRooms`, a Warsaw postal address and an `Offer` with price/currency PLN. It understands nested JSON-LD and linked graph objects, and follows bounded same-origin item/detail and pagination links. It does not guess CSS selectors for an unknown website.

First test parsing without touching the cloud database:

```powershell
.\.venv\Scripts\python.exe -m src.fetch_data --html tests/fixtures/listings.html --url https://example.test/warsaw --no-db --output data/parser_demo.csv
# After choosing and verifying a real source:
.\.venv\Scripts\python.exe -m src.fetch_data --no-db --output data/source_check.csv
```

Inspect the rows: sale rather than rent, PLN rather than price per square metre, total area, room count, stable IDs and canonical URLs. The generic parser rejects explicit rental offers; absent sale/rental metadata must be resolved by the source adapter and selected sale-search URL. A zero-record result fails clearly instead of silently updating the model with no data.

Then run the live flow:

```powershell
.\.venv\Scripts\python.exe -m src.fetch_data
.\.venv\Scripts\python.exe -m src.preprocess
.\.venv\Scripts\python.exe -m src.train
```

The first snapshot will usually be insufficient for temporal evaluation. Collect history before using the default `temporal` split. For a clearly labelled exploratory check with at least 30 unique listings, use `python -m src.train --split group`. This does not measure performance on future listings.

### 4. Publish the repository and enable GitHub Actions

Create an empty GitHub repository with default branch `main`. Upload/commit the **contents of this directory at the repository root**, so `.github/workflows/` is at the root. Include source, tests, notebook, README, requirements and `.env.example`; exclude `.env`, local databases, collected CSV files and model binaries.

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

`listings` stores the current state keyed by `(source, listing_id)`. `listing_observations` stores historical snapshots keyed by `(source, listing_id, observed_at)`. Both are updated in one transaction. Repeating the same observation is idempotent; an older observation cannot roll the current price backwards. `first_seen_at` and `last_seen_at` preserve collection history. Observations record **fetch time**, not an unverified publication date.

Preprocessing validates Warsaw, positive finite prices/areas, integer room counts and observation dates. Optional missing values are imputed in the model. Distance is straight-line distance from the configured Warsaw reference point, not travel time. Training uses the earliest valid observation per source/listing ID; candidate scoring uses the latest.

The model uses area, rooms, district, distance, floor, build year and coordinates. `price_per_m2` is available for EDA only and is excluded from predictors because it contains the target. Imputation and categorical encoding are fitted on training data only. Unseen districts are handled by the encoder.

Temporal evaluation keeps whole UTC days separate, approximately 60% training, 20% validation and 20% test. Listing IDs are disjoint. Early stopping uses validation only; the final test is not passed to `fit`. The persisted model is the same model that was evaluated. Compare MAE/RMSE (PLN) and R² against a training-median baseline. [XGBoost sklearn interface](https://xgboost.readthedocs.io/en/stable/python/python_api.html#xgboost.XGBRegressor)

For prediction `P` and asking price `A`, the discrepancy is `(P - A) / P`. A prediction of 1,000,000 PLN and asking price of 850,000 PLN yields 15%. The candidate CSV marks whether a listing was seen during training; discrepancies on training listings can be optimistic.

Limitations: asking prices differ from completed transaction prices; missing property quality/features can explain apparent discounts. Listing-ID deduplication does not match the same property republished under another ID or on another portal. A latest observation does not prove a listing remains active. Temporal evaluation needs more than the minimum three days and 30 listings for a credible market conclusion. There is no drift monitoring, automatic model promotion, transactional artifact registry or source-specific portal adapter yet.

## Exploration and tests

Open `notebooks/01_eda.ipynb` in VS Code and select the project's Python environment. Its price/feature analysis uses the training partition, preserving the holdouts. See [KROK_PO_KROKU.md](KROK_PO_KROKU.md) for a Polish walkthrough.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Offline tests exercise parsing, bounded network behaviour, database history/idempotence, invalid records, leakage boundaries, unknown categories, model serialization and the complete fixture-to-model path. PostgreSQL integration tests also run when `TEST_DATABASE_URL` points at an **isolated test database**. Do not use a production Supabase database for the test suite.

## Next portfolio milestones

1. Verify and version one real data-source adapter with representative HTML fixtures.
2. Collect enough history and report reproducible out-of-time performance and error slices by district/size.
3. Improve cross-listing duplicate detection and property features before claiming deal quality.
4. Add model version/promotion rules, data freshness checks and a small prediction API when the data/model are reliable.

This version is a scheduled ML/data-engineering pipeline. An LLM agent that interprets results and invokes tools can be a later layer, after ingestion and model evaluation are reliable.
