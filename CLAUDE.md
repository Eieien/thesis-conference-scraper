# CLAUDE.md

Scraper for computer science and technology conferences **starting 2026-10-01 to 2027-03-31**
(`WINDOW_START`/`WINDOW_END` in `.env`). For each conference it collects name, dates, location,
description, abstract/paper deadlines and, most importantly, **registration fees**. It combines
several sources, with a FastAPI backend for testing each step on its own. SQLite for now; Alembic
for schema changes. This folder is intentionally **not a git repo**. Don't run `git init`.

## Commands

```powershell
uv sync                                   # install deps
uv run pytest -q                          # tests (network is faked; must stay offline)
uv run ruff check . ; uv run ruff format . # lint + format (rules in pyproject.toml)
uv run alembic revision --autogenerate -m "..." ; uv run alembic upgrade head
uv run uvicorn app.main:app --reload --reload-include ".env" --host 127.0.0.1 --port 8080
uv run python -m scripts.edas_login       # logs in with EDAS_USERNAME/PASSWORD from .env, or by hand
.\scripts\dev.ps1 -Open                  # backend on 8080 + visualizer on 5173, Ctrl+C stops both
uv run python -m scripts.export_mysql    # data -> data/exports/conference_data.sql.gz for phpMyAdmin
# Everything at once: curl -X POST http://127.0.0.1:8080/pipeline/update  (or the dashboard button)
uv run python -m scripts.copy_to_supabase  # whole database -> Supabase schema `scraper` (the API reads it)
.\scripts\dev.ps1 -Prod -Open            # same, but serves the optimised visualizer build
```

- `visualizer/` is a temporary React dashboard over the API (gitignored; the real frontend is
  built by someone else). Its design rules are in `visualizer/DESIGN.md`. It proxies `/api/*` to
  8080 and needs no backend changes. Tabs: Conferences (default: glance figures, search, sort,
  plain-language fee status from `src/state.ts`), Timeline (deadlines on a calendar), Pipeline,
  Listings, Sources, Tools. "Play with the map" hides the UI and leaves the animated desk map.
