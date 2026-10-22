# Warsaw offers website

For deployment to your own public Cloudflare Workers account, see [the direct Cloudflare guide](../web/cloudflare/README.md). That version uses D1 and does not require ChatGPT viewer sign-in. It is prepared separately from the existing private Sites deployment; configuring code alone does not publish it or change the live publisher destination. Set `WEBSITE_PROVIDER=cloudflare` only after bootstrap and verification. The existing deployment remains the default `sites` provider.

The website displays a curated ranking of Warsaw apartments from the project's cloud PostgreSQL catalogue. Each offer shows its asking price, the experimental model estimate, the model price gap, its last observation time and a link to the original advertisement. The page also shows when its feed was generated and which model produced it.

The deployed website is [Warsaw Deal Hunter](https://warsaw-deal-hunter-filip.filipskopczynski.chatgpt.site). It has **private viewer access**: authorized visitors sign in through the hosting access gate. Publishing source code on GitHub does not make this hosted site public. The hosting access gate and the ingestion endpoint's credentials serve different purposes: viewing the page does not grant permission to replace its data or download the saved model.

## Source and hosting architecture

The canonical frontend lives in `web/index.html`, `web/app.js` and `web/styles.css`. A portable copy of the hosting source is retained under `web/hosting/`, including API routes, validation tests, build-source plugins and dependency lockfile. Run `python web/sync_hosting.py` after frontend edits, and `python web/sync_hosting.py --check` to verify that its served assets match. This copies HTML to `hosting/site-assets/` and JavaScript/CSS to `hosting/public/`; it does not deploy changes.

The hosted API runs as a Vinext/Cloudflare Worker, with persistent R2 binding `BUCKET`. R2 contains the curated feed at `website/deals.json` and the approved Python model at `website/model.joblib`. The website server has **no Supabase connection or database secret**. The Python publisher alone opens a read-only, repeatable-read PostgreSQL transaction, reuses the fitted model and publishes a validated subset. No raw catalogue or model is sent to the browser.

The retained `.openai/hosting.json` has `d1: null`, `r2: "BUCKET"` and no registered `project_id`. A new independent deployment needs its own identity through the normal Sites creation flow, private access configuration, binding and secrets. See [the hosting template guide](../web/hosting/README.md). Cloning the repository does not recreate a registered Site or copy its stored model/feed.

## How updates work

1. The existing **Warsaw OLX and Otodom catalogue** workflow collects listings and stores observations in Supabase.
2. After a successful run on the repository's `main` default branch, **Warsaw offers website feed** reads the current database catalogue and downloads the previously approved model from the website's protected `/api/model` endpoint.
3. Python reuses that fitted model to score the latest catalogue, applies the ranking's quality, freshness and provenance rules, and prepares a small JSON feed.
4. Python sends that feed to the protected `/api/ingest/deals` endpoint. The server stores it in R2; the browser reads the curated data through `/api/deals`.

There is no model training in this workflow. The website feed can contain newer observations than the original model experiment. Replacing or retraining a model remains a separate manual operation. The experiment's original held-out metrics describe that experiment, not the accuracy of every subsequent daily ranking.

The catalogue job is scheduled at **06:15 Europe/Warsaw**, with a broader refresh on Sunday. The website job follows successful collection, rather than running at an unrelated fixed time. GitHub may delay scheduled jobs. Collection can finish successfully after bounded or partial traversal, so a green run does not establish complete coverage or a fresh price check for every listing. The feed preserves each listing's real observation time.

If collection fails, the automatic website update does not start. If scoring or publishing fails, the page may retain an older feed; check its generated-at time. A missing model or missing configuration must be resolved before a feed can be published. An empty eligible ranking is a valid result and must not be replaced with invented offers.

## Repository configuration

Set these in **GitHub repository → Settings → Secrets and variables → Actions**:

| Kind | Name | Purpose |
| --- | --- | --- |
| Secret | `DATABASE_URL` | Existing SSL PostgreSQL connection for catalogue reads |
| Secret | `WEBSITE_INGEST_TOKEN` | Authorizes server-side feed/model requests |
| Secret | `WEBSITE_SERVICE_TOKEN` | Authorizes the non-browser client through the website hosting access gate |
| Variable | `WEBSITE_URL` | The website's HTTPS base URL, without a path or query |
| Variable | `WEBSITE_ENABLED` | Set to `true` only after the website and trusted model are configured |

Set `WEBSITE_URL` to `https://warsaw-deal-hunter-filip.filipskopczynski.chatgpt.site` for the current deployment. Keep `WEBSITE_INGEST_TOKEN` in the website's server-side hosting secrets; provision `WEBSITE_SERVICE_TOKEN` through the hosting service-access configuration. Never place either token, the database URL or the serialized model in browser code, a public JSON feed or Git history. GitHub stores this workflow's successful curated JSON feed as an artifact for seven days; assume it has the same audience as the repository's Actions artifacts. The upload path deliberately excludes models, environment files and raw database exports.

The workflow accepts only a successful same-repository `main` catalogue run, or a manual dispatch on `main`, while `main` is the default branch. It checks out trusted `main` code and does not download or execute artifacts from the triggering workflow. Pull requests cannot start the privileged publisher. Concurrent publishers are serialized.

Once configuration is ready, open **Actions → Warsaw offers website feed → Run workflow** on `main` to test an update. The scheduled collector will trigger later updates automatically. The workflow is disabled until `WEBSITE_ENABLED=true`; creating the YAML file alone does not configure credentials, host a website or enable publication.

## Approved model bootstrap and API contract

The maintainer uploads only an owner-reviewed, trusted joblib artifact from this project's training pipeline. Model storage is separate from daily scoring; the website does not train or automatically replace models.

| Method and path | Purpose | Application authentication |
| --- | --- | --- |
| `PUT /api/ingest/model` | Store a trusted model, up to 10 MiB | Ingestion bearer token |
| `GET /api/model` | Restore the approved model for Python scoring | Ingestion bearer token |
| `POST /api/ingest/deals` | Validate and store a newer curated JSON feed, up to 512 KiB | Ingestion bearer token |
| `GET /api/deals` | Read the curated feed, suppressing offers older than seven days | Authorized private viewer access |

Non-browser requests must first pass the hosting access gate using `OAI-Sites-Authorization: Bearer <WEBSITE_SERVICE_TOKEN>`. The three privileged model/ingestion routes also require `Authorization: Bearer <WEBSITE_INGEST_TOKEN>`. Tokens are supplied at runtime, not substituted into these source files.

Model bootstrap uses the binary joblib file as the `PUT /api/ingest/model` request body, `Content-Type: application/octet-stream` and an `X-Model-SHA256` header containing its lowercase SHA-256 digest. The server calculates the digest before storing the model and returns HTTP 400 for a mismatch. Download responses include the same hash header; Python verifies it before deserialization. The hash checks transport integrity, while the maintainer's review establishes model trust. The browser cannot upload a model through the dashboard.

Publish the matching feed after an approved model replacement: feed ingestion requires its `model_sha256` to match the stored model. It also rejects equal/older capture timestamps and uses an R2 conditional write to prevent concurrent publishers from silently overwriting one another. The feed read endpoint flags a feed older than 36 hours as stale and removes individual offers whose observation age exceeds seven days; it does not move their observation timestamps forward.

## Local operation

Activate the project's Python environment and run from the repository root. Keep local secrets in the ignored `.env` file or your process environment.

Generate a local feed with the existing local model and current database catalogue:

```powershell
python -m src.website_feed --output data/website/deals.json
```

Use the protected website's persisted model and publish the result:

```powershell
python -m src.website_feed --download-model --publish-url https://warsaw-deal-hunter-filip.filipskopczynski.chatgpt.site --output data/website/deals.json
```

The first command generates a file without publishing it. Both commands read Supabase; neither collects portal pages nor trains a model. The second requires the website tokens in the local environment, plus an approved model already stored on the server. Do not load serialized models supplied by untrusted people: Python model serialization can execute code when loaded.

The terminal launcher `python best_offers.py --top 5` continues to read its existing `data/deals.csv`. Website feed generation does not silently replace that separate local CSV or retrain its model.

## Interpreting the ranking

The price gap is `(model estimate − asking price) / model estimate`. It measures disagreement with an experimental asking-price model, rather than confirmed savings or a verified market valuation. A listing's last observation can predate feed generation. Portal presence does not prove the apartment is still available; disappearance does not establish that it sold.

The existing model's small validation set, incomplete features, imperfect tenure screening and unresolved cross-portal physical-property duplicates remain relevant. A large gap can be caused by data errors or property characteristics the model does not capture. Check the original advertisement and full terms before interpreting an offer. The website uses ordinary Python scoring and scheduled collection, with no LLM agent or OpenAI API dependency.
