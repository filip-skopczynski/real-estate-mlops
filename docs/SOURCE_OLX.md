# OLX: local apartment-listing preview

## Scope

`src.fetch_olx` reads one public OLX search-result HTML page for apartments for
sale in Warsaw and writes a small local sample. It is a source-specific preview,
separate from the generic JSON-LD adapter and the Bemovo collector. The project
uses Python collectors and XGBoost without an LLM agent or OpenAI API.

The default source is the [Warsaw apartment-sale search](https://www.olx.pl/nieruchomosci/mieszkania/sprzedaz/warszawa/).
`--url` is restricted to this HTTPS host and search path, with first-page filters.
It does not accept other cities, rental searches or later result pages.

The preview uses no account, cookies from a logged-in browser, API credentials,
internal API calls or proxies. It does not execute JavaScript, visit listing
detail pages or follow pagination. A successful first-page read does not establish
complete catalogue coverage or reliable daily access.

## Run locally

From the repository root, after installing the dependencies:

```powershell
.\.venv\Scripts\python.exe -m src.fetch_olx --max-listings 20 --delay 2
# Offline example with fictional listings:
.\.venv\Scripts\python.exe -m src.fetch_olx --html tests/fixtures/olx_search.html --output data/olx_offline_preview.csv --audit data/olx_offline_audit.json
```

| Option | Purpose |
| --- | --- |
| `--url` | Select first-page filters on the supported Warsaw apartment-sale search URL |
| `--html` | Parse a saved HTML file offline |
| `--output` | Local CSV path; default `data/olx_preview.csv` |
| `--audit` | Local audit path; default `data/olx_preview_audit.json` |
| `--max-listings` | Preview limit; default 20, maximum 100 |
| `--delay` | Request delay in seconds; at least 2 |

The command needs no `.env`, `DATABASE_URL` or Supabase project. It writes no
database records, availability snapshots, training files or model artifacts.
Collected outputs remain in the ignored `data/` directory; do not commit raw
live pages or exports.

The audit records the requested/final URL, UTC observation time, parsed/exported
counts, HTML hash and size, request counts and robots-check result. Offline mode
records that no robots check was performed and makes no HTTP requests. Its
observation time is the local parsing time, not proof of a current live price.

Both outputs are validated and staged before publication. Each file is replaced
atomically; handled replacement errors restore the previous pair. This protects
ordinary write failures, not a process or machine crash between replacements.

## Verified preview

On 2026-10-07 at 08:33:47 UTC, a manual run read robots.txt and one search page
successfully, parsed 46 unique valid advertisements and exported the default
sample of 20 records. The local audit records two requests, no detail-page
requests and no availability inference. Counts can change on the next run.

A separate local replay of the earlier saved search page produced 43 unique
valid advertisements. All 43 total prices and areas matched the corresponding
visible result cards in that same HTML. These are checks of extraction, not
checks of seller statements or completed transaction prices. The live HTML and
CSV are local, ignored files; the checked-in fixture contains fictional ads.

## Data meaning

The verified public-page format is `script#olx-init-config`, containing an
assignment to `window.__PRERENDERED_STATE__`. The adapter parses its JSON payload
without executing JavaScript and reads `listing.listing.ads`. This format is
specific to OLX and can change; it is not a general parser for other portals.

| Source field | Interpretation/check |
| --- | --- |
| Listing/category context | Category `14` with path `nieruchomosci/mieszkania/sprzedaz` |
| Ad ID and URL | Numeric listing ID and canonical OLX link; Otodom `externalUrl` is not followed |
| `location.cityName` | Must be Warsaw |
| `price.regularPrice.value`, `currencyCode` | Positive finite total asking price in PLN; `price_per_m` is ignored |
| `params` key `m`, `normalizedValue` | Published positive area in square metres |
| `params` key `rooms`, `normalizedValue` | Exact mappings `one`, `two`, `three`; `four` means four or more and is skipped |

Price, area and room count are required. Invalid individual advertisements are
skipped; no valid advertisements means an error. The `four` value is a grouped
**four-or-more** label, not an exact count of four. This preview excludes that
group instead of guessing from a description or an LLM. It is therefore biased
toward one-, two- and three-room apartments and cannot establish the market's
room-count distribution. Supporting a lower-bound room feature would require
an explicit future data/model change.

Optional district/floor values are retained only when published in a supported format.
Floor values `floor_-1`, `floor_0` and `floor_1` through `floor_10` have exact
numeric mappings. `floor_11` means above the tenth floor and `floor_17` means
attic; both remain missing rather than being converted to 11 or 17.
Build year is not verified and remains missing. Coordinates are exported only
when the source explicitly sets `show_detailed=True` and supplies a valid pair;
approximate/hidden locations remain missing.

Records describe advertised apartment asking prices, not completed transactions.
The parser exports the listing identifier and URL, total asking price in PLN,
published area, room count, Warsaw location and observation time. Missing optional
features remain missing; area is never derived from price per square metre.
Seller contact information is outside the export.

The preview is bounded to one search page and its record limit. It is not a full
inventory and supplies no confirmed sale or reservation status. Absence from a
sample cannot mark a listing `missing`, `sold` or otherwise unavailable. Repeated
first-page captures also do not resolve refreshed advertisements or the same
apartment listed under different IDs or on another portal.

## HTTP behaviour and access

Online collection checks robots.txt and applies timeouts, response-size bounds
and a delay between requests. HTTP 403/429 stops the preview. Access failures are
not converted into an empty successful inventory. There is no fallback through
another account, proxy or private endpoint.

The current limits are 8 MiB for HTML, 256 KiB for robots.txt and a 20-second
request timeout. A readable, valid robots.txt response with HTTP 200 is required
before the search-page request. Timeout/server-error retries are bounded to
three attempts; robots denial, HTTP 403/429 and unsupported redirects stop
collection. A zero-record parse fails instead of writing a successful empty
preview.

Rule matching supports wildcard/end markers, the most specific matching path,
merged matching crawler groups and an Allow preference for equivalent rules,
following [RFC 9309's matching rules](https://www.rfc-editor.org/rfc/rfc9309.html#section-2.2.2).
Crawler delays and request-rate limits can increase the configured delay. The
transport deliberately has stricter response/redirect limits; it is a bounded
preview client, not a general-purpose crawler.

[OLX robots.txt](https://www.olx.pl/robots.txt) describes crawler preferences;
it is not a licence for storing history or training a model.
[OLX Partner API FAQ](https://developer.olx.pl/articles/faq), point 6, describes
management of the authorized user's own advertisements, not a public export of
other users' listings. This preview does not use that API.

Provider terms, permission for repeated collection and permission to use these
records in a training dataset remain unresolved. A public HTTP response alone
does not settle these questions. The findings and potential access channels are
documented in [OLX_OTODOM.md](OLX_OTODOM.md).

## Integration status

The original preview runs manually and locally. The opt-in
`collect_olx_search` / `parse_olx_search_page` APIs add consecutive public result
pages with strict requested-versus-returned page validation. The separate
`src.daily_listings` runner stores price observations; its daily GitHub job was
enabled on 2026-10-07 after verified database readback. It remains a bounded
sample and performs no training or availability inference. The older generic
production workflow remains disabled. See [DAILY_COLLECTION.md](DAILY_COLLECTION.md)
for the active deployment, observed OLX result ceiling and coverage audit.