- **Supabase:** the update chain ends with a `publish` step when `SUPABASE_DB_URL` is set; it
  copies the whole database into the private schema `scraper`, which the public API reads.
  Deploy steps: `docs/DEPLOY.md`. psycopg needs `prepare_threshold=None` (Supabase's pooler).
- `scripts/dev.ps1` runs the backend in its own hidden console (log in `data/backend.log`): a
  uvicorn reload sends Ctrl+C to its whole console, which used to stop the visualizer too.

- The dev server runs on **port 8080** (docs at `/docs`, status at `/health`). Port 8000 is not used.
- The shell may have `VIRTUAL_ENV` pointing at another project. Run `unset VIRTUAL_ENV` (bash) before `uv run`.
- `get_settings()` is cached, so a server that hasn't restarted still has the old `.env` values.
  If `/health` disagrees with `.env`, restart the server.
- On this machine `--reload` has twice failed to pick up code changes. After backend edits,
  check the change is live (or just restart the server) before testing through the API.
- **CORS** (`app/main.py`): any localhost/127.0.0.1 port may call the API from a browser;
  deployed frontends go in `CORS_ORIGINS`. GET only, so a web page can't start scraping runs.
  The frontend's guide is `docs/API.md`.
- **MariaDB copy** (`scripts/export_mysql.py`): conferences, fees, raw_listings and
  alembic_version as a .sql.gz for phpMyAdmin's Import tab (drops and recreates the tables).
  It removes the EDAS account's email, person ID, IP and session token from EDAS page text and
  refuses to write a file that still has them. `raw_listings.source_id` gets `utf8mb4_bin`:
  MariaDB's default collation ignores case and EasyChair has `icai2026` and `ICAI2026`.
  Tested by importing into a throwaway XAMPP MariaDB 10.4 in strict mode.
- **Public server** (DCISM, `conference-scraper-api.dcism.org`): runs the API with
  `READ_ONLY=true` (only the data router; /sources, /pipeline, /browse, /tools are not
  mounted) under pm2 on `127.0.0.1:20280` (the port DCISM's Apache proxies to). Since
  2026-10-17 it reads Supabase's private `scraper` schema (`DATABASE_SCHEMA=scraper`), which
  the update chain's `publish` step refreshes with `copy_to_supabase`; scraping stays on the
  laptop. Setup and redeploy: `docs/DEPLOY.md` section 4. (It used to read a MariaDB copy
  imported by hand; DCISM's DB port is closed from outside.)
- Responses are gzipped. `/conferences` and `/listings` take `compact=true`, which leaves out the
  long text (`description`, `field_sources`) that lists don't show: all 732 conferences drop from
  2.4 MB to 50 KB over the wire.

## Pipeline

`browse` → `collect` → `filter` → `merge` → `enrich` → `export`, each with its own endpoint:

| Step | Code | Endpoint |
|---|---|---|
| Names only, from listing links, nothing saved | `app/api/browse.py`, `SourceAdapter.browse_page` | `GET /browse/{source}` |
| Source → `raw_listings` | `app/pipeline/collect.py`, `app/scrapers/*` | `POST /sources/{name}/preview` (no DB write), `/collect` (background → `/runs/{id}`) |
| Date window + CS/tech topic | `app/pipeline/filter.py` (keywords, Gemini if unclear) | `POST /pipeline/filter` |
| Combine duplicates → `conferences` | `app/pipeline/merge.py` | `POST /pipeline/merge` |
| Homepage crawl + Gemini → fees, deadlines, description | `app/pipeline/enrich.py`, `app/extract/gemini.py` | `POST /pipeline/enrich` (background) |
| Re-check stored fees, find missed ones (no LLM) | `app/pipeline/validate.py`, `run_validate` | `POST /pipeline/validate` (background) |
| Retry unreadable sites (real browser) | `app/pipeline/recover.py` | `POST /pipeline/recover` (background), then enrich |
| CSV/XLSX | `app/pipeline/export.py` | `GET /conferences/export` |

`/tools/*` runs single pieces in isolation (fetch/render a page into `data/recon/`, discover links,
Gemini extract, date/money/location parsers, acronym key). Use these to test a change before
running the pipeline.

## Architecture rules

- **All HTTP goes through `app/scrapers/http.py` `Fetcher`**, which handles the per-domain delay,
  robots.txt, retries, and the disk + DB cache (`fetched_pages`, bodies in `data/cache/`). Don't
  call `httpx` directly from adapters. Logged-in fetches use `cache_namespace="edas"` so they
  never mix with anonymous ones.
- **Adding a source** = one `SourceAdapter` subclass in `app/scrapers/` (`collect`, and optionally
  `fetch_detail` and `browse_page`), registered in `app/scrapers/registry.py`. Sources whose pages
  haven't been inspected yet live in `pending.py` as `NotImplementedAdapter` (the API returns 501).
- **Inspect before writing a parser.** Save real pages with `/tools/fetch` or `/tools/render`, copy
  them into `tests/fixtures/`, and test the parser against them. Don't guess markup.
- **The LLM sits behind `app/extract/base.py` `Extractor`.** Gemini first; with `GROQ_API_KEY`
  set, `get_extractor` wraps it in `FallbackExtractor` and Groq (`app/extract/groq.py`) answers
  whenever Gemini fails.
  Its output schema is `ExtractedPage` in `app/schemas.py`. Those fields deliberately have no
  defaults, so Gemini returns explicit nulls.
- **Gemini must stay free.** The user runs the free tier and wants to be told before anything
  could cost money. `ExtractorStop` (quota used up after retries, or a 401/403/billing error)
  stops a whole batch instead of failing every remaining item; batch loops re-raise it. Never add
  paid APIs or services without asking.
- **Every field value records where it came from** in `conferences.field_sources`
  (`{field: {source, url}}`). Keep this up to date when adding writers. Homepage values
  (`source == "homepage"`) are never overwritten by merge.
- Schema changes: edit `app/models.py`, then autogenerate a migration. Never edit the DB by hand.
  `render_as_batch=True` is on for SQLite.

## Source-specific facts (found by testing; not obvious)

- **WikiCFP** category pages list open CFPs first, then an "Expired CFPs" section in descending
  deadline order. After about 20 pages the site returns the **same stale page forever**, so paging
  stops when a page brings no new event IDs. Rows with `When: N/A` are journals and are skipped.
  The detail page has the homepage (`Link:`), deadlines (machine-readable `v:startDate`) and the
  CFP text.
- **EDAS** needs a login. `scripts/edas_login.py` saves Playwright `storage_state` to
  `data/edas_state.json`. If `EDAS_USERNAME`/`EDAS_PASSWORD` are set in `.env`, it fills the login
  form itself (generic selectors; tested and working) and falls back to a manual login if that
  fails. Only that script reads the credentials. Keep them out of code, logs and the API, and keep
  the password a `SecretStr`. The list and register-page parsers are verified against real pages
  (`tests/test_edas.py`, fixtures redacted: account name, email and person ID replaced). A page
  containing a password field means the session expired (the API returns 401).
- **EDAS robots.txt disallows everything** except `/doc/` and `/web/`. The user chose to scrape it
  anyway: `EDAS_RESPECT_ROBOTS` defaults to false and only EDAS fetchers skip the check. Every other
  source still respects robots.txt, and EDAS keeps the 4 s delay and one-at-a-time rule. Playwright
  runs against EDAS (login script, `/tools/render` with `use_edas_session`) use `playwright-stealth`;
  plain `httpx` fetches with the saved cookies worked without it. EDAS fixtures carry the account
  holder's details, so redact the name, emails and `p=` person ID before saving new ones.
- **EasyChair** `/cfp/area?area=N` pages hold every open CFP of an area in one table (the paging
  is client-side), so it is one request per area (`EASYCHAIR_AREAS`, default Computing and
  Technology). End date, homepage and deadlines are only on `/cfp/<slug>`. There are no
  structured fees; they only appear in CFP text, which goes to `description` for enrich.
- **IEEE** (conferences.ieee.org) answers plain HTTP clients with HTTP 418 on every path
  (robots.txt is 404). The user chose to read it through a real browser (`app/scrapers/ieee.py`,
  Playwright + stealth, at least `IEEE_DELAY_SECONDS` between loads): the search page's own
  `searchfacet` JSON is read as the page receives it, never by reusing IEEE's key. `pos` is a PAGE
  number. Detail pages (`POST /sources/ieee/details`) give each conference's official website. Since
  October 2026 a detail page loads its data from `conference-api.ieee.org/public/details?id=`
  (was `.../conf/details`; same JSON, `entity.eventDetail`). Missing that address looked
  exactly like throttling ("stopped sending detail data"), so check `is_detail_response`
  first when the details run stops at once. The update chain rests 15 min between IEEE's
  search and detail pages.
- **ConferenceIndex** (`/conferences/<topic>/<country>`) is plain HTML, 500 rows a page in date
  order from today, with no date filter; the adapter reads the computer-science page of each
  Asian country and stops paging past the window. Event pages carry structured dates, homepage and
  deadlines, but no fees. The topic is noisy (e.g. bioethics events), so the filter step matters.
- **IEEE Computer Society** (`ieeecs`): computer.org's calendar loads from a robots-disallowed
  `/api/`, so the adapter reads the sitemaps and the `/conferences/cfp-…` pages instead (plain
  paths only; robots.txt forbids query strings). Only a few are in the window and in Asia; they
  are IEEE's own flagship CS events, trusted above aggregators in `SOURCE_PRIORITY`.
- **robots.txt is parsed with Protego** (Google's rules: `*`/`$` wildcards, most specific rule
  wins). The standard library's parser ignored wildcards and applied rules in file order, so a
  robots.txt starting with `Allow: /` (computer.org) let everything through.
- **ACM**: www.acm.org puts /conferences behind a Cloudflare challenge (HTTP 403), the old
  upcoming-conferences page is 404, and robots.txt disallows its calendar. Not scraped; ACM
  events still arrive through WikiCFP and EasyChair.
- **Springer** has no public listing of upcoming conferences anymore: the old springer.com
  conference and LNCS "forthcoming proceedings" pages redirect to author guidelines, and
  link.springer.com / springernature.com answer the bot with HTTP 406 "Access Blocked". Not
  scraped.
- **Tables as text** (`html_clean.html_to_text`): rowspan/colspan cells are repeated in every
  cell they cover (a "Student" row group appears on each of its rows), stacked header rows are
  merged ("IEEE Member / Onsite"), including `<td>` headers (rows without digits), and empty
  cells stay as "-" so prices stay under their column. Before this, two-row headers made the LLM
  shift tiers by one column (A-SSCC, CSDE).
- **Fee spot-check (2026-09-25, 20 Groq-read conferences):** 14 fully right, 3 wrong, 3 missing
  fees; about 87% of stored fees right. Causes, now fixed: `condense` kept only the first row of
  long tables (tables are now kept whole), merged-cell tables (above), and a fee "6" with no
  currency read from a year split over two lines (the validator now rejects fees without a
  currency and numbers glued to a digit across a line break). The prompt now says how to read
  "cell | cell" tables and to skip "-", Free and currency-less numbers. `MIN_USD` is 3.
- **Missed fees:** `validate.fee_mentions(text)` finds prices next to fee words on pages where no
  fee was extracted (skipping hotel, per-page, APC, funding, tuition and spam lines). Enrich and
  `POST /pipeline/validate` set such conferences to `needs_review` with the note "prices on the
  page were not extracted, e.g. ...". First run: 7 flagged, 5 were false alarms, now in tests.
- **Fee tables:** `fees_from_tables` only reads columns whose header names a price and skips
  cancellation, "covers N papers" and description columns (EDAS has all three). A Description
  column becomes the fee's notes.
- **EDAS sessions** break when the network location changes: EDAS answers with a tiny JS redirect
  to `login.php` ("First login from network location"). `_looks_logged_out` catches it; re-run
  `scripts/edas_login.py`.
- **Duplicates already in the table** (`merge.run_dedupe`, also run at the end of every merge):
  `run_merge` only places new listings, so pairs created earlier stayed apart. Pairs = same
  `acronym_key` (which now also drops "EI"/"Scopus" indexing prefixes and suffixes like
  "CIIS--EI") or near-identical names with compatible acronyms ("ICETC"/"ICETCCOM"), start dates
  within 7 days, same country. The one with fees and homepage values is kept; listings, missing
  fields and (if it had none) fees move over. First run (2026-09-29): 14 pairs, 1,168 -> 1,154.
  Co-located events (different acronyms, one venue and date) are never paired.
- **Headline fees** (`app/pipeline/fee_kinds.py`, no LLM, derived on read): each fee is classified
  from its free text (2,617 distinct categories) into who (author/attendee/extra), student,
  member, mode (onsite/virtual), period (early/regular/late) and local (host-country rate).
  `standard_fee` = the best match for a regular, non-member, international author in person;
  `student_fee` = the lowest international student author rate (listener only as fallback).
  Both are on `/conferences` rows (with USD/PHP) and in the export; each fee carries `kind`.
- **Publication history** (`app/pipeline/history.py`, `POST /pipeline/history`, stored in
  `conferences.history`): did earlier editions publish proceedings? Looked up in Crossref (DBLP's
  robots.txt disallows all crawlers). Only a label, "has history" / "no history found", never a
  predatory verdict (the user's rule: history is one factor among several). Common names are
  reused, so a match needs the acronym plus shared words (ICB "Bioethics" is not "Biometrics"),
  fitting edition numbers (a "1st" has none), and a last edition within 4 years (2008-2013
  namesakes don't count; kept as evidence with a note). WASET-flagged events aren't checked:
  they copy real series' names. Lists get `history_label` and `history_years`; the detail view
  and export carry the editions with DOIs.
- **Online attendance** (`app/pipeline/attendance.py`, `POST /pipeline/attendance`, stored in
  `conferences.attendance`, also set by every enrich): "online option" / "online only" / "in
  person only" / None, with evidence. Strongest first: an explicit page rule ("in-person
  presentation is mandatory"; not when followed by "/ online"), a virtual fee, IEEE's
  `event_format` (hybrid/virtual/inperson), then page wording. "Online registration" is not
  online attendance; "fully online" only counts when it is about the event itself (a hackathon
  or a past edition "was held online" misled the first run). Asia 2026-09-29: 324 online option,
  148 in person only, 228 not stated.
- **SQLite takes one writer.** Connections wait up to 60 s for it (`timeout` in `app/db.py`),
  and collect commits each listing together with the run's counts, so no write stays open
  while a page downloads. That lets the sources collect in parallel (different sites, each
  with its own delay). Merge and dedupe hold the lock longer: run them after collection, as
  the update chain does.
- **Update chain** (`app/pipeline/update.py`, `POST /pipeline/update`, the dashboard's "Update
  everything" button): every source at once, then filter, merge, IEEE websites, enrich (scope,
  only new), history (only unchecked) and the MariaDB export, each a run of its own. A failed
  step (EDAS login, AI quota) is recorded and the chain goes on. 2026-10-11: four sources
  collected in parallel instead of one after another.
- **Starting runs is idempotent** (`app/api/run_guard.py`, every POST that starts a run): an
  `Idempotency-Key` header replays the run it started (200 + `Idempotent-Replayed: true`; same
  key, other request = 422); an identical run still going is returned instead of a duplicate;
  runs that would collide get 409 naming the running one (enrich vs ieee_details/recover/
  validate/attendance; same-source collect; update vs everything); startup marks runs a dead
  process left "running" as failed (writing server only, never the read-only one).
- **The Supabase sync is an upsert**: only changed rows are written (`IS DISTINCT FROM`), gone
  rows deleted, in one transaction; a repeat sync writes nothing. Check changes in its output.
- **Full copy in Supabase** (`scripts/copy_to_supabase.py`): every table
  into the private schema `scraper` (no grants for anon/authenticated, RLS without policies, not
  in Supabase's exposed API schemas), replaced in one transaction, EDAS account details scrubbed
  and checked, row counts checked.
- **Tests:** some API tests read whatever database `DATABASE_URL` names; on a fresh checkout run
  `uv run alembic upgrade head` first so they get an empty one.
- **Never run enrich at the same time as the IEEE website lookup** (`ieee_details`). Enrich loads
  its conferences up front; when it saves a "no homepage" result it overwrites the status the
  lookup just reset to `collected`, so the conference keeps its new homepage but is marked
  unreadable. It happened on 2026-10-11 (352 conferences, re-read in run 109). The update chain
  runs the two in order; ad hoc scripts must too.
- **Detail pages are reused for 7 days** (`DETAIL_CACHE_DAYS`, `Fetcher.cache_for`) during
  collection; listing pages keep the 72 h limit so new CFPs still show up. A re-collection
  within a week skips hundreds of 3 s detail fetches per source.
- **Unchanged sites skip the LLM** (`conferences.read_fingerprint`, SHA-256 of the text enrich
  sent): a re-read whose pages give the same text is counted "unchanged" and keeps its status,
  so weekly `retry_no_fees` re-reads spend quota only on changed sites. `skip_unchanged=false`
  after changing the prompt or extractor.
- **Merge pitfall:** many events share generic names ("International Conference on Computer Science
  and Information Technology") and are held at the same venue on the same dates. Matching is by
  normalized acronym (`acronym_key`) plus a start date within 7 days. The name fallback never
  merges two *different* known acronyms or conflicting countries. `tests/test_matching.py` holds
  the real false positives; keep them passing.
- **Continents** are derived when read, not stored: `app/pipeline/geo.py` cleans the stored
  country ("ITALY", "China.", "USA", even US states such as "MI") and maps it. `/conferences`
  takes `continent` (or `Unknown`), each row carries `continent`, `/summary` counts them. The
  country column itself stays as the sources wrote it.
- **Enrich runs `ENRICH_CONCURRENCY` (default 6) conferences at once.** Only the network part
  (homepage crawl, Gemini call) overlaps; each site keeps its own delay through the shared
  `Fetcher`, Gemini calls stay `GEMINI_MIN_INTERVAL_SECONDS` apart, and results are written one
  at a time. About 5x faster than one at a time (11/min vs 2/min). Faster does not mean more per
  day: the free tier's daily request limit is the real ceiling.
- **Dead sites fail fast.** A connection that is refused or never answers gets one retry; then
  the `Fetcher` skips that domain for the rest of the run. Gemini calls time out after 120 s, so
  a call cut off by laptop sleep can't hang a run forever.
- **Fees are validated** (`app/pipeline/validate.py`, no LLM): each amount must appear in the
  text it was read from (any spelling: `1,250`, `1.250,00`, `1 250`), fall between US$5 and
  US$6,000 using rough built-in rates, and not repeat. The result is stored on the fee
  (`check_status` verified | not_found | implausible, `check_note`, `evidence` snippet); only
  verified (or not-yet-checked) fees count towards `fee_min`/`fee_max`. Enrich and merge check new
  fees as they are saved; `POST /pipeline/validate` re-checks stored ones from the fetch cache.
  First full run: 93% verified, 5% not on the page, 1% implausible, 1% duplicates removed.
- **Gemini model rotation.** Free-tier quota and overload are per model, so `GEMINI_BACKUP_MODELS`
  (in `.env`) lists free models tried after `GEMINI_MODEL`. A model that is overloaded (503),
  rate-limited or drops the connection rests a few minutes; one out of daily quota is dropped for
  the run; a batch stops (`ExtractorStop`) only when every model is out. When all are out, the
  only fix is time: overload clears in hours, quota refills over the day.
- `conferences.enriched_at` records when enrich last read a conference, so re-read runs can skip
  what was just read. `/pipeline/enrich` takes `continent` and `retry_no_fees`;
  `/conferences` takes `source` and each row lists its `sources`.
- **Date filters:** `/conferences` and `/conferences/export` take `starts_from`/`starts_to`
  (conference start) and `abstract_from`/`abstract_to` (abstract deadline), inclusive, plus
  `has_abstract`. Only ~15% of Asian conferences list an abstract deadline; the visualizer shows
  the paper deadline in its place, labelled.
- **Scope is Asia** (`SCOPE_CONTINENTS`, default `["Asia"]`). Every conference stays in the
  database, but enrich and export default to the scope (`continent=all` to override), `/summary`
  reports scope counts, and the visualizer opens on it.
- **Fees in USD and PHP.** `app/pipeline/fx.py` converts with ExchangeRate-API's free open-access
  rates (no key), cached daily in `data/fx_rates.json` and refreshed at startup or via
  `POST /tools/refresh-rates`. Their terms require an attribution link wherever converted prices
  are shown (the visualizer's fee table has it). Conversions are computed on read (`fee_min_usd`,
  `fee_max_php`, `amount_usd`, ...) and in the export; nothing converted is stored. A currency
  the rates don't cover gives `null`, never a guess.
- **Groq fallback (free plan, checked from its rate-limit headers: 1,000 requests/day and 8,000
  tokens/min per model; a paid plan would show far more).** Each model has its own minute budget,
  so calls go to whichever of `GROQ_MODELS` has room; pages are cut to `GROQ_MAX_INPUT_CHARS`
  (14,000) by `condense`, which keeps the start plus fee/deadline/date lines. Output is strict
  JSON schema (`strict_schema`). A 429 "per day" drops a model; all dropped stops the batch.
  With Groq behind it, Gemini gives up on a busy model at once (`always_quick`). The key is a
  `SecretStr`; never print it.
- Keep models with tiny free quotas out of `GEMINI_BACKUP_MODELS`: only the "flash-lite" models get
  500 requests/day; `gemini-3-flash-preview`, `gemini-3.5-flash` and `gemini-3.6-flash` get 20. The tiny ones
  answer a availability check, then run out after a handful of calls.
- **Unreadable sites** ("no homepage and no source page text", Asia 102 on 2026-09-25): 50 WASET
  (HTTP 403), 17 refused/silent connections, 11 without a website, 9 robots.txt disallow, 6 dead
  (404/500), 4 other 403/418, 2 back. `POST /pipeline/recover` retries each, and opens the ones
  that reject the crawler in a real browser (Playwright + stealth, one site at a time,
  `RECOVER_DELAY_SECONDS`, robots.txt respected), saving the pages into the fetch cache
  (`Fetcher.store`) and setting the conference back to `collected` for enrich.
- **Predatory organizers:** `app/pipeline/quality.py` `organizer_flag(homepage)` flags WASET
  (waset.org). Derived on read like continents: `organizer_flag` on each `/conferences` row and in
  the export. Flagged sites are not recovered; the visualizer hides them by default.
- **Old editions:** if homepage dates are more than 45 days from the listing's dates, `enrich`
  marks the conference `needs_review` instead of overwriting.

## Testing conventions

- Tests never touch the network: patch `Fetcher.get` (see `tests/test_browse.py`) and use saved
  real pages in `tests/fixtures/`.
- Override settings with `app.dependency_overrides[get_settings]` and clear the override afterwards.
- For live smoke tests, point `DATABASE_URL` at a scratch SQLite file so `data/scraper.db` stays
  clean, and keep requests small (`preview?limit=…`, `browse?pages=1`).

## Data and secrets

- `.env` holds `GEMINI_API_KEY`, the EDAS login and settings. Never print it, log it or commit it (`.env.example`
  is the template).
- `data/` (DB, cache, recon pages, exports, EDAS session) is local-only.
- Keep scraping polite: at least 3 s between requests to a domain (4 s for EDAS), robots.txt
  respected (except EDAS, see above). Don't lower these, and don't run EDAS requests in parallel.

## Status and next steps

- Coverage widened on 2026-09-29: IEEE also reads "Communication, Networking and Broadcast
  Technologies", "Signal Processing and Analysis", "Robotics and Control Systems" and
  "Components, Circuits, Devices and Systems" (exact names from the search facets); WikiCFP 10
  more categories; EasyChair area 2 (Engineering). Result: 97 new Asian conferences (604 -> 701).
  These are now the defaults in `app/config.py`.
- Built: WikiCFP, EDAS, EasyChair CFP, ConferenceIndex, IEEE Computer Society and IEEE
  (browser), the whole pipeline with fee validation, missed-fee detection and site recovery, the
  Groq fallback, fees in USD/PHP, and the `visualizer/` dashboard.
- Window widened to 2027-03-31 on 2026-10-11 (first run of the update chain): Asia 701 -> 1,415
  conferences (1,261 not WASET-flagged), 793 with fees. Few Jan-Mar 2027 events list fees yet.
- **Final verification, 2026-10-12:** fee check over every conference (run 110: 13,874 fees
  confirmed on their pages, 449 no longer there, 81 implausible), then `scripts/audit_data.py`
  (read-only data audit, exits 1 on a must-be-zero failure: all pass), 201 tests, ruff, web
  build, Postgres parity (24/24) and the browser walkthrough (24/24). The audit found and the
  pipeline now fixes: end dates with last year's year (`normalize.sane_end_date`, applied by
  merge and enrich), currency spellings EURO/TL/KM/DA, conferences whose homepage moves the
  start outside the window (kept, not published), and two missed duplicate pairs. Re-run the
  audit after big runs: `uv run python -m scripts.audit_data`.
- Blocked, not scraped: ACM, Springer (see above).
- Fees appear close to the event: a re-read of 178 fee-less Asian conferences on 2026-09-29
  (four days after the last) found fees for only 3. Re-read weekly until the window closes:
  `POST /pipeline/enrich?continent=Asia&retry_no_fees=true`.
- Ideas the user hasn't decided on yet: a CORE rank / publisher column and a "co-located
  cluster" flag (65 venue+date clusters in Asia, often multi-conference organizers).
