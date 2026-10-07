# Otodom: local apartment-listing preview

## Scope

`src.fetch_otodom` reads one public Warsaw apartment-sale search page and writes
a small local CSV and audit. `src.otodom` parses the embedded data offline. They
use no LLM, OpenAI API, account, private API calls or proxies. They do not execute
JavaScript, visit advertisement details or follow pagination.

The default source is the [canonical Warsaw apartment-sale search](https://www.otodom.pl/pl/wyniki/sprzedaz/mieszkanie/mazowieckie/warszawa/warszawa/warszawa).
The corresponding legacy `/pl/oferty/sprzedaz/mieszkanie/warszawa` URL can redirect
to this canonical search. A supported same-host search redirect is checked before
it is followed. Other cities and rental searches are outside this adapter's scope.

A first-page preview is not a complete inventory or a guarantee of future access.
It does not establish market coverage, present-day availability or model quality.

## Run locally

From the repository root, after installing the dependencies:

```powershell
.\.venv\Scripts\python.exe -m src.fetch_otodom --max-listings 20 --delay 2
# Offline example with fictional listings:
.\.venv\Scripts\python.exe -m src.fetch_otodom --html tests/fixtures/otodom_search.html --output data/otodom_offline_preview.csv --audit data/otodom_offline_audit.json
```

| Option | Purpose |
| --- | --- |
| `--url` | Select the supported public Warsaw apartment-sale search |
| `--html` | Parse a saved HTML file without network access |
| `--output` | Local CSV path; default `data/otodom_preview.csv` |
| `--audit` | Local audit path; default `data/otodom_preview_audit.json` |
| `--max-listings` | Preview limit; default 20, maximum 100 |
| `--delay` | Request delay in seconds; at least 2 |

No `.env`, database connection or API credentials are needed. Collected outputs
remain in the ignored `data/` directory. Raw live HTML and exports are not committed.
Offline fixtures contain fictional listings; running them does not verify a
current live price.

The audit records the requested/final URL, UTC observation time, parsed/exported
counts, HTML size/hash, request counts, bounds and robots-check result. Live
robots metadata also preserves `content_signals`; it does not translate them
into a permission claim. Offline mode records that no robots check was performed
and makes no HTTP requests.

## Verified preview

On 2026-10-07 at 09:22:43 UTC, a manual live run successfully read robots.txt
and the canonical search page. It parsed and exported 18 unique valid apartment
offers, below the default maximum of 20. The audit records two requests and no
detail-page requests, training or availability inference. Counts may change
between requests.

A separate local replay of an earlier captured search page produced 17 valid
offers. All 17 total asking prices, areas and exact room counts matched the
visible cards in that same HTML. These checks validate extraction, not seller
statements, market coverage or completed transaction prices. The live exports
and captured HTML remain local; only fictional fixtures are checked in.

## Verified source format

The parser reads the JSON in `script#__NEXT_DATA__`, using
`props.pageProps.data.searchAds.items`. It never executes the script or follows
API URLs contained in that state. The page context must confirm apartments
(`FLAT`) for sale (`SELL`) in Warsaw on the first search page.

| Source field | Interpretation/check |
| --- | --- |
| Advertisement estate/transaction context | Apartment sale; development wrappers (`INVESTMENT`) are excluded |
| Listing ID and `slug` | Source identity and canonical `/pl/oferta/{slug}` URL; the supported slug ends in `-ID` plus an alphanumeric code |
| `href` | Normal `[lang]/ad/{slug}` presentation only; `hpr/[lang]/ad/{slug}` copies are excluded |
| `location.reverseGeocoding.locations` | Explicit Warsaw city ID/name, with an optional district level |
| `totalPrice.value`, currency | Positive finite numeric total asking price in PLN; hidden prices are excluded |
| `areaInSquareMeters` | Positive finite numeric published area, never reconstructed from price per square metre |
| `roomsNumber` | Exact `ONE`/`TWO`/`THREE`/`FOUR` mappings to 1–4; unknown codes are excluded |
| Floor | `GROUND` is 0, `FIRST` through `TENTH` are 1–10; `ABOVE_TENTH` and `ATTIC` remain missing |

Price, area and an exact supported room count are required. Invalid individual
items are excluded; zero valid apartments is an error. District/floor are optional.
Numeric fields must be actual JSON numbers; locale-formatted strings are not
converted. `FOUR` was checked against a visible card showing exactly four rooms.
The adapter does not assume mappings for other, unverified room codes, which
limits the room-count coverage of this sample.

The search can contain both a normal advertisement and an HPR advertising
presentation of the same slug with a synthetic presentation ID. The parser
exports only the normal `href` template; it skips HPR copies rather than
counting them as another apartment or guessing an identity. No HPR HTTP route
is requested; the observed robots rules disallow `/hpr`.

Coordinates, distance and build year have not been verified for this search
format and remain missing. `mapDetails.radius` and the search's Warsaw boundary
geometries do not provide apartment coordinates. Seller contacts are outside
the export.

Records are advertised asking prices, not transaction prices. The observation
time records the capture/parsing time rather than an unverified publication date.
An offline file's current parsing time does not turn it into a current observation
of the live market.

## HTTP behaviour and reuse scope

Online collection checks robots.txt using the collector's transparent identity,
enforces request delays, timeouts and response-size bounds, and stops on HTTP
403/429. Access failures and empty parses do not become successful empty
inventories. There is no fallback through another account, proxy or private
endpoint. Pagination and listing details are not fetched.

The current bounds are 8 MiB for HTML, 256 KiB for robots.txt, a 20-second request
timeout, three attempts per URL and three supported redirects. A valid readable
robots.txt with HTTP 200 is required before the HTML request. A failed parse
does not replace an earlier successful preview. CSV/audit writes are staged,
and handled replacement failures attempt to restore the previous pair; this is
not a crash-safe two-file transaction.

The public robots and canonical search were readable in the 2026-10-07 source
format check. This check establishes the observed format, not reliable scheduled
access. [Otodom robots.txt](https://www.otodom.pl/robots.txt) included
`search=yes, ai-input=no, ai-train=no`. These signals are preserved as source
constraints; this preview supplies no input to an LLM or model training.
robots.txt is not a data licence. Repeated collection, storage-history and
model-training reuse terms remain unresolved.

[Otodom RE API overview](https://developer.olxgroup.com/docs/overview) describes
publication and management of clients' advertisements; it does not establish a
public export of other users' catalogue. This HTML preview does not use that API.
See [OLX_OTODOM.md](OLX_OTODOM.md) for the access findings.

## Integration status and limitations

The preview is manual and local, separate from PostgreSQL ingestion, price and
availability history, model training and daily collection. The gated production
workflow calls the generic `src.fetch_data`, does not call `src.fetch_otodom`,
and remains disabled.

Absence from one sampled search page cannot mark a listing `missing` or `sold`.
An advertisement can move to another results page, be refreshed or be removed for
many reasons. Duplicate apartments across portals or under replacement IDs are
not resolved by this preview. No accuracy or deal-quality claim follows from a
successful export.
