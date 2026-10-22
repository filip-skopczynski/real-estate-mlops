# Direct Cloudflare deployment

This deployment serves the existing dashboard publicly on your own Cloudflare Workers account. Visitors do not need ChatGPT sign-in. Python scoring and the main listing database remain in GitHub Actions and Supabase.

The Worker uses a small D1 database for the approved model and latest curated feed. The public endpoint exposes only screened offers. Model download and both update endpoints require the server-only `WEBSITE_INGEST_TOKEN`. No Supabase credentials are installed in this Worker.

Cloudflare Workers and D1 include free allowances, with request, CPU, storage and query limits. This setup does not require an R2 subscription or a paid Workers plan. Check your account's current plan and [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/) and [D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/) before enabling anything paid. Requests may fail when free limits are exceeded; this is not an unlimited hosting promise.

## Prepare and validate

Use Node.js 22.13+ (Node 24 recommended). From this directory:

```powershell
npm ci
node build.mjs
node --test tests/worker.test.mjs
npx wrangler deploy --dry-run
```

The build copies only the three canonical files from `web/` into ignored `public/`, plus a response-header configuration. It fails if unexpected files are present. Server source, credentials, model binaries and raw listing exports are not static assets.

## Connect your Cloudflare account

```powershell
npx wrangler login
npx wrangler whoami
npx wrangler d1 list
```

Complete sign-in and the authorization prompt in your own browser. Do not paste Cloudflare API credentials into a chat or source file. If you already have the state database for this project, reuse its actual ID instead of creating another.

Create a dedicated database if needed:

```powershell
npx wrangler d1 create warsaw-deal-hunter-state --jurisdiction eu
node configure.mjs ACTUAL_D1_DATABASE_UUID ACTUAL_CLOUDFLARE_ACCOUNT_ID
```

The IDs come from Cloudflare's responses. `configure.mjs` saves those non-secret IDs in ignored `wrangler.account.json`; it refuses to silently replace an existing account/database selection. The checked-in `wrangler.jsonc` is a template, with an explicit database-ID placeholder. Do not deploy its placeholder.

Apply the reviewed schema and deploy:

```powershell
npx wrangler d1 migrations apply DB --remote --config wrangler.account.json
node build.mjs
npx wrangler deploy --config wrangler.account.json
npx wrangler secret put WEBSITE_INGEST_TOKEN --config wrangler.account.json
```

Use a newly generated, long random ingestion token dedicated to this deployment. Store the same value as an encrypted GitHub Actions secret; never put it in `wrangler.jsonc`, browser code, a commit or an ordinary shell argument. The initial page can load before a ranking exists, but `/api/deals` will return HTTP 503 until the trusted model and first feed have been uploaded.

The returned `https://…workers.dev` address is the deployed origin. A custom domain is optional. Before publishing over an existing Worker name, inspect that Worker and preserve unrelated applications.

## Move the model and daily publisher

Keep the existing Sites website and its publisher configuration working until these checks pass:

1. Upload this project's reviewed fitted `models/model.joblib` to `PUT /api/ingest/model` on the new origin. Send its raw bytes, `Content-Type: application/octet-stream`, `X-Model-SHA256`, and `Authorization: Bearer <ingestion token>`. The server checks the digest. This direct D1 deployment accepts model files up to **1 MiB**; the first fitted model is about 168 KB. Its base64 representation and the bounded feed fit D1's [2,000,000-byte row limit](https://developers.cloudflare.com/d1/platform/limits/).
2. Generate and publish a current feed from the repository root, using the new token in the process environment:

   ```powershell
   python -m src.website_feed --provider cloudflare --download-model --publish-url https://YOUR-ACTUAL-WORKER.workers.dev
   ```

3. Read `/api/deals` anonymously and verify its generated time, offer count and model hash. Check that anonymous `/api/model` and update requests return HTTP 401. Check the dashboard and original offer links in a browser.
4. Update the repository Actions secret `WEBSITE_INGEST_TOKEN` and variables `WEBSITE_URL` and `WEBSITE_PROVIDER=cloudflare`. Preserve `WEBSITE_ENABLED=true` only after the new deployment is ready. The older `WEBSITE_SERVICE_TOKEN` is used only in `sites` mode; Cloudflare requests never send it.
5. Dispatch **Warsaw offers website feed** on `main`. Verify success and read back the newly stored feed. Later successful daily collection runs trigger the same updater automatically.

Do not change the repository's live URL before bootstrap and access checks succeed. The Python publisher's default provider remains `sites`, preserving the earlier deployment while this migration is prepared. No cloud resource is created and no destination is changed by copying these files or running the local tests.

## Storage and consistency

The schema has one state row. Feed publication is a conditional SQL update requiring the approved model hash and a strictly newer capture time, so an older or concurrent publisher cannot overwrite a newer feed. Changing the model clears the old feed; uploading the identical model preserves it. Reads use the primary D1 session where supported.

The read endpoint removes observations older than seven days and marks a ranking older than 36 hours as stale. These rules preserve actual observation times. Neither an observation nor a model gap establishes current availability or verified savings.

The Worker executes no Python, deserializes no model and opens no PostgreSQL connection. The model is an authenticated transport artifact for the trusted Python publisher only. There are no LLM/API or scheduled Worker training calls.
