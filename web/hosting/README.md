# Warsaw Deal Hunter hosting source

This directory retains the source template used by the project's private website:

https://warsaw-deal-hunter-filip.filipskopczynski.chatgpt.site

The URL requires the hosted Site's authorized viewer access. It is not an anonymous public portfolio demo. The repository contains source code and configuration templates, without hosting identity, service credentials, saved model binaries or listing data.

## Source layout

The canonical frontend is **one directory above this template**:

- `web/index.html`: dashboard document.
- `web/app.js`: filters, offer cards, links and feed loading.
- `web/styles.css`: visual styling.

Run from the repository root after changing the frontend:

```powershell
python web/sync_hosting.py
python web/sync_hosting.py --check
```

The synchronization copies HTML into `site-assets/index.html`, which `app/route.ts` serves, and copies JavaScript/CSS into `public/`. There are no unused extra JavaScript/CSS copies under `site-assets/`. Synchronization updates this retained template only; it does not publish a deployment or modify an independently registered Site checkout.

The API routes live under `app/api/` and share validation/storage functions from `lib/feed.mjs`. Persistent Cloudflare R2 storage holds the curated JSON feed and the trusted model. The website server has no PostgreSQL credentials: the Python publisher reads Supabase in a read-only transaction and sends a bounded feed to the ingestion API. See [the website guide](../../docs/WEBSITE.md) for endpoint authentication and update behaviour.

## Running the template

Use Node.js 22.13 or later. From this directory:

```powershell
npm run install:ci
npm run dev
```

Run the server unit tests without starting a preview:

```powershell
node --test tests/feed.test.mjs
```

`npm run build` creates the hosting build. The retained `.openai/hosting.json` declares `d1: null` and R2 binding `BUCKET`; it deliberately has no `project_id`. To publish a new independent instance, register it through the normal Sites creation flow, which supplies a new hosting identity. Configure private viewer access, the R2 binding, server secret `WEBSITE_INGEST_TOKEN`, service access and an approved model before enabling its publisher. Copying this directory does not grant access to the existing Site or its stored data.

The checked-in `build/` directory contains required source plugins and worker entry points; it is not a generated production build. Generated `dist/`, dependency trees, local R2 state, runtime profiles and environment files are ignored and are not included in this retained source.

The original platform starter documentation is preserved in [STARTER.md](STARTER.md), including portable runtime profiles, local authentication simulation and Cloudflare binding details.
